from __future__ import annotations

import logging
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from app.bot.context import get_services
from app.services.free_slots import FreeSlotRequest
from app.services.habit_planner import HabitSetup

logger = logging.getLogger(__name__)

(HABIT_NAME, HABIT_FREQUENCY, HABIT_DURATION, HABIT_PREFERRED_TIME) = range(4)


def _habit_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🕐 Фіксований час", callback_data="habit_type_fixed")],
            [InlineKeyboardButton("🔄 Гнучкий розклад", callback_data="habit_type_flexible")],
        ]
    )


def _habit_type_prompt(frequency: int) -> str:
    freq_text = (
        "щоденно" if frequency >= 7 else f"{frequency} разів на тиждень"
        if frequency > 0
        else "регулярно"
    )
    return (
        f"Звичка {freq_text}. Обери формат:\n\n"
        "🕐 Фіксований — усі сесії у той самий час (швидко, одна повторювана подія).\n\n"
        "🔄 Гнучкий — знаходжу вільні вікна під обраний діапазон і покажу попередній перегляд.\n"
        "Можна буде підтвердити або змінити час перед створенням."
    )


async def habit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    services = get_services(context)
    telegram_id = update.effective_user.id

    context.user_data["habit_flow_started"] = True
    await update.message.reply_text(
        "Опиши, яку звичку хочеш впровадити. Наприклад: 'Ранкова йога' або 'Читання книги'."
    )
    return HABIT_NAME


async def habit_set_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["habit_name"] = update.message.text.strip()
    await update.message.reply_text("Скільки разів на тиждень плануєш займатися? Введи число, наприклад 3.")
    return HABIT_FREQUENCY


async def habit_set_frequency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        freq = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Будь ласка, введи число (кількість сесій на тиждень).")
        return HABIT_FREQUENCY
    context.user_data["habit_frequency"] = max(1, min(freq, 14))
    await update.message.reply_text("Яка тривалість однієї сесії у хвилинах? Наприклад, 30.")
    return HABIT_DURATION


async def habit_set_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        duration = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введи тривалість у хвилинах, наприклад 45.")
        return HABIT_DURATION
    context.user_data["habit_duration"] = max(10, min(duration, 240))
    context.user_data["expecting_habit_type"] = True

    freq = context.user_data.get("habit_frequency", 0)
    await update.message.reply_text(
        _habit_type_prompt(freq),
        reply_markup=_habit_type_keyboard(),
    )
    return ConversationHandler.END


async def habit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Налаштування звички скасовано.")
    _clear_habit_context(context)
    return ConversationHandler.END


