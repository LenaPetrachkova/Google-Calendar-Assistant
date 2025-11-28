from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.bot.context import (
    FREE_SLOT_EXPECTATION_KEY,
    AgendaContext,
    PendingCreateConflict,
    PendingDeleteContext,
    PendingDeleteItem,
    PendingUpdateConflict,
    PendingUpdateDetail,
    PendingUpdateListItem,
    PendingUpdateListContext,
    ServiceContainer,
    get_agenda_context,
    get_last_event_context,
    get_last_event_query,
    get_pending_delete,
    get_pending_delete_list,
    get_pending_update_detail,
    get_pending_update_list,
    pop_pending_delete,
    pop_pending_delete_list,
    pop_pending_update_detail,
    pop_pending_update_list,
    set_agenda_context,
    set_last_event_context,
    set_last_event_query,
    set_pending_create_conflict,
    set_pending_delete,
    set_pending_delete_list,
    set_pending_update_conflict,
    set_pending_update_detail,
    set_pending_update_list,
)
from app.schemas.calendar import CalendarEvent, EventDraft, EventUpdatePayload, RemindersConfig, ReminderOverride
from app.bot.free_slots import (
    build_window_range as _build_window_range,
    pick_slot_from_context as _pick_slot_from_context,
    _detect_range_from_text,
    _detect_window_from_text,
    _extract_custom_time_range,
    _extract_date_range,
    _parse_iso_datetime,
    _resolve_preferred_hours,
)
from app.config.settings import Settings
from app.services.gemini import EventProposal, GeminiAnalysisResult

logger = logging.getLogger(__name__)

CATEGORY_COLOR_MAP = {
    "work": "6",  # тangerine
    "meeting": "2",  # sage
    "study": "9",  # blueberry
    "personal": "5",  # banana
    "health": "10",  # basil
    "sport": "11",  # tomato
    "hobby": "7",  # peacock
    "travel": "4",  # flamingo
    "focus": "1",  # lavender
    "other": "3",  # grape
}

CATEGORY_KEYWORDS = {
    "work": ["робоч", "проєкт", "проекта", "project", "meeting з клієнтом", "звіт"],
    "meeting": ["зустріч", "call", "колл", "колег", "співбесід", "meeting"],
    "study": ["лекці", "пара", "семінар", "курс", "заняття", "вебінар", "стаді", "лаба"],
    "sport": ["спорт", "спортзал", "йога", "фітнес", "біг", "тренув", "зарядка"],
    "health": ["лікар", "стоматолог", "психолог", "мед", "вітаміни"],
    "personal": ["родин", "друзі", "сім'я", "кава", "зустріч з друзями"],
    "hobby": ["читан", "малюван", "музик", "грати", "хобі"],
    "travel": ["подорож", "квиток", "поїздка", "аеропорт", "виліт"],
    "focus": ["фокус", "deep work", "концентрація", "планування"],
}

DEFAULT_REMINDER_MINUTES = 10


class UpdateConflictDetected(Exception):
    def __init__(self, conflict: dict[str, Any]):
        super().__init__("update_conflict_detected")
        self.conflict = conflict


async def handle_create_event(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
    original_text: str,
) -> None:
    telegram_id = update.effective_user.id
    message = update.effective_message
    event = analysis.event

    start_dt, end_dt = _build_event_times(event, services.settings)
    slot_from_context = None
    if not start_dt or not end_dt:
        slot_from_context = _pick_slot_from_context(context, original_text, services.settings)
        if slot_from_context:
            start_dt, end_dt = slot_from_context

    if not start_dt or not end_dt or not event.title:
        await message.reply_text(
            "Не вистачає даних. Вкажи точну дату й час, наприклад: \"12 листопада о 14:30 на 1 годину\"."
        )
        return

    start_payload = {
        "dateTime": start_dt.isoformat(),
        "timeZone": services.settings.timezone,
    }
    end_payload = {
        "dateTime": end_dt.isoformat(),
        "timeZone": services.settings.timezone,
    }

    recurrence_rules = None
    if event.recurrence:
        recurrence_rules = _build_recurrence_rule(event.recurrence)

    attach_meet = _should_attach_meet(event, original_text)
    color_id = _resolve_color_id(event, original_text)
    conference_data = services.calendar.build_conference_data() if attach_meet else None
    reminders_payload = _build_reminders_payload(event, original_text)

    reply_text = analysis.reply
    if slot_from_context:
        time_str = start_dt.strftime("%d.%m %H:%M")
        reply_text = f"{event.title} заплановано на {time_str}."

    try:
        event_draft = EventDraft(
            summary=event.title,
            start=start_payload,
            end=end_payload,
            description=event.notes,
            location=event.location,
            recurrence=recurrence_rules,
            conference_data=conference_data,
            color_id=color_id,
            reminders=reminders_payload,
        )
    except ValueError as exc:
        logger.warning("Некоректні дані події для %s: %s", telegram_id, exc)
        await message.reply_text("Не вдалося розібрати дату чи час. Перевір, будь ласка, запит.")
        return

    conflict = await _detect_conflict(services, telegram_id, start_dt, end_dt)
    if conflict:
        set_pending_create_conflict(
            context,
            PendingCreateConflict(
                draft=event_draft,
                conflict=conflict,
                reply_text=reply_text,
            ),
        )
        await message.reply_text(
            "⚠️ У цей час вже є подія:\n"
            f"• {conflict['summary']} — {conflict['time']}\n"
            "Створити все одно?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ Так, створити", callback_data="conflict_confirm")],
                    [InlineKeyboardButton("❌ Скасувати", callback_data="conflict_cancel")],
                ]
            ),
        )
        return

    await _create_event_with_payload(
        services,
        telegram_id,
        event_draft,
        reply_text,
        context,
        message.reply_text,
    )


