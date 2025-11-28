from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.bot.context import ServiceContainer, get_services
from app.services.analytics import AnalyticsSnapshot
from app.reports.charts import generate_pie_chart, generate_heatmap, generate_daily_bar_chart

logger = logging.getLogger(__name__)


async def insights_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    await _send_insights(update, context, services)


async def handle_analytics_intent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    *,
    days: int = 7,
) -> None:
    await _send_insights(update, context, services, days=days)


async def _send_insights(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    *,
    days: int = 7,
) -> None:
    telegram_id = update.effective_user.id

    try:
        snapshot = await services.analytics.compute_snapshot(telegram_id, days=days)
    except Exception as exc:  # pragma: no cover
        await update.effective_message.reply_text(f"Не вдалося побудувати зведення: {exc}")
        return

    text = _render_snapshot(snapshot)
    
    # Створюємо inline keyboard з кнопками для графіків
    keyboard_buttons = []
    
    # Кнопка для pie chart (якщо є категорії)
    if snapshot.category_stats:
        keyboard_buttons.append([InlineKeyboardButton("📊 Розподіл по категоріях", callback_data="analytics_chart_pie")])
    
    # Кнопка для heatmap та bar chart (завжди показуємо, якщо є події)
    # Перевіряємо, чи є події для цих графіків
    try:
        tz = ZoneInfo(services.settings.timezone)
        now = datetime.now(tz)
        start = now - timedelta(days=days)
        end = now
        events = await services.calendar.list_events_between(
            telegram_id,
            start=start,
            end=end,
            max_results=250,
        )
        if events:
            keyboard_buttons.append([InlineKeyboardButton("🔥 Теплова карта", callback_data="analytics_chart_heatmap")])
            keyboard_buttons.append([InlineKeyboardButton("📈 Завантаженість по днях", callback_data="analytics_chart_daily")])
    except Exception:
        # Якщо не вдалося отримати події, просто не додаємо ці кнопки
        pass

    # Зберігаємо snapshot в контексті для подальшого використання
    context.user_data["analytics_snapshot"] = snapshot
    context.user_data["analytics_days"] = days

    # Відправляємо повідомлення з кнопками (якщо є що показати)
    if keyboard_buttons:
        reply_markup = InlineKeyboardMarkup(keyboard_buttons)
        await update.effective_message.reply_text(
            text,
            reply_markup=reply_markup,
        )
    else:
        # Якщо немає графіків, просто відправляємо текст
        await update.effective_message.reply_text(text)


def _render_snapshot(snapshot: AnalyticsSnapshot) -> str:
    lines = [
        f"🧠 Інсайти за останні {snapshot.days} днів",
        f"• Зайнятий час: {snapshot.total_hours:.1f} год ({int(snapshot.busy_ratio * 100)}% тижня)",
    ]
    if snapshot.category_stats:
        top_categories = ", ".join(
            f"{stat.label.lower()} — {stat.hours:.1f} год" for stat in snapshot.category_stats[:3]
        )
        lines.append(f"• Категорії: {top_categories}")
    if snapshot.busiest_day:
        lines.append(f"• Найнасиченіший день: {snapshot.busiest_day[0]} — {snapshot.busiest_day[1]:.1f} год")
    lines.append(f"• Довгі блоки (>90 хв): {snapshot.long_blocks}")
    if snapshot.avg_block_minutes:
        lines.append(f"• Середня довжина блоку: {snapshot.avg_block_minutes:.0f} хв")
    lines.append(f"• Сесії звичок: {snapshot.habit_sessions}")
    lines.append(f"• Блоки підготовки (серії): {snapshot.series_blocks}")

    if snapshot.recommendations:
        lines.append("\nРекомендації:")
        for tip in snapshot.recommendations[:3]:
            lines.append(f"— {tip}")
    else:
        lines.append("\nВсе виглядає збалансовано ✅")

    lines.append("\nКоманда /plan допоможе розкласти важливу задачу на блоки.")
    return "\n".join(lines)


async def handle_analytics_chart_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
) -> None:
    """Обробляє натискання на кнопки графіків аналітики."""
    query = update.callback_query
    data = (query.data or "").strip()
    telegram_id = update.effective_user.id

    # Отримуємо snapshot з контексту
    snapshot = context.user_data.get("analytics_snapshot")
    days = context.user_data.get("analytics_days", 7)

    if not snapshot or not isinstance(snapshot, AnalyticsSnapshot):
        await query.answer("❌ Дані аналітики втрачені. Запусти /insights знову.", show_alert=True)
        return

    try:
        if data == "analytics_chart_pie":
            # Pie chart для категорій
            if not snapshot.category_stats:
                await query.answer("Немає даних для графіка категорій.", show_alert=True)
                return

            chart_buf = generate_pie_chart(snapshot.category_stats)
            if not chart_buf:
                await query.answer("Не вдалося згенерувати графік.", show_alert=True)
                return

            await query.answer()
            await query.message.reply_photo(
                photo=chart_buf,
                caption="📊 Розподіл по категоріях",
            )

        elif data == "analytics_chart_heatmap":
            # Heatmap
            tz = ZoneInfo(services.settings.timezone)
            now = datetime.now(tz)
            start = now - timedelta(days=days)
            end = now

            events = await services.calendar.list_events_between(
                telegram_id,
                start=start,
                end=end,
                max_results=250,
            )

            if not events:
                await query.answer("Немає подій для теплової карти.", show_alert=True)
                return

            chart_buf = generate_heatmap(events, days=days)
            if not chart_buf:
                await query.answer("Не вдалося згенерувати графік.", show_alert=True)
                return

            await query.answer()
            await query.message.reply_photo(
                photo=chart_buf,
                caption="🔥 Теплова карта продуктивності",
            )

        elif data == "analytics_chart_daily":
            # Bar chart по днях
            tz = ZoneInfo(services.settings.timezone)
            now = datetime.now(tz)
            start = now - timedelta(days=days)
            end = now

            events = await services.calendar.list_events_between(
                telegram_id,
                start=start,
                end=end,
                max_results=250,
            )

            if not events:
                await query.answer("Немає подій для графіка.", show_alert=True)
                return

            # Генеруємо day_totals з подій
            from app.services.analytics import _extract_datetime

            day_totals: dict[str, float] = {}
            for event in events:
                if hasattr(event, 'start'):
                    start_payload = event.start
                    end_payload = event.end
                else:
                    start_payload = event.get("start")
                    end_payload = event.get("end")

                start_dt = _extract_datetime(start_payload)
                end_dt = _extract_datetime(end_payload)
                if not start_dt or not end_dt:
                    continue

                duration = (end_dt - start_dt).total_seconds() / 60
                if duration <= 0:
                    continue

                day_key = start_dt.strftime("%a %d.%m")
                day_totals[day_key] = day_totals.get(day_key, 0.0) + duration / 60

            if not day_totals:
                await query.answer("Немає даних для графіка.", show_alert=True)
                return

            chart_buf = generate_daily_bar_chart(day_totals)
            if not chart_buf:
                await query.answer("Не вдалося згенерувати графік.", show_alert=True)
                return

            await query.answer()
            await query.message.reply_photo(
                photo=chart_buf,
                caption="📈 Завантаженість по днях",
            )

    except Exception as exc:
        logger.exception("Помилка генерації графіка: %s", exc)
        await query.answer(f"Помилка: {exc}", show_alert=True)