async def handle_habit_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    services = get_services(context)
    telegram_id = query.from_user.id

    if query.data == "habit_type_fixed":
        context.user_data["habit_use_recurrence"] = True
        context.user_data.pop("expecting_habit_type", None)
        context.user_data["expecting_fixed_time"] = True
        await query.edit_message_text(
            "О котрій годині хочеш займатися?\n"
            "Введи час у форматі HH:MM (наприклад, 07:00 або 19:30)"
        )
        return

    if query.data == "habit_type_flexible":
        context.user_data["habit_use_recurrence"] = False
        context.user_data.pop("expecting_habit_type", None)
        context.user_data["expecting_habit_timeofday"] = True

        buttons = [
            [InlineKeyboardButton("Ранок (6:00-12:00)", callback_data="habit_tod_morning")],
            [InlineKeyboardButton("День (12:00-18:00)", callback_data="habit_tod_day")],
            [InlineKeyboardButton("Вечір (18:00-22:00)", callback_data="habit_tod_evening")],
            [InlineKeyboardButton("Ніч (22:00-6:00)", callback_data="habit_tod_night")],
        ]
        await query.edit_message_text(
            "Який час доби найкращий?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if query.data.startswith("habit_tod_"):
        time_of_day = query.data.replace("habit_tod_", "")
        context.user_data["habit_time_of_day"] = time_of_day
        context.user_data.pop("expecting_habit_timeofday", None)
        context.user_data["expecting_habit_timerange"] = True

        time_ranges = {
            "morning": [
                ("06:00-08:00", "habit_range_06-08"),
                ("08:00-10:00", "habit_range_08-10"),
                ("10:00-12:00", "habit_range_10-12"),
                ("Будь-який ранковий", "habit_range_morning_any"),
            ],
            "day": [
                ("12:00-14:00", "habit_range_12-14"),
                ("14:00-16:00", "habit_range_14-16"),
                ("16:00-18:00", "habit_range_16-18"),
                ("Будь-який денний", "habit_range_day_any"),
            ],
            "evening": [
                ("18:00-19:00", "habit_range_18-19"),
                ("19:00-20:00", "habit_range_19-20"),
                ("20:00-21:00", "habit_range_20-21"),
                ("21:00-22:00", "habit_range_21-22"),
                ("Будь-який вечірній", "habit_range_evening_any"),
            ],
            "night": [
                ("22:00-00:00", "habit_range_22-24"),
                ("00:00-02:00", "habit_range_00-02"),
                ("02:00-06:00", "habit_range_02-06"),
                ("Будь-який нічний", "habit_range_night_any"),
            ],
        }

        buttons = [[InlineKeyboardButton(label, callback_data=cb)] for label, cb in time_ranges.get(time_of_day, [])]

        await query.edit_message_text(
            "Обери зручний діапазон часу або введи свій у форматі HH:MM-HH:MM (наприклад, 19:00-20:00):",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if query.data.startswith("habit_range_"):
        range_str = query.data.replace("habit_range_", "")
        context.user_data["habit_time_range"] = range_str
        context.user_data.pop("expecting_habit_timerange", None)

        await query.edit_message_text("🔍 Шукаю вільні вікна...")
        await _show_habit_preview(
            context,
            services,
            telegram_id,
            lambda text, markup: query.edit_message_text(text, reply_markup=markup),
        )
        return

    if query.data == "habit_confirm":
        context.user_data.pop("expecting_habit_confirmation", None)
        await query.edit_message_text("⏳ Створюю події в календарі...")

        selected_slots = context.user_data.get("habit_selected_slots")
        if selected_slots:
            try:
                habit_name = context.user_data.get("habit_name", "Звичка")
                created_count = 0
                created_summary: list[str] = []
                for start_str, end_str in selected_slots:
                    start_dt = datetime.fromisoformat(start_str)
                    end_dt = datetime.fromisoformat(end_str)
                    created_event = await services.calendar.create_event(
                        telegram_id,
                        summary=habit_name,
                        start={"dateTime": start_dt.isoformat(), "timeZone": services.settings.timezone},
                        end={"dateTime": end_dt.isoformat(), "timeZone": services.settings.timezone},
                        description="Сесія звички",
                    )
                    created_count += 1
                    link = created_event.html_link
                    created_summary.append(
                        f"• {start_dt:%a %d.%m %H:%M} → {end_dt:%H:%M}"
                        + (f" ({link})" if link else "")
                    )
                summary_text = [
                    f"✅ Створено {created_count} сесій звички \"{habit_name}\".",
                    "Події додано у Google Calendar:",
                    *created_summary,
                ]
                await query.edit_message_text("\n".join(summary_text))
            except Exception as exc:  # pragma: no cover
                logger.exception("Помилка при створенні звички: %s", exc)
                await query.edit_message_text(f"Не вдалося створити звичку: {exc}")
        else:
            setup = HabitSetup(
                name=context.user_data.get("habit_name", "Звичка"),
                duration_minutes=context.user_data.get("habit_duration", 30),
                preferred_time_of_day=context.user_data.get("habit_preference"),
                target_sessions_per_week=context.user_data.get("habit_frequency", 3),
                use_recurrence=context.user_data.get("habit_use_recurrence", False),
                fixed_time=context.user_data.get("habit_fixed_time"),
            )
            try:
                summary = await services.habit_planner.setup_habit(telegram_id, setup)
                await query.edit_message_text(summary)
            except Exception as exc:  # pragma: no cover
                logger.exception("Помилка при створенні звички: %s", exc)
                await query.edit_message_text(f"Не вдалося створити звичку: {exc}")

        _clear_habit_context(context)
        return

    if query.data == "habit_change_time":
        context.user_data["expecting_habit_timeofday"] = True
        context.user_data.pop("habit_time_range", None)
        buttons = [
            [InlineKeyboardButton("Ранок (6:00-12:00)", callback_data="habit_tod_morning")],
            [InlineKeyboardButton("День (12:00-18:00)", callback_data="habit_tod_day")],
            [InlineKeyboardButton("Вечір (18:00-22:00)", callback_data="habit_tod_evening")],
            [InlineKeyboardButton("Ніч (22:00-6:00)", callback_data="habit_tod_night")],
        ]
        await query.edit_message_text(
            "Обери інший час доби:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if query.data == "habit_cancel":
        await query.edit_message_text("Налаштування звички скасовано.")
        _clear_habit_context(context)
        return


async def handle_habit_shortcut(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
) -> bool:
    telegram_id = update.effective_user.id

    context.user_data["expecting_habit_name"] = True
    await update.effective_message.reply_text(
        "Опиши, яку звичку хочеш впровадити. Наприклад: 'Ранкова йога' або 'Читання книги'."
    )
    return True


async def process_habit_state_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    text: str,
) -> bool:
    message = update.effective_message
    telegram_id = update.effective_user.id
    stripped = (text or "").strip()

    if context.user_data.pop("expecting_fixed_time", False):
        time_match = re.match(r"^(\d{1,2}):(\d{2})$", stripped)
        if not time_match:
            context.user_data["expecting_fixed_time"] = True
            await message.reply_text(
                "Неправильний формат. Введи час у форматі HH:MM\nНаприклад: 07:00 або 19:30"
            )
            return True

        hour, minute = int(time_match.group(1)), int(time_match.group(2))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            context.user_data["expecting_fixed_time"] = True
            await message.reply_text(
                "Неправильний час. Години: 0-23, хвилини: 0-59\nНаприклад: 07:00 або 19:30"
            )
            return True

        context.user_data["habit_fixed_time"] = f"{hour:02d}:{minute:02d}"
        setup = HabitSetup(
            name=context.user_data.get("habit_name", "Звичка"),
            duration_minutes=context.user_data.get("habit_duration", 30),
            preferred_time_of_day=None,
            target_sessions_per_week=context.user_data.get("habit_frequency", 7),
            use_recurrence=True,
            fixed_time=context.user_data["habit_fixed_time"],
        )
        try:
            summary = await services.habit_planner.setup_habit(telegram_id, setup)
            await message.reply_text(summary)
        except Exception as exc:  # pragma: no cover
            logger.exception("Помилка при створенні звички: %s", exc)
            await message.reply_text(f"Не вдалося створити звичку: {exc}")
        _clear_habit_context(context)
        return True

    if context.user_data.get("expecting_habit_timerange"):
        manual_match = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*$", stripped)
        if not manual_match:
            await message.reply_text("Введи діапазон у форматі HH:MM-HH:MM, наприклад 19:00-20:00.")
            return True
        start_hour = int(manual_match.group(1))
        end_hour = int(manual_match.group(3))
        if not (0 <= start_hour < 24 and 0 < end_hour <= 24 and end_hour > start_hour):
            await message.reply_text("Діапазон має бути в межах 00-24 і кінець повинен бути пізніше початку.")
            return True
        context.user_data["habit_time_range"] = f"{start_hour:02d}-{end_hour:02d}"
        context.user_data.pop("expecting_habit_timerange", None)
        await message.reply_text("🔍 Шукаю вільні вікна...")
        await _show_habit_preview(
            context,
            services,
            telegram_id,
            lambda text, markup: message.reply_text(text, reply_markup=markup),
        )
        return True

    if context.user_data.pop("expecting_habit_name", False):
        context.user_data["habit_name"] = stripped
        context.user_data["expecting_habit_frequency"] = True
        await message.reply_text("Скільки разів на тиждень плануєш займатися? Введи число, наприклад 3.")
        return True

    if context.user_data.pop("expecting_habit_frequency", False):
        try:
            freq = int(stripped)
            context.user_data["habit_frequency"] = max(1, min(freq, 14))
            context.user_data["expecting_habit_duration"] = True
            await message.reply_text("Яка тривалість однієї сесії у хвилинах? Наприклад, 30.")
        except ValueError:
            context.user_data["expecting_habit_frequency"] = True
            await message.reply_text("Будь ласка, введи число (кількість сесій на тиждень).")
        return True

    if context.user_data.pop("expecting_habit_duration", False):
        try:
            duration = int(stripped)
            context.user_data["habit_duration"] = max(10, min(duration, 240))
            freq = context.user_data.get("habit_frequency", 0)
            context.user_data["expecting_habit_type"] = True
            await message.reply_text(
                _habit_type_prompt(freq),
                reply_markup=_habit_type_keyboard(),
            )
        except ValueError:
            context.user_data["expecting_habit_duration"] = True
            await message.reply_text("Введи тривалість у хвилинах, наприклад 45.")
        return True

    return False


async def _generate_habit_preview(
    telegram_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    services,
) -> str:
    habit_name = context.user_data.get("habit_name", "Звичка")
    duration = context.user_data.get("habit_duration", 30)
    frequency = context.user_data.get("habit_frequency", 3)
    time_range = context.user_data.get("habit_time_range", "")

    start_hour, end_hour = _parse_time_range(time_range)

    tz = ZoneInfo(services.settings.timezone)
    now = datetime.now(tz)
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)

    request = FreeSlotRequest(
        telegram_id=telegram_id,
        duration_minutes=duration,
        date_from=week_start,
        date_to=week_end,
        preferred_start=start_hour,
        preferred_end=end_hour,
    )

    slots = await services.free_slot_service.find_slots(request, max_suggestions=frequency * 2)
    if not slots:
        raise RuntimeError("Не знайдено вільних вікон у вказаному діапазоні. Спробуй інший час.")

    selected_slots = slots[:frequency]
    context.user_data["habit_selected_slots"] = [
        (slot.start.isoformat(), slot.end.isoformat()) for slot in selected_slots
    ]

    tz_label = services.settings.timezone
    lines = [f"📋 Попередній розклад для \"{habit_name}\":\n"]
    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
    for slot in selected_slots:
        weekday = weekdays[slot.start.weekday()]
        date_str = slot.start.strftime("%d.%m")
        start_str = slot.start.strftime("%H:%M")
        end_str = slot.end.strftime("%H:%M")
        lines.append(f"• {weekday} {date_str} — {start_str} → {end_str} ({tz_label})")

    lines.append(f"\n⏱️ Тривалість: {duration} хв")
    lines.append(f"🔁 Частота: {frequency} разів/тиждень")
    lines.append("Можеш підтвердити, змінити діапазон або скасувати нижче.")
    return "\n".join(lines)


async def _show_habit_preview(
    context: ContextTypes.DEFAULT_TYPE,
    services,
    telegram_id: int,
    send_func,
) -> None:
    try:
        preview_text = await _generate_habit_preview(telegram_id, context, services)
        buttons = [
            [InlineKeyboardButton("✅ Підтвердити", callback_data="habit_confirm")],
            [InlineKeyboardButton("🔄 Змінити час", callback_data="habit_change_time")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="habit_cancel")],
        ]
        await send_func(preview_text, InlineKeyboardMarkup(buttons))
        context.user_data["expecting_habit_confirmation"] = True
    except Exception as exc:  # pragma: no cover
        logger.exception("Помилка при пошуку слотів звички: %s", exc)
        await send_func(
            "Не вдалося знайти вільні вікна. Спробуй інший час доби, інший діапазон або меншу кількість сесій.",
            None,
        )