async def handle_agenda(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
    original_text: str,
) -> None:
    message = update.effective_message
    telegram_id = update.effective_user.id

    tz = ZoneInfo(services.settings.timezone)
    now = datetime.now(tz)
    agenda_info = analysis.metadata.get("agenda") or {}
    last_agenda = get_agenda_context(context)

    date_value = agenda_info.get("date") or (last_agenda.date if last_agenda else None)
    date_dt = _parse_iso_datetime(date_value, tz) if date_value else None

    if not date_dt:
        start_guess, _ = _detect_range_from_text(original_text, now)
        date_dt = start_guess or (last_agenda.date_dt if last_agenda else None)

    if not date_dt:
        await message.reply_text("Уточни дату, наприклад 'на завтра' або 'на понеділок'.")
        return

    window = (
        agenda_info.get("time_window")
        or _detect_window_from_text(original_text)
        or last_agenda.get("time_window")
        or "full"
    )
    start_dt, end_dt, label = _build_window_range(date_dt, window)

    events = await services.calendar.list_events_between(telegram_id, start_dt, end_dt)
    reply = format_events_list(events, start_dt, end_dt, label=label)
    await message.reply_text(reply)

    if events:
        first_event = events[0]
        set_last_event_context(context, first_event.id, first_event.summary)

    set_agenda_context(
        context,
        AgendaContext(
            date=date_dt.strftime("%Y-%m-%d"),
            date_dt=date_dt,
            time_window=window,
        ),
    )


async def handle_agenda_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    day: str,
) -> None:
    message = update.effective_message
    telegram_id = update.effective_user.id

    tz = ZoneInfo(services.settings.timezone)
    now = datetime.now(tz)

    if day == "today":
        date_dt = now
    else:
        date_dt = now + timedelta(days=1)

    start_dt, end_dt, label = _build_window_range(date_dt, "full")
    events = await services.calendar.list_events_between(telegram_id, start_dt, end_dt)
    reply = format_events_list(events, start_dt, end_dt, label=label)
    await message.reply_text(reply)

    if events:
        first_event = events[0]
        set_last_event_context(context, first_event.id, first_event.summary)

    set_agenda_context(
        context,
        AgendaContext(
            date=date_dt.strftime("%Y-%m-%d"),
            date_dt=date_dt,
            time_window="full",
        ),
    )


async def handle_event_lookup_direct(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    keywords: str,
) -> None:
    message = update.effective_message
    telegram_id = update.effective_user.id

    keywords = keywords.strip()
    if len(keywords) < 2:
        await message.reply_text("Уточни назву події або ключові слова.")
        return

    events, used_query = await _search_events_with_fallback(services, telegram_id, keywords, max_results=10)

    if not events:
        await message.reply_text(f'Подій із ключовими словами "{keywords}" не знайдено.')
        return

    set_last_event_query(context, used_query or keywords)

    lines = ["Знайдені події:"]
    for event in events:
        start_str = format_iso_datetime(event.start)
        end_str = format_iso_datetime(event.end)
        summary = event.summary
        link = event.html_link
        if link:
            lines.append(f"• {summary} — {start_str} → {end_str} ({link})")
        else:
            lines.append(f"• {summary} — {start_str} → {end_str}")
        _append_event_details(lines, event)
    await message.reply_text("\n".join(lines))

    first_event = events[0]
    set_last_event_context(context, first_event.id, first_event.summary)


async def handle_event_lookup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
    original_text: str,
) -> None:
    message = update.effective_message
    telegram_id = update.effective_user.id

    query_info = analysis.metadata.get("event_query") or {} if analysis.metadata else {}
    keywords = (query_info.get("keywords") or "").strip()
    if not keywords:
        keywords = original_text

    if len(keywords) < 2:
        await message.reply_text("Уточни назву події або ключові слова.")
        return

    events, used_query = await _search_events_with_fallback(services, telegram_id, keywords, max_results=10)

    if not events:
        await message.reply_text(f'Подій із ключовими словами "{keywords}" не знайдено.')
        return

    set_last_event_query(context, used_query or keywords)

    lines = ["Знайдені події:"]
    for event in events:
        start_str = format_iso_datetime(event.start)
        end_str = format_iso_datetime(event.end)
        summary = event.summary
        link = event.html_link
        if link:
            lines.append(f"• {summary} — {start_str} → {end_str} ({link})")
        else:
            lines.append(f"• {summary} — {start_str} → {end_str}")
        _append_event_details(lines, event)
    await message.reply_text("\n".join(lines))

    first_event = events[0]
    set_last_event_context(context, first_event.id, first_event.summary)


