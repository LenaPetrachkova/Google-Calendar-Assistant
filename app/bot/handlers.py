"""Обробники Telegram-бота Calendar Assist."""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from app.bot.context import (
    FREE_SLOT_EXPECTATION_KEY,
    PendingDeleteContext,
    PendingDeleteItem,
    PendingUpdateDetail,
    PendingUpdateListContext,
    ServiceContainer,
    get_last_event_context,
    get_last_event_query,
    get_last_free_slots,
    get_pending_delete,
    get_pending_delete_list,
    get_pending_update_detail,
    get_pending_update_list,
    get_services,
    pop_pending_create_conflict,
    pop_pending_delete,
    pop_pending_delete_list,
    pop_pending_update_conflict,
    pop_pending_update_detail,
    pop_pending_update_list,
    reset_user_context,
    set_last_event_context,
    set_last_event_query,
    set_pending_delete,
    set_pending_delete_list,
    set_pending_update_detail,
    should_reset_context,
)
from app.bot.analytics import handle_analytics_intent
from app.bot.events import (
    _append_event_details,
    apply_update_from_pending_conflict,
    create_event_from_pending,
    event_reminder_label,
    format_iso_datetime,
    handle_agenda,
    handle_agenda_button,
    handle_create_event,
    handle_event_delete,
    handle_event_lookup,
    handle_event_lookup_direct,
    handle_event_update,
    handle_event_update_by_id,
    infer_update_data_from_text,
    maybe_handle_reminder_command,
    text_refers_to_last_created_event,
    text_requests_meet,
    text_requests_remove_meet,
)
from app.bot.free_slots import (
    explain_last_free_slots as _explain_last_free_slots,
    handle_free_slots as _handle_free_slots,
    handle_more_free_slots as _handle_more_free_slots,
)
from app.bot.habits import (
    handle_habit_button_callback as habit_callback_handler,
    handle_habit_shortcut,
    process_habit_state_message,
)
from app.bot.series import (
    handle_series_button_callback as series_callback_handler,
    handle_series_intent,
    handle_series_shortcut,
    process_series_state_message,
)
from app.schemas.calendar import EventUpdatePayload
SERIES_EXPECTATION_KEYS = (
    "expecting_series_goal",
    "expecting_series_deadline",
    "expecting_series_hours",
    "expecting_series_block_duration",
)

def _series_flow_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get("pending_series_plan"):
        return True
    return any(context.user_data.get(key) for key in SERIES_EXPECTATION_KEYS)

from app.bot.router import create_router
from app.services.gemini import GeminiAnalysisResult

if TYPE_CHECKING:  # pragma: no cover
    from telegram.ext import Application

_intent_router = None


def get_intent_router():
    global _intent_router
    if _intent_router is None:
        _intent_router = create_router()
    return _intent_router

logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    reset_user_context(context)
    user = update.effective_user
    keyboard = ReplyKeyboardMarkup(
        [
            ["📋 Список подій", "🔍 Знайти вільний час"],
            ["➕ Запланувати подію", "🔎 Пошук події"],
            ["📅 Розклад на сьогодні", "📆 Розклад на завтра"],
            ["🧠 Аналітика тижня", "📚 План підготовки"],
            ["🎯 Налаштувати звичку"],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )
    greeting = (
        f"Привіт, {user.first_name or 'друже'}! Я Calendar Assist.\n\n"
        "Скористайся кнопками нижче для швидкого доступу до функцій:"
    )
    await update.message.reply_text(greeting, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    logger.info("Start command від %s", user.id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Доступні команди:\n"
        "/start — коротка довідка\n"
        "/help — цей список\n"
        "/events — показати 5 найближчих подій\n"
        "/window — знайти вільне 'вікно' у календарі\n"
        "/habit — налаштувати звичку\n"
        "/insights — аналітика завантаженості за 7 днів\n"
        "/plan — розкласти підготовку на серію блоків\n"
        "Також працюють запити на кшталт: 'який розклад на завтра', 'коли зустріч з клієнтом', 'знайди 2 години наступного тижня ввечері'."
    )
    await update.message.reply_text(text)


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    telegram_id = update.effective_user.id
    query = update.callback_query
    await query.answer()
    data = (query.data or "").strip()

    if data.startswith("habit_"):
        await habit_callback_handler(update, context)
        return

    if data.startswith("series_"):
        handled = await series_callback_handler(update, context)
        if handled:
            return

    if data == "confirm_delete":
        pending = get_pending_delete(context)
        if not pending:
            await query.edit_message_text("❌ Інформація про подію втрачена. Спробуй знову.")
            return
        
        event_id = pending.event_id
        summary = pending.summary
        
        try:
            await services.calendar.delete_event(telegram_id, event_id)
            await query.edit_message_text(f"✅ Подію \"{summary}\" видалено.")
        except Exception as exc:
            logger.exception("Помилка при видаленні події: %s", exc)
            await query.edit_message_text(f"Не вдалося видалити подію: {exc}")
        
        pop_pending_delete(context)
        return
    
    if data.startswith("delete_"):
        idx = int(data.replace("delete_", ""))
        pending_list = get_pending_delete_list(context)
        
        if idx >= len(pending_list):
            await query.edit_message_text("❌ Вибрана подія не знайдена.")
            return
        
        event_item = pending_list[idx]
        set_pending_delete(
            context,
            PendingDeleteContext(
                event_id=event_item.event_id,
                summary=event_item.summary,
                start=event_item.start,
            ),
        )
        
        buttons = [
            [InlineKeyboardButton("✅ Так, видалити", callback_data="confirm_delete")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="cancel_delete")],
        ]
        
        await query.edit_message_text(
            f"Видалити подію?\n\n📅 {event_item.summary}\n🕒 {event_item.start}",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    
    if data == "cancel_delete":
        await query.edit_message_text("❌ Видалення скасовано.")
        pop_pending_delete(context)
        set_pending_delete_list(context, None)
        return

    if data.startswith("update_"):
        idx = int(data.replace("update_", ""))
        pending_list_context = get_pending_update_list(context)
        pop_pending_update_detail(context)
        
        if not pending_list_context or idx >= len(pending_list_context.items):
            await query.edit_message_text("❌ Вибрана подія не знайдена.")
            return
        
        event_item = pending_list_context.items[idx]
        event_id = event_item.event_id
        summary = event_item.summary

        await query.edit_message_text(f"Оновлюю подію \"{summary}\"…")
        await handle_event_update_by_id(
            update,
            context,
            services,
            telegram_id,
            event_id,
            pending_list_context.update_data,
            "",
        )

        pop_pending_update_list(context)
        return
    
    if data == "cancel_update":
        await query.edit_message_text("❌ Редагування скасовано.")
        pop_pending_update_list(context)
        return

    if data == "conflict_confirm":
        payload = pop_pending_create_conflict(context)
        if payload:
            await query.edit_message_text("Створюю подію…")
            await create_event_from_pending(context, services, telegram_id, payload)
            return
        update_payload = pop_pending_update_conflict(context)
        if update_payload:
            await query.edit_message_text("Оновлюю…")
            await apply_update_from_pending_conflict(context, services, telegram_id, update_payload)
            return
        await query.edit_message_text("❌ Дані про конфлікт не знайдено.")
        return

    if data == "conflict_cancel":
        cancelled = False
        if pop_pending_create_conflict(context):
            cancelled = True
        if pop_pending_update_conflict(context):
            cancelled = True
        await query.edit_message_text("Дію скасовано." if cancelled else "Немає активного конфлікту.")
        return

    if data.startswith("analytics_chart_"):
        from app.bot.analytics import handle_analytics_chart_callback
        await handle_analytics_chart_callback(update, context, services)
        return


async def list_events(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    telegram_id = update.effective_user.id

    try:
        events = await services.calendar.list_upcoming_events(telegram_id, max_results=5)
    except Exception as exc:  # pragma: no cover
        logger.exception("Помилка при отриманні подій: %s", exc)
        await update.message.reply_text("Не вдалося отримати події — повтори запит пізніше.")
        return

    if not events:
        await update.message.reply_text("У календарі немає найближчих запланованих подій.")
        return

    lines = ["Найближчі події:"]
    total = len(events)
    for idx, item in enumerate(events):
        start_str = format_iso_datetime(item.start)
        end_str = format_iso_datetime(item.end)
        summary = item.summary
        link = item.html_link
        if link:
            lines.append(f"• {summary} — {start_str} → {end_str} ({link})")
        else:
            lines.append(f"• {summary} — {start_str} → {end_str}")
        _append_event_details(lines, item)
        if idx < total - 1:
            lines.append("────────────────────")
    await update.message.reply_text("\n".join(lines))

    first_event = events[0]
    summary = first_event.summary
    set_last_event_context(context, first_event.id, summary)
    set_last_event_query(context, summary or "")


async def window_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    telegram_id = update.effective_user.id

    await update.message.reply_text(
        "Опиши, який проміжок потрібен. Наприклад: 'Знайди 2 години між завтра і п'ятницею ввечері'."
    )
    context.user_data["expecting_window_query"] = True


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    message = update.effective_message
    telegram_id = update.effective_user.id
    text = message.text or message.caption or ""
    if not text:
        await message.reply_text("Надішли, будь ласка, текст.")
        return

    lower_text = text.lower()
    
    if should_reset_context(lower_text):
        reset_user_context(context)
        await message.reply_text("Можемо продовжити.")
        return
    
    meet_add_requested = text_requests_meet(text)
    meet_remove_requested = text_requests_remove_meet(text)
    meet_command_detected = meet_add_requested or meet_remove_requested
    if meet_command_detected:
        context.user_data.pop(FREE_SLOT_EXPECTATION_KEY, None)
    
    pending_update_detail = get_pending_update_detail(context)
    if pending_update_detail:
        general_query_keywords = ("що", "шо", "коли", "розклад", "список", "window", "знайди", "шо в", "що в")
        if text.startswith("/") or any(word in lower_text for word in general_query_keywords):
            pop_pending_update_detail(context)
        else:
            if any(word in lower_text for word in ("скасуй", "скасувати", "відміна", "відмінити", "відміни")):
                pop_pending_update_detail(context)
                await message.reply_text("Редагування скасовано.")
                return
            inferred_update = infer_update_data_from_text(text)
            if not inferred_update:
                await message.reply_text(
                    "Не зрозуміло, що змінити. Напиши, наприклад, \"на 16:30\" або \"на 2 години пізніше\"."
                )
                return
            pop_pending_update_detail(context)
            fake_analysis = GeminiAnalysisResult(
                intent="event_update",
                confidence=1.0,
                reply="",
                event=None,
                metadata={
                    "event_query": {"keywords": pending_update_detail.keywords},
                    "event_update": inferred_update,
                },
            )
            await handle_event_update(update, context, services, fake_analysis, text)
            return
    
    if "список подій" in lower_text:
        await list_events(update, context)
        return
    
    if "розклад на сьогодні" in lower_text:
        context.user_data["expecting_agenda"] = "today"
        await handle_agenda_button(update, context, services, "today")
        return
    
    if "розклад на завтра" in lower_text:
        context.user_data["expecting_agenda"] = "tomorrow"
        await handle_agenda_button(update, context, services, "tomorrow")
        return
    
    if "знайти вільний час" in lower_text:
        context.user_data[FREE_SLOT_EXPECTATION_KEY] = True
        await message.reply_text(
            "Вкажи, скільки часу потрібно і в який період.\n"
            "Наприклад: '2 години завтра ввечері' або 'півтори години між завтра і п'ятницею'."
        )
        return
    
    if "запланувати подію" in lower_text:
        context.user_data["expecting_event"] = True
        await message.reply_text(
            "Опиши подію, яку потрібно створити.\n"
            "Наприклад: 'завтра о 19:00 семінар, триває годину' або 'зустріч з Оксаною в п'ятницю о 14:30'."
        )
        return
    
    if "пошук події" in lower_text:
        context.user_data["expecting_search"] = True
        await message.reply_text(
            "Введи назву події або ключові слова для пошуку.\n"
            "Наприклад: 'зустріч по диплому' або 'семінар'."
        )
        return
    
    if "налаштувати звичку" in lower_text:
        handled = await handle_habit_shortcut(update, context, services)
        if handled:
            return
    
    if any(keyword in lower_text for keyword in ("аналітик", "інсайт", "статистик", "продуктивн")):
        await handle_analytics_intent(update, context, services)
        return

    series_flow_active = _series_flow_active(context)

    plan_keywords = ("підгот", "сері", "plan", "time blocking", "глобальн", "іспит", "лабу", "лаб")
    if not series_flow_active and any(keyword in lower_text for keyword in plan_keywords):
        handled = await handle_series_shortcut(update, context, services)
        if handled:
            return

    expecting_window = context.user_data.get(FREE_SLOT_EXPECTATION_KEY, False)
    expecting_event = context.user_data.pop("expecting_event", False)
    expecting_search = context.user_data.pop("expecting_search", False)

    if await process_habit_state_message(update, context, services, text):
        return
    
    if await process_series_state_message(update, context, services, text):
        return
    
    last_slots_state = get_last_free_slots(context)
    if last_slots_state and any(word in lower_text for word in ("чому", "поясни", "чого саме")):
        reply = _explain_last_free_slots(last_slots_state, services.settings)
        await message.reply_text(reply)
        return

    more_keywords = (
        "ще",
        "інші",
        "інших",
        "інше",
        "інший",
        "інший варіант",
        "інший час",
        "далі",
        "більше",
        "пізніше",
        "пізніший",
        "пізніше будь ласка",
    )
    earlier_keywords = (
        "раніше",
        "раніший",
        "раніше будь ласка",
    )
    slot_navigation_blockers = (
        "перенес",
        "перенеси",
        "перенести",
        "зміни",
        "подію",
        "подія",
        "зустріч",
        "семінар",
        "практик",
        "лекці",
    )
    block_slot_navigation = any(word in lower_text for word in slot_navigation_blockers)
    if last_slots_state and not re.search(r"\d", lower_text) and not block_slot_navigation:
        if any(word in lower_text for word in earlier_keywords):
            await _handle_more_free_slots(update, context, services, direction="earlier")
            return
        if any(word in lower_text for word in more_keywords):
            await _handle_more_free_slots(update, context, services, direction="later")
            return

    if expecting_search:
        await handle_event_lookup_direct(update, context, services, text)
        return

    analysis = services.gemini.analyze_user_message(text)
    logger.info("Intent %s (%.2f) для користувача %s", analysis.intent, analysis.confidence, update.effective_user.id)

    refers_to_last_event = text_refers_to_last_created_event(lower_text)
    last_event = get_last_event_context(context)

    reminder_verbs = (
        "нагадай",
        "нагадати",
        "нагаду",
        "прибери нагадування",
        "видали нагадування",
        "скасуй нагадування",
        "без нагадування",
        "нагадування не треба",
        "нагадування не потрібно",
    )
    reminder_context_markers = ("про неї", "туди", "тоді", "сюди", "про це", "цю подію", "до неї", "на неї", "її", "нього")
    is_reminder_with_context = (
        any(word in lower_text for word in reminder_verbs)
        and any(word in lower_text for word in reminder_context_markers)
        and last_event is not None
    )
    
    if (refers_to_last_event or is_reminder_with_context) and last_event:
        inferred_update = infer_update_data_from_text(text)
        if inferred_update:
            event_id = last_event.event_id
            if event_id:
                await handle_event_update_by_id(
                    update, context, services, telegram_id, event_id, inferred_update, text
                )
                return
            fake_analysis = GeminiAnalysisResult(
                intent="event_update",
                confidence=1.0,
                reply="",
                event=None,
                metadata={
                    "event_query": {"keywords": last_event.summary if last_event.summary else ""},
                    "event_update": inferred_update,
                },
            )
            await handle_event_update(update, context, services, fake_analysis, text)
            return

    if await maybe_handle_reminder_command(
        update, context, services, analysis, text, lower_text, telegram_id, expecting_event
    ):
        return

    if expecting_event:
        if analysis.intent != "create_event":
            analysis = GeminiAnalysisResult(
                intent="create_event",
                confidence=analysis.confidence,
                reply=analysis.reply,
                event=analysis.event,
                metadata=analysis.metadata,
            )
    
    if expecting_window:
        if analysis.intent != "find_free_slot":
            analysis = GeminiAnalysisResult(
                intent="find_free_slot",
                confidence=analysis.confidence,
                reply=analysis.reply,
                event=analysis.event,
                metadata=analysis.metadata,
            )

    router = get_intent_router()
    if await router.route(update, context, services, analysis, text):
        return

    if meet_command_detected:
        last_keywords = get_last_event_query(context)
        if last_keywords:
            event_update_payload: dict[str, Any] = {}
            if meet_add_requested:
                event_update_payload["add_meet"] = True
            if meet_remove_requested:
                event_update_payload["remove_meet"] = True
            fake_analysis = GeminiAnalysisResult(
                intent="event_update",
                confidence=0.9,
                reply="",
                event=None,
                metadata={
                    "event_query": {"keywords": last_keywords},
                    "event_update": event_update_payload,
                },
            )
            await handle_event_update(update, context, services, fake_analysis, text)
            return
        else:
            await message.reply_text("Уточни, до якої події додати або прибрати Meet.")
            return

    await message.reply_text(analysis.reply)