def _parse_time_range(range_str: str) -> tuple[int, int]:
    if "-" in range_str:
        start_str, end_str = range_str.split("-")
        try:
            return int(start_str), int(end_str)
        except ValueError:
            pass
    mapping = {
        "morning_any": (6, 12),
        "day_any": (12, 18),
        "evening_any": (18, 22),
        "night_any": (22, 24),
    }
    return mapping.get(range_str, (6, 22))


def _clear_habit_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in [
        "habit_name",
        "habit_frequency",
        "habit_duration",
        "habit_preference",
        "habit_use_recurrence",
        "habit_fixed_time",
        "habit_time_of_day",
        "habit_time_range",
        "habit_selected_slots",
        "expecting_habit_name",
        "expecting_habit_frequency",
        "expecting_habit_duration",
        "expecting_habit_time",
        "expecting_habit_type",
        "pending_habit",
        "expecting_habit_timeofday",
        "expecting_habit_timerange",
        "expecting_habit_confirmation",
        "habit_flow_started",
    ]:
        context.user_data.pop(key, None)


__all__ = [
    "HABIT_NAME",
    "HABIT_FREQUENCY",
    "HABIT_DURATION",
    "habit_start",
    "habit_set_name",
    "habit_set_frequency",
    "habit_set_duration",
    "habit_cancel",
    "handle_habit_button_callback",
    "handle_habit_shortcut",
    "process_habit_state_message",
]