async def handle_event_delete(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
) -> None:
    message = update.effective_message
    telegram_id = update.effective_user.id

    query_info = analysis.metadata.get("event_query") or {} if analysis.metadata else {}
    keywords = (query_info.get("keywords") or "").strip()
    if not keywords:
        keywords = get_last_event_query(context).strip()

    if not keywords:
        await message.reply_text("Уточни, яку подію потрібно видалити. Наприклад: 'видали семінар'.")
        return

    events, used_query = await _search_events_with_fallback(
        services,
        telegram_id,
        keywords,
        max_results=10,
    )

    if not events:
        await message.reply_text(f'Подій із назвою "{keywords}" не знайдено.')
        return

    set_last_event_query(context, used_query or keywords)

    if len(events) == 1:
        event = events[0]
        event_id = event.id
        summary = event.summary
        start_str = format_iso_datetime(event.start)

        set_pending_delete(
            context,
            PendingDeleteContext(
                event_id=event_id,
                summary=summary,
                start=start_str,
            ),
        )

        buttons = [
            [InlineKeyboardButton("✅ Так, видалити", callback_data="confirm_delete")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="cancel_delete")],
        ]

        await message.reply_text(
            f"Видалити подію?\n\n📅 {summary}\n🕒 {start_str}",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        delete_items = [
            PendingDeleteItem(
                event_id=e.get("id", ""),
                summary=e.get("summary", "(без назви)"),
                start=format_iso_datetime(e.get("start", {})),
            )
            for e in events[:5]
        ]
        set_pending_delete_list(context, delete_items)

        buttons = [
            [
                InlineKeyboardButton(
                    f"{i+1}. {item.summary} ({item.start})",
                    callback_data=f"delete_{i}",
                )
            ]
            for i, item in enumerate(delete_items)
        ]
        buttons.append([InlineKeyboardButton("❌ Скасувати", callback_data="cancel_delete")])

        await message.reply_text(
            f"Знайдено {len(events)} подій із назвою \"{keywords}\".\nОбери, яку видалити:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def handle_event_update_by_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    telegram_id: int,
    event_id: str,
    update_data: EventUpdatePayload | dict[str, Any],
    original_text: str,
) -> None:
    message = update.effective_message

    try:
        event = await services.calendar.get_event(telegram_id, event_id)
    except Exception as exc:
        logger.exception("Не вдалося отримати подію за ID %s: %s", event_id, exc)
        await message.reply_text("Не вдалося знайти цю подію. Можливо, вона була видалена.")
        return

    original_start_dt = _parse_google_datetime(event.start, ZoneInfo(services.settings.timezone))
    if original_start_dt and original_text:
        additional_updates = infer_update_data_from_text(original_text, original_start_dt)
        if additional_updates:
            if isinstance(update_data, dict):
                for key, value in additional_updates.items():
                    update_data.setdefault(key, value)
            elif isinstance(update_data, EventUpdatePayload):
                for key, value in additional_updates.items():
                    if key not in ("add_meet", "remove_meet", "color_id", "reminder_minutes"):
                        update_data.patch.setdefault(key, value)

    plan = EventUpdatePayload.from_dict(update_data)
    sanitized_update = {k: v for k, v in plan.patch.items() if v not in (None, "", [])}
    add_meet_requested = plan.add_meet or bool(sanitized_update.pop("add_meet", False))
    remove_meet_requested = plan.remove_meet or bool(sanitized_update.pop("remove_meet", False))
    if add_meet_requested and remove_meet_requested:
        add_meet_requested = False
    new_category = sanitized_update.pop("category", None)
    color_id = plan.color_id or _color_id_from_category(new_category)
    reminder_minutes = plan.reminder_minutes
    if reminder_minutes is None:
        reminder_minutes = _safe_int_value(sanitized_update.pop("reminder_minutes", None))

    reminders_update = _build_reminders_from_minutes(reminder_minutes)

    try:
        updated_event = await _apply_event_update(
            services,
            telegram_id,
            event_id,
            event,
            sanitized_update,
            add_meet=add_meet_requested,
            remove_meet=remove_meet_requested,
            color_id=color_id,
            reminders=reminders_update,
            clear_reminders=reminder_minutes == 0,
        )

        summary = updated_event.summary or "(без назви)"
        start_str = format_iso_datetime(updated_event.start)
        link = updated_event.html_link or ""

        reply_lines = [
            "✅ Подію оновлено:",
            f"📅 {summary}",
            f"🕒 {start_str}",
        ]
        reminder_label = event_reminder_label(updated_event)
        if reminder_label:
            reply_lines.append(reminder_label)
        if link:
            reply_lines.append(link)
        meet_link = updated_event.hangout_link
        if meet_link:
            reply_lines.append(f"🔗 Google Meet: {meet_link}")

        await message.reply_text("\n".join(reply_lines))
        context.user_data.pop(FREE_SLOT_EXPECTATION_KEY, None)

        set_last_event_context(context, event_id, summary)
        set_last_event_query(context, summary)
    except UpdateConflictDetected as conflict_exc:
        set_pending_update_conflict(
            context,
            PendingUpdateConflict(
                event_id=event_id,
                update=EventUpdatePayload(
                    patch=sanitized_update.copy(),
                    add_meet=add_meet_requested,
                    remove_meet=remove_meet_requested,
                    color_id=color_id,
                    reminder_minutes=reminder_minutes,
                ),
                original_event=event.as_dict(),
                conflict=conflict_exc.conflict,
            ),
        )
        await message.reply_text(
            "⚠️ У цей час вже є подія:\n"
            f"• {conflict_exc.conflict['summary']} — {conflict_exc.conflict['time']}\n"
            "Оновити все одно?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ Так, оновити", callback_data="conflict_confirm")],
                    [InlineKeyboardButton("❌ Скасувати", callback_data="conflict_cancel")],
                ]
            ),
        )
    except Exception as exc:
        logger.exception("Помилка при оновленні події: %s", exc)
        await message.reply_text(f"Не вдалося оновити подію: {exc}")


async def maybe_handle_reminder_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
    original_text: str,
    lower_text: str,
    telegram_id: int,
    expecting_event: bool,
) -> bool:
    if expecting_event:
        return False
    if "нагад" not in lower_text:
        return False

    metadata = analysis.metadata or {}
    query_info = metadata.get("event_query") or {}
    keywords = (query_info.get("keywords") or "").strip()
    last_event = get_last_event_context(context)
    last_query = get_last_event_query(context).strip().lower()
    mentions_last_query = bool(last_query and last_query in lower_text)
    last_summary = (
        (last_event.summary or "").lower() if last_event else ""
    )
    mentions_last_summary = bool(last_summary and last_summary in lower_text)

    if analysis.event and not (keywords or mentions_last_query or mentions_last_summary):
        return False
    reminder_minutes = _parse_reminder_from_text(original_text)
    if reminder_minutes is None:
        return False

    update_payload = {"reminder_minutes": reminder_minutes}
    if text_requests_meet(original_text):
        update_payload["add_meet"] = True
    if text_requests_remove_meet(original_text):
        update_payload["remove_meet"] = True

    if keywords:
        fake_analysis = GeminiAnalysisResult(
            intent="event_update",
            confidence=0.9,
            reply="",
            event=None,
            metadata={
                "event_query": {"keywords": keywords},
                "event_update": update_payload,
            },
        )
        await handle_event_update(update, context, services, fake_analysis, original_text)
        return True

    if last_event and last_event.event_id:
        await handle_event_update_by_id(
            update,
            context,
            services,
            telegram_id,
            last_event.event_id,
            update_payload,
            original_text,
        )
        return True

    if last_event and last_event.summary:
        fake_analysis = GeminiAnalysisResult(
            intent="event_update",
            confidence=0.9,
            reply="",
            event=None,
            metadata={
                "event_query": {"keywords": last_event.summary},
                "event_update": update_payload,
            },
        )
        await handle_event_update(update, context, services, fake_analysis, original_text)
        return True

    await update.effective_message.reply_text(
        "Уточни, для якої події змінити нагадування. Наприклад: 'нагадування для семінару за 15 хв'."
    )
    return True


async def handle_event_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
    original_text: str,
) -> None:
    message = update.effective_message
    telegram_id = update.effective_user.id

    pop_pending_update_detail(context)

    metadata = analysis.metadata or {}
    query_info = metadata.get("event_query") or {}
    keywords = (query_info.get("keywords") or "").strip()
    if not keywords:
        keywords = get_last_event_query(context).strip()

    if not keywords:
        await message.reply_text("Уточни, яку подію потрібно редагувати. Наприклад: 'перенеси семінар на 20:00'.")
        return

    events, used_query = await _search_events_with_fallback(
        services,
        telegram_id,
        keywords,
        max_results=10,
    )

    if not events:
        await message.reply_text(f'Подій із назвою "{keywords}" не знайдено.')
        return

    set_last_event_query(context, used_query or keywords)

    first_event = events[0]
    original_start_dt = None
    if isinstance(first_event, CalendarEvent):
        original_start_dt = _parse_google_datetime(first_event.start, ZoneInfo(services.settings.timezone))
    elif isinstance(first_event, dict):
        original_start_dt = _parse_google_datetime(first_event.get("start", {}), ZoneInfo(services.settings.timezone))

    update_data = dict(metadata.get("event_update") or {})
    inferred = infer_update_data_from_text(original_text, original_start_dt)
    for key, value in inferred.items():
        update_data.setdefault(key, value)

    sanitized_update = {k: v for k, v in update_data.items() if v not in (None, "", [])}
    add_meet_requested = bool(sanitized_update.pop("add_meet", False))
    remove_meet_requested = bool(sanitized_update.pop("remove_meet", False))
    if add_meet_requested and remove_meet_requested:
        add_meet_requested = False
    new_category = sanitized_update.pop("category", None)
    color_id = _color_id_from_category(new_category)
    reminder_minutes = _safe_int_value(sanitized_update.pop("reminder_minutes", None))

    update_plan = EventUpdatePayload(
        patch=sanitized_update.copy(),
        add_meet=add_meet_requested,
        remove_meet=remove_meet_requested,
        color_id=color_id,
        reminder_minutes=reminder_minutes,
    )

    has_updates = update_plan.has_effect()

    if not has_updates:
        set_pending_update_detail(context, PendingUpdateDetail(keywords=keywords))
        await message.reply_text(
            "Уточни, що саме потрібно змінити (наприклад, 'на 16:30', 'на 2 години пізніше', 'зміни тривалість на 45 хв')."
        )
        return

    if len(events) == 1:
        event = events[0]
        event_id = event.id if isinstance(event, CalendarEvent) else event.get("id")

        reminders_update = _build_reminders_from_minutes(update_plan.reminder_minutes)
        try:
            updated_event = await _apply_event_update(
                services,
                telegram_id,
                event_id,
                event,
                update_plan.patch.copy(),
                add_meet=update_plan.add_meet,
                remove_meet=update_plan.remove_meet,
                color_id=update_plan.color_id,
                reminders=reminders_update,
                clear_reminders=update_plan.reminder_minutes == 0,
            )

            summary = updated_event.summary or "(без назви)"
            start_str = format_iso_datetime(updated_event.start)
            link = updated_event.html_link or ""

            reply_lines = [
                "✅ Подію оновлено:",
                f"📅 {summary}",
                f"🕒 {start_str}",
            ]
            reminder_label = event_reminder_label(updated_event)
            if reminder_label:
                reply_lines.append(reminder_label)
            if link:
                reply_lines.append(link)
            meet_link = updated_event.hangout_link
            if meet_link:
                reply_lines.append(f"🔗 Google Meet: {meet_link}")

            await message.reply_text("\n".join(reply_lines))
            context.user_data.pop(FREE_SLOT_EXPECTATION_KEY, None)

            set_last_event_context(context, event_id, summary)
            set_last_event_query(context, summary)
        except UpdateConflictDetected as conflict_exc:
            set_pending_update_conflict(
                context,
                PendingUpdateConflict(
                    event_id=event_id,
                    update=update_plan,
                    original_event=event.as_dict(),
                    conflict=conflict_exc.conflict,
                ),
            )
            await message.reply_text(
                "⚠️ У цей час вже є подія:\n"
                f"• {conflict_exc.conflict['summary']} — {conflict_exc.conflict['time']}\n"
                "Оновити все одно?",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("✅ Так, оновити", callback_data="conflict_confirm")],
                        [InlineKeyboardButton("❌ Скасувати", callback_data="conflict_cancel")],
                    ]
                ),
            )
            return
        except Exception as exc:
            logger.exception("Помилка при оновленні події: %s", exc)
            await message.reply_text(f"Не вдалося оновити подію: {exc}")
    else:
        update_items = []
        for e in events[:5]:
            if isinstance(e, CalendarEvent):
                event_id = e.id
                summary = e.summary or "(без назви)"
                start_dict = e.start
                event_data = e.as_dict()
            else:
                event_id = e.get("id", "")
                summary = e.get("summary", "(без назви)")
                start_dict = e.get("start", {})
                event_data = e
            update_items.append(
                PendingUpdateListItem(
                    event_id=event_id,
                    summary=summary,
                    start=format_iso_datetime(start_dict),
                    event_data=event_data,
                )
            )
        set_pending_update_list(
            context,
            PendingUpdateListContext(items=update_items, update_data=update_plan),
        )

        buttons = [
            [
                InlineKeyboardButton(
                    f"{i+1}. {item.summary} ({item.start})",
                    callback_data=f"update_{i}",
                )
            ]
            for i, item in enumerate(update_items)
        ]
        buttons.append([InlineKeyboardButton("❌ Скасувати", callback_data="cancel_update")])

        await message.reply_text(
            f"Знайдено {len(events)} подій із назвою \"{keywords}\".\nОбери, яку редагувати:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def create_event_from_pending(
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    telegram_id: int,
    payload: PendingCreateConflict | dict[str, Any],
) -> None:
    if isinstance(payload, PendingCreateConflict):
        draft = payload.draft
        reply_text = payload.reply_text
    else:
        data = payload.get("event_payload") or {}
        try:
            draft = EventDraft.from_dict(data)
        except Exception as exc:  # pragma: no cover - legacy payloads
            logger.exception("Не вдалося відновити подію з конфлікту: %s", exc)
            await context.bot.send_message(
                chat_id=telegram_id,
                text="Дані для створення події пошкоджені. Спробуй створити її знову.",
            )
            return
        reply_text = payload.get("analysis_reply") or "Подію створено."

    async def _send(text: str) -> None:
        await context.bot.send_message(chat_id=telegram_id, text=text)

    await _create_event_with_payload(
        services,
        telegram_id,
        draft,
        reply_text,
        context,
        _send,
    )


async def apply_update_from_pending_conflict(
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    telegram_id: int,
    payload: PendingUpdateConflict | dict[str, Any],
) -> None:
    if isinstance(payload, PendingUpdateConflict):
        plan = payload.update
        event_id = payload.event_id
        original_event = payload.original_event
    else:
        plan = EventUpdatePayload.from_dict(payload)
        event_id = payload.get("event_id")
        original_event = payload.get("original_event", {})
    update_data = plan.patch.copy()
    reminders_payload = _build_reminders_from_minutes(plan.reminder_minutes)

    try:
        updated_event = await _apply_event_update(
            services,
            telegram_id,
            event_id,
            original_event,
            update_data,
            add_meet=plan.add_meet,
            remove_meet=plan.remove_meet,
            color_id=plan.color_id,
            reminders=reminders_payload,
            clear_reminders=plan.reminder_minutes == 0,
            ignore_conflicts=True,
        )
    except Exception as exc:  # pragma: no cover
        logger.exception("Не вдалося оновити подію після підтвердження: %s", exc)
        await context.bot.send_message(
            chat_id=telegram_id,
            text="Не вдалося оновити подію. Спробуй ще раз або уточни дані.",
        )
        return

    summary = updated_event.summary or "(без назви)"
    start_str = format_iso_datetime(updated_event.start)
    link = updated_event.html_link or ""

    reminder_label = event_reminder_label(updated_event)
    meet_link = updated_event.hangout_link or ""
    reply = f"✅ Подію оновлено:\n\n📅 {summary}\n🕒 {start_str}"
    if reminder_label:
        reply += f"\n{reminder_label}"
    if link:
        reply += f"\n\n{link}"
    if meet_link:
        reply += f"\n🔗 Google Meet: {meet_link}"
    await context.bot.send_message(chat_id=telegram_id, text=reply)

    set_last_event_context(context, event_id, summary)
    set_last_event_query(context, summary)


def infer_update_data_from_text(text: str, original_start: datetime | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    absolute_time = _parse_absolute_time_from_text(text, original_start)
    if absolute_time:
        result["start"] = absolute_time.isoformat()
    else:
        shift = _parse_time_shift(text)
        if shift:
            result["shift_minutes"] = shift
    
    duration = _parse_duration_minutes(text)
    if duration:
        result["duration_minutes"] = duration
    reminder = _parse_reminder_from_text(text)
    if reminder is not None:
        result["reminder_minutes"] = reminder
    if text_requests_meet(text):
        result["add_meet"] = True
    if text_requests_remove_meet(text):
        result["remove_meet"] = True
    return result


def text_refers_to_last_created_event(text: str) -> bool:
    if not text:
        return False
    context_markers = [
        r"\bпро\s+(неї|це|цю|цю\s+подію|її)",
        r"\bдо\s+(неї|цієї|цієї\s+події)",
        r"\bв\s+(неї|цю|цю\s+подію)",
        r"\bна\s+(неї|це|цю|цю\s+подію)",
        r"\bтуди\s+(ж|же)?",
        r"\bїї\b",
        r"\bцієї\s+події\b",
        r"\bцю\s+подію\b",
        r"\bза\s+неї\b",
    ]
    for pattern in context_markers:
        if re.search(pattern, text):
            return True
    return False


def text_requests_meet(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    keywords = [
        "google meet",
        "гугл міт",
        "міт",
        "meet",
        "онлайн зустріч",
        "онлайн-зустріч",
        "дзвінок",
        "дзвонок",
        "зум",
        "zoom",
        "відеодзвінок",
        "потрібен лінк",
        "потрібне посилання",
        "посилання на зустріч",
    ]
    triggers = ["додай міт", "додай meet", "зроби meet", "міт треба", "meet треба"]
    if any(trigger in lower for trigger in triggers):
        return True
    return any(keyword in lower for keyword in keywords)


def text_requests_remove_meet(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    keywords = [
        "без meet",
        "без міт",
        "прибери meet",
        "прибери міт",
        "видали meet",
        "видали міт",
        "скасуй meet",
        "скасуй міт",
        "відключи meet",
        "без посилання",
    ]
    return any(keyword in lower for keyword in keywords)


def format_events_list(
    events: list[CalendarEvent],
    start: datetime,
    end: datetime,
    *,
    label: str | None = None,
) -> str:
    if not events:
        return "У зазначений період подій не заплановано."
    if label and start.date() == end.date():
        header = f"Події {label} {start:%d.%m.%Y}:"
    else:
        header = f"Події з {start:%d.%m.%Y %H:%M} до {end:%d.%m.%Y %H:%M}:"
    lines = [header]
    total = len(events)
    for idx, event in enumerate(events):
        start_str = format_iso_datetime(event.start)
        end_str = format_iso_datetime(event.end)
        summary = event.summary
        link = event.html_link
        if link:
            lines.append(f"• {summary} — {start_str} → {end_str} ({link})")
        else:
            lines.append(f"• {summary} — {start_str} → {end_str}")
        _append_event_details(lines, event)
        if idx < total - 1:
            lines.append("────────────────────")
    return "\n".join(lines)


def format_iso_datetime(payload: dict[str, Any]) -> str:
    value = payload.get("dateTime") or payload.get("date")
    if not value:
        return "невідомо"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if payload.get("dateTime"):
        return dt.strftime("%d.%m.%Y %H:%M")
    return dt.strftime("%d.%m.%Y")


def event_reminder_label(event: CalendarEvent | dict[str, Any]) -> str:
    if isinstance(event, CalendarEvent):
        config = event.reminders
    else:
        config = _ensure_reminders_config(event.get("reminders"))
    if config is None:
        return ""
    minutes = config.first_override_minutes()
    if config.use_default and minutes is None:
        return f"🔔 Нагадування за замовчуванням ({DEFAULT_REMINDER_MINUTES} хв)"
    if minutes is None:
        return "🔔 Нагадування вимкнено."
    return _reminder_label_from_minutes(minutes)


def _append_event_details(lines: list[str], event: CalendarEvent | dict[str, Any]) -> None:
    reminder_label = event_reminder_label(event)
    if reminder_label:
        lines.append(f"  {reminder_label}")
    if isinstance(event, CalendarEvent):
        meet_link = event.hangout_link
    else:
        meet_link = event.get("hangoutLink")
    if meet_link:
        lines.append(f"  🔗 Google Meet: {meet_link}")


def _parse_duration_minutes(text: str) -> int | None:
    match = re.search(r"(\d+)\s*(хв|хвилин|год|години)", text.lower())
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("год"):
        value *= 60
    return value


def _safe_int_value(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _infer_category_from_text(text: str) -> str | None:
    if not text:
        return None
    lower = text.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in lower:
                return category
    if "зустріч" in lower or "call" in lower:
        return "meeting"
    if "спорт" in lower or "тренув" in lower:
        return "sport"
    if "лекц" in lower or "пара" in lower or "семінар" in lower:
        return "study"
    return None


def _color_id_from_category(category: str | None) -> str | None:
    if not category:
        return None
    return CATEGORY_COLOR_MAP.get(category.lower())


def _resolve_color_id(event: EventProposal | None, text: str) -> str | None:
    category = event.category if event and event.category else None
    inferred_category = category or _infer_category_from_text(text)
    return _color_id_from_category(inferred_category)


def _build_event_times(event: EventProposal, settings: Settings) -> tuple[datetime | None, datetime | None]:
    if not event.date:
        return None, None

    tz = ZoneInfo(settings.timezone)
    try:
        if event.start_time:
            start_dt = datetime.fromisoformat(f"{event.date}T{event.start_time}")
        else:
            start_dt = datetime.fromisoformat(f"{event.date}T09:00")
        start_dt = start_dt.replace(tzinfo=tz)
    except ValueError:
        return None, None

    if event.end_time:
        try:
            end_dt = datetime.fromisoformat(f"{event.date}T{event.end_time}").replace(tzinfo=tz)
        except ValueError:
            return None, None
    else:
        duration = event.duration_minutes or 60
        end_dt = start_dt + timedelta(minutes=duration)

    if end_dt <= start_dt:
        end_dt = start_dt + timedelta(minutes=event.duration_minutes or 60)
    return start_dt, end_dt


def _build_recurrence_rule(recurrence_type: str) -> list[str]:
    if recurrence_type == "daily":
        return ["RRULE:FREQ=DAILY;COUNT=30"]
    if recurrence_type == "weekly":
        return ["RRULE:FREQ=WEEKLY;COUNT=12"]
    if recurrence_type == "monthly":
        return ["RRULE:FREQ=MONTHLY;COUNT=6"]
    return [f"RRULE:{recurrence_type}"]


def _should_attach_meet(event: EventProposal | None, text: str) -> bool:
    if event and event.needs_meet:
        return True
    return text_requests_meet(text)


def _build_reminders_payload(event: EventProposal | None, text: str) -> RemindersConfig | None:
    minutes = event.reminder_minutes if event else None
    if minutes is None:
        minutes = _parse_reminder_from_text(text)
    if minutes is None:
        minutes = DEFAULT_REMINDER_MINUTES
    return RemindersConfig.from_minutes(minutes)


def _build_reminders_from_minutes(minutes: int | None) -> RemindersConfig | None:
    if minutes is None:
        return None
    return RemindersConfig.from_minutes(minutes)


def _parse_reminder_from_text(text: str) -> int | None:
    if not text:
        return None
    lower = text.lower()
    removal_keywords = (
        "без нагад",
        "прибери нагад",
        "видали нагад",
        "скасуй нагад",
        "нагадування не треба",
        "нагадування не потрібно",
    )
    if any(phrase in lower for phrase in removal_keywords):
        return 0
    pattern = r"нагад\w*.*?(?:за|перед)\s*(\d+)\s*(хв|хвилин|год|години)"
    match = re.search(pattern, lower)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("год"):
            value *= 60
        return value
    pattern_short = r"за\s+(\d+)\s*(хв|хвилин|год|години)\s+до"
    match = re.search(pattern_short, lower)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("год"):
            value *= 60
        return value
    if "за годину" in lower:
        return 60
    if "за півгод" in lower:
        return 30
    return None


def _format_reminder_note(reminders: RemindersConfig | dict[str, Any] | list[dict[str, Any]] | None) -> str:
    config = _ensure_reminders_config(reminders)
    if config is None:
        return ""
    minutes = config.first_override_minutes()
    if config.use_default and minutes is None:
        return f"\n🔔 Нагадування за замовчуванням ({DEFAULT_REMINDER_MINUTES} хв)"
    if minutes is None:
        return "\n🔔 Нагадування вимкнено."
    label = _reminder_label_from_minutes(minutes)
    return f"\n{label}" if label else ""


def _reminder_label_from_minutes(minutes: int | None) -> str:
    if minutes is None:
        return ""
    if minutes <= 0:
        return "🔔 Нагадування вимкнено."
    if minutes % 60 == 0 and minutes >= 60:
        hours = minutes // 60
        return f"🔔 Нагадування за {hours} год."
    return f"🔔 Нагадування за {minutes} хв."


def _ensure_reminders_config(
    reminders: RemindersConfig | dict[str, Any] | list[dict[str, Any]] | None
) -> RemindersConfig | None:
    if reminders is None:
        return None
    if isinstance(reminders, RemindersConfig):
        return reminders
    if isinstance(reminders, dict):
        return RemindersConfig.from_api(reminders)
    if isinstance(reminders, list):
        overrides = [ReminderOverride.from_api(item) for item in reminders]
        return RemindersConfig(overrides=overrides, use_default=False)
    return None


def _parse_google_datetime(payload: dict[str, Any], tz: ZoneInfo) -> datetime | None:
    value = payload.get("dateTime") or payload.get("date")
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = datetime.fromisoformat(f"{value}T00:00:00")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


async def _detect_conflict(
    services: ServiceContainer,
    telegram_id: int,
    start_dt: datetime,
    end_dt: datetime,
    exclude_event_id: str | None = None,
) -> dict[str, Any] | None:
    tz = ZoneInfo(services.settings.timezone)
    window_start = start_dt - timedelta(minutes=1)
    window_end = end_dt + timedelta(minutes=1)
    events = await services.calendar.list_events_between(
        telegram_id,
        window_start,
        window_end,
        max_results=20,
    )
    for item in events:
        if item.raw.get("status") == "cancelled":
            continue
        if exclude_event_id and item.id == exclude_event_id:
            continue
        existing_start = _parse_google_datetime(item.start, tz)
        existing_end = _parse_google_datetime(item.end, tz)
        if not existing_start or not existing_end:
            continue
        if start_dt < existing_end and end_dt > existing_start:
            return {
                "summary": item.summary,
                "time": f"{existing_start:%d.%m %H:%M} — {existing_end:%H:%M}",
                "event_id": item.id,
            }
    return None


async def _create_event_with_payload(
    services: ServiceContainer,
    telegram_id: int,
    draft: EventDraft,
    reply_text: str,
    context: ContextTypes.DEFAULT_TYPE,
    send_reply,
) -> None:
    try:
        kwargs = draft.to_calendar_kwargs()
        created = await services.calendar.create_event(telegram_id, **kwargs)
    except Exception as exc:  # pragma: no cover
        logger.exception("Не вдалося створити подію: %s", exc)
        await send_reply("Не вийшло створити подію. Спробуй уточнити дані або повторити пізніше.")
        return

    reply = reply_text or "Подію створено."
    recurrence_rules = kwargs.get("recurrence")
    if recurrence_rules:
        recurrence_text = {
            "daily": "щодня",
            "weekly": "щотижня",
            "monthly": "щомісяця",
        }.get(
            recurrence_rules[0].replace("RRULE:FREQ=", "").split(";")[0].lower(),
            None,
        )
        if recurrence_text:
            reply += f" ({recurrence_text})"
    link = created.html_link
    if link:
        reply += f"\n📅 Подію створено: {link}"
    else:
        reply += "\n📅 Подію створено у твоєму Google Calendar."
    meet_link = created.hangout_link
    if meet_link:
        reply += f"\n🔗 Google Meet: {meet_link}"
    reminder_note = _format_reminder_note(created.reminders)
    if reminder_note:
        reply += reminder_note

    await send_reply(reply)

    set_last_event_query(context, draft.summary)
    set_last_event_context(context, created.id, draft.summary)
    context.user_data.pop(FREE_SLOT_EXPECTATION_KEY, None)


async def _apply_event_update(
    services: ServiceContainer,
    telegram_id: int,
    event_id: str,
    original_event: CalendarEvent | dict[str, Any],
    update_data: dict[str, Any],
    *,
    add_meet: bool = False,
    remove_meet: bool = False,
    color_id: str | None = None,
    reminders: RemindersConfig | None = None,
    clear_reminders: bool = False,
    ignore_conflicts: bool = False,
) -> CalendarEvent:
    tz = ZoneInfo(services.settings.timezone)
    update_kwargs: dict[str, Any] = {}

    def _parse(payload: CalendarEvent | dict[str, Any]) -> tuple[datetime | None, datetime | None]:
        if isinstance(payload, CalendarEvent):
            start_payload = payload.start
            end_payload = payload.end
        else:
            start_payload = payload.get("start", {})
            end_payload = payload.get("end", {})
        start = _parse_google_datetime(start_payload, tz)
        end = _parse_google_datetime(end_payload, tz)
        return start, end

    original_start, original_end = _parse(original_event)
    new_start, new_end = original_start, original_end

    has_absolute_time = False
    if "start" in update_data:
        try:
            parsed_start = datetime.fromisoformat(update_data["start"]).replace(tzinfo=tz)
            now = datetime.now(tz)
            if original_start and parsed_start.date() == now.date() and original_start.date() != now.date():
                new_start = original_start.replace(hour=parsed_start.hour, minute=parsed_start.minute, second=0, microsecond=0)
            else:
                new_start = parsed_start
            has_absolute_time = True
            if original_start and original_end and "end" not in update_data:
                duration = original_end - original_start
                new_end = new_start + duration
        except Exception:
            new_start = original_start
    
    if "end" in update_data:
        try:
            new_end = datetime.fromisoformat(update_data["end"]).replace(tzinfo=tz)
        except Exception:
            new_end = original_end

    shift_minutes = update_data.get("shift_minutes")
    if shift_minutes and original_start and original_end and not has_absolute_time:
        new_start = original_start + timedelta(minutes=shift_minutes)
        new_end = original_end + timedelta(minutes=shift_minutes)

    duration_minutes = update_data.get("duration_minutes")
    if duration_minutes and new_start:
        new_end = new_start + timedelta(minutes=duration_minutes)

    if new_start:
        update_kwargs["start"] = {
            "dateTime": new_start.isoformat(),
            "timeZone": services.settings.timezone,
        }
    if new_end:
        update_kwargs["end"] = {
            "dateTime": new_end.isoformat(),
            "timeZone": services.settings.timezone,
        }

    if "title" in update_data:
        update_kwargs["summary"] = update_data["title"]
    if "description" in update_data:
        update_kwargs["description"] = update_data["description"]
    if "location" in update_data:
        update_kwargs["location"] = update_data["location"]

    if add_meet:
        update_kwargs["conference_data"] = services.calendar.build_conference_data()
    if remove_meet:
        update_kwargs["remove_conference"] = True

    if color_id:
        update_kwargs["color_id"] = color_id

    if reminders is not None:
        update_kwargs["reminders"] = reminders
    if clear_reminders:
        update_kwargs["clear_reminders"] = True

    if (
        not ignore_conflicts
        and new_start
        and new_end
        and (not shift_minutes or shift_minutes != 0)
    ):
        conflict = await _detect_conflict(services, telegram_id, new_start, new_end, exclude_event_id=event_id)
        if conflict:
            raise UpdateConflictDetected(conflict)

    updated_event = await services.calendar.update_event(telegram_id, event_id, **update_kwargs)
    return updated_event


def _parse_absolute_time_from_text(text: str, reference_datetime: datetime | None = None) -> datetime | None:
    if not text:
        return None
    
    lower = text.lower()
    tz = reference_datetime.tzinfo if reference_datetime else ZoneInfo("Europe/Kyiv")
    ref = reference_datetime or datetime.now(tz)
    
    match = re.search(r"на\s+(\d{1,2}):(\d{2})", lower)
    if match:
        try:
            hour = int(match.group(1))
            minute = int(match.group(2))
            hour = hour % 24
            minute = min(59, max(0, minute))
            return ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (ValueError, IndexError):
            pass
    
    match = re.search(r"о\s+(\d{1,2})[:.](\d{2})", lower)
    if match:
        try:
            hour = int(match.group(1))
            minute = int(match.group(2))
            hour = hour % 24
            minute = min(59, max(0, minute))
            return ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (ValueError, IndexError):
            pass
    
    match = re.search(r"на\s+(\d{1,2})(?:\s*(?:вечора|ранку|дня|ночі|год|години))?", lower)
    if match:
        try:
            hour = int(match.group(1))
            minute = 0
            
            if "вечора" in lower or "ночі" in lower:
                if hour < 12:
                    hour += 12
            elif "ранку" in lower or "дня" in lower:
                pass
            elif hour < 8 and hour > 0:
                hour += 12
            
            hour = hour % 24
            return ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (ValueError, IndexError):
            pass
    
    match = re.search(r"о\s+(\d{1,2})(?:[:.](\d{2}))?(?:\s*(?:вечора|ранку|дня|ночі|год))?", lower)
    if match:
        try:
            hour = int(match.group(1))
            minute = 0
            if match.group(2):
                minute = int(match.group(2))
            
            if "вечора" in lower or "ночі" in lower:
                if hour < 12:
                    hour += 12
            
            hour = hour % 24
            minute = min(59, max(0, minute))
            return ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (ValueError, IndexError):
            pass
    
    return None


def _parse_time_shift(text: str) -> int | None:
    lower = text.lower()
    pattern = r"на\s+(?:([\d]+|[а-яіїєґ'’`]+)\s*)?(год|годин|години|годину|хв|хвилин|хвилини|хвилину)\s*(пізніше|пізнише|пізн|позд|раніше|ранише|скоріше|скорше)"
    match = re.search(pattern, lower)
    if not match:
        return None
    amount_token = match.group(1)
    unit_token = match.group(2)
    direction_token = match.group(3)
    amount: float | None = None
    if amount_token:
        if amount_token.isdigit():
            amount = float(amount_token)
        else:
            amount = _word_to_number(amount_token)
    else:
        amount = 1.0
    if amount is None:
        return None
    minutes = amount * 60 if unit_token.startswith("год") else amount
    if direction_token.startswith(("рані", "рани", "скор")):
        minutes = -minutes
    return int(minutes)


def _word_to_number(word: str) -> float | None:
    mapping = {
        "одна": 1,
        "одну": 1,
        "один": 1,
        "пів": 0.5,
        "півгодини": 0.5,
        "півгодину": 0.5,
        "півтори": 1.5,
        "дві": 2,
        "двіє": 2,
        "двох": 2,
        "два": 2,
        "три": 3,
        "чотири": 4,
        "чотирьох": 4,
    }
    return mapping.get(word.strip(" '’`"))


async def _search_events_with_fallback(
    services: ServiceContainer,
    telegram_id: int,
    keywords: str,
    *,
    max_results: int = 10,
) -> tuple[list[CalendarEvent], str]:
    candidates: list[str] = []
    base = (keywords or "").strip()
    if base:
        candidates.append(base)
        normalized = _normalize_keywords(base)
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    else:
        candidates.append("")

    seen: set[str] = set()
    for candidate in candidates:
        normalized_candidate = candidate.strip()
        if len(normalized_candidate) < 2 or normalized_candidate in seen:
            continue
        seen.add(normalized_candidate)
        events = await services.calendar.search_events(
            telegram_id,
            normalized_candidate,
            max_results=max_results,
        )
        if events:
            return events, normalized_candidate

    return [], base


def _normalize_keywords(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"[\"'«»“”]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    suffixes = ("а", "я", "у", "ю", "і", "ї", "е", "о", "и", "ь")
    for suffix in suffixes:
        if cleaned.lower().endswith(suffix) and len(cleaned) > 2:
            return cleaned[:-1]
    return cleaned


__all__ = [
    "handle_create_event",
    "handle_agenda",
    "handle_agenda_button",
    "handle_event_lookup_direct",
    "handle_event_lookup",
    "handle_event_delete",
    "handle_event_update_by_id",
    "handle_event_update",
    "maybe_handle_reminder_command",
    "create_event_from_pending",
    "apply_update_from_pending_conflict",
    "infer_update_data_from_text",
    "text_refers_to_last_created_event",
    "text_requests_meet",
    "text_requests_remove_meet",
    "format_events_list",
    "format_iso_datetime",
    "event_reminder_label",
    "_append_event_details",
]

