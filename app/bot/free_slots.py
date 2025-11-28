from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import re
from typing import Any
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from app.bot.context import (
    FREE_SLOT_EXPECTATION_KEY,
    LastFreeSlotsContext,
    LastFreeSlotsRequest,
    get_last_free_slots,
    set_last_free_slots,
)
from app.services.free_slots import FreeSlot, FreeSlotRequest, FreeSlotService
from app.services.gemini import GeminiAnalysisResult


async def handle_free_slots(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    analysis: GeminiAnalysisResult,
    original_text: str,
) -> None:
    message = update.effective_message
    telegram_id = update.effective_user.id

    tz = ZoneInfo(services.settings.timezone)
    now = datetime.now(tz)
    metadata = dict(analysis.metadata or {})

    pending_slot = context.user_data.get("pending_free_slot") or {}
    pending_request = pending_slot.get("request") or {}
    last_context = get_last_free_slots(context)
    last_request_saved = last_context.request.as_dict() if last_context else {}
    fallback_request = pending_request or last_request_saved

    duration = _extract_duration_minutes(metadata, original_text)
    if not duration:
        duration = pending_request.get("duration")
    if not duration:
        temp_date_from, temp_date_to = _extract_date_range(metadata, original_text, now, None)
        temp_window = metadata.get("preferred_window") or _detect_window_from_text(original_text)
        temp_explicit = _extract_custom_time_range(original_text, 60)
        pref_start, pref_end = _resolve_preferred_hours(temp_explicit, temp_window, None, None)
        context.user_data["pending_free_slot"] = {
            "request": {
                "date_from": temp_date_from.isoformat(),
                "date_to": temp_date_to.isoformat(),
                "preferred_window": temp_window,
                "preferred_start": pref_start,
                "preferred_end": pref_end,
            }
        }
        context.user_data[FREE_SLOT_EXPECTATION_KEY] = True
        await message.reply_text("Вкажи тривалість. Наприклад: 'півтори години' або '45 хв'.")
        return
    duration = int(duration)

    date_from, date_to = _extract_date_range(metadata, original_text, now, fallback_request)

    explicit_range = _extract_custom_time_range(original_text, duration)
    preferred_window = (
        metadata.get("preferred_window")
        or _detect_window_from_text(original_text)
        or fallback_request.get("preferred_window")
    )
    preferred_start = fallback_request.get("preferred_start")
    preferred_end = fallback_request.get("preferred_end")
    preferred_start, preferred_end = _resolve_preferred_hours(
        explicit_range, preferred_window, preferred_start, preferred_end
    )

    request = FreeSlotRequest(
        telegram_id=telegram_id,
        duration_minutes=duration,
        date_from=date_from,
        date_to=date_to,
        preferred_start=preferred_start,
        preferred_end=preferred_end,
    )

    slots = await services.free_slot_service.find_slots(request)
    reply = FreeSlotService.format_slots(slots)
    await message.reply_text(reply)

    next_start = slots[-1].end + timedelta(minutes=15) if slots else date_from
    request_state = LastFreeSlotsRequest(
        duration=duration,
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        preferred_window=preferred_window,
        preferred_start=preferred_start,
        preferred_end=preferred_end,
        next_start=next_start.isoformat(),
        cursor_history=[date_from.isoformat()],
    )
    set_last_free_slots(
        context,
        LastFreeSlotsContext(
            slots=[slot for slot in slots],
            request=request_state,
            awaiting_use=bool(slots),
        ),
    )
    context.user_data.pop("pending_free_slot", None)
    context.user_data.pop(FREE_SLOT_EXPECTATION_KEY, None)


async def handle_more_free_slots(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services,
    direction: str = "later",
) -> None:
    message = update.effective_message
    info = get_last_free_slots(context)
    if not info:
        await message.reply_text("Спочатку попроси мене знайти вільний час через /window.")
        return

    req = info.request
    last_shown_slots = info.slots or []
    duration = req.duration
    if not duration:
        await message.reply_text("Спочатку попроси мене знайти вільний час через /window.")
        return

    tz = ZoneInfo(services.settings.timezone)
    original_date_from = _parse_iso_datetime(req.date_from, tz) or datetime.now(tz)
    original_date_to = _parse_iso_datetime(req.date_to, tz) or (original_date_from + timedelta(days=7))
    history_raw = req.cursor_history or [req.date_from]
    history = [value for value in history_raw if value]

    def _date_or_default(value: str | None, default: datetime) -> datetime:
        parsed = _parse_iso_datetime(value, tz) if value else None
        return parsed or default

    history_next = history

    if direction == "earlier":
        if len(history) <= 1:
            if last_shown_slots:
                first_slot = last_shown_slots[0]
                date_from = original_date_from
                date_to = first_slot.start - timedelta(minutes=15)
                if date_from >= date_to:
                    await message.reply_text("Раніше у цьому проміжку вільних вікон немає.")
                    return
            else:
                await message.reply_text("Раніше у цьому проміжку вільних вікон немає.")
                return
        else:
            current_start = _date_or_default(history[-1], original_date_from)
            previous_start = _date_or_default(history[-2], original_date_from)
            date_from = previous_start
            date_to = current_start - timedelta(minutes=15)
            history_next = history[:-1]
            if date_from >= date_to:
                await message.reply_text("Раніше у цьому проміжку вільних вікон немає.")
                return
    else:
        date_from = _parse_iso_datetime(req.next_start or req.date_from, tz) or datetime.now(tz)
        date_to = original_date_to
        history_next = history + [date_from.isoformat()]
        if date_from >= date_to:
            await message.reply_text("Інших вільних вікон у цьому проміжку вже немає.")
            return

    request = FreeSlotRequest(
        telegram_id=update.effective_user.id,
        duration_minutes=int(duration),
        date_from=date_from,
        date_to=date_to,
        preferred_start=req.preferred_start,
        preferred_end=req.preferred_end,
    )

    slots = await services.free_slot_service.find_slots(request)
    if not slots:
        if direction == "earlier":
            await message.reply_text("Раніше у цьому проміжку вільних вікон немає.")
        else:
            updated_request = replace(req, next_start=date_to.isoformat())
            set_last_free_slots(
                context,
                LastFreeSlotsContext(
                    slots=info.slots,
                    request=updated_request,
                    awaiting_use=info.awaiting_use,
                ),
            )
            await message.reply_text("Інших варіантів у цьому проміжку не знайдено.")
        return

    reply = FreeSlotService.format_slots(slots)
    await message.reply_text(reply)

    next_start = slots[-1].end + timedelta(minutes=15)
    updated_request = LastFreeSlotsRequest(
        duration=int(duration),
        date_from=req.date_from or original_date_from.isoformat(),
        date_to=req.date_to or original_date_to.isoformat(),
        preferred_window=req.preferred_window,
        preferred_start=req.preferred_start,
        preferred_end=req.preferred_end,
        next_start=next_start.isoformat(),
        cursor_history=history_next,
    )
    set_last_free_slots(
        context,
        LastFreeSlotsContext(
            slots=[slot for slot in slots],
            request=updated_request,
            awaiting_use=bool(slots),
        ),
    )


def pick_slot_from_context(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    settings,
) -> tuple[datetime, datetime] | None:
    info = get_last_free_slots(context)
    if not info:
        return None
    slots: list[FreeSlot | str] = info.slots or []
    if not slots:
        return None
    awaiting_use = info.awaiting_use
    if not text_refers_to_last_slot(text) and not awaiting_use:
        return None

    slot = slots[0]
    if not isinstance(slot, FreeSlot):
        return None
    start = slot.start
    end = slot.end
    tz = ZoneInfo(settings.timezone)
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    if end.tzinfo is None:
        end = end.replace(tzinfo=tz)

    remaining = slots[1:]
    info.slots = remaining
    info.awaiting_use = bool(remaining)
    set_last_free_slots(context, info)
    context.user_data.pop(FREE_SLOT_EXPECTATION_KEY, None)
    return start, end


def text_refers_to_last_slot(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    keywords = [
        "туди",
        "сюди",
        "туда",
        "туди ж",
        "цей час",
        "в цей час",
        "у цей час",
        "це вікно",
        "цей варіант",
        "перший варіант",
        "знайдений час",
        "знайдене вікно",
        "на цей час",
        "тоді",
        "той час",
    ]
    return any(keyword in lower for keyword in keywords)


def explain_last_free_slots(info: LastFreeSlotsContext | None, settings) -> str:
    tz = ZoneInfo(settings.timezone)
    if info is None or not info.slots:
        return "Вільних вікон у попередньому проміжку не виявлено: календар зайнятий."

    request = info.request
    duration = request.duration
    date_from = _parse_iso_datetime(request.date_from, tz)
    date_to = _parse_iso_datetime(request.date_to, tz)
    preferred_window = request.preferred_window

    range_text = ""
    if date_from and date_to:
        range_text = f"з {date_from:%d.%m.%Y} по {date_to:%d.%m.%Y}"

    window_text = "протягом дня"
    if preferred_window:
        window_map = {
            "morning": "зранку (06:00-12:00)",
            "day": "вдень (12:00-18:00)",
            "evening": "увечері (18:00-22:00)",
            "night": "у вечірньо-нічному проміжку",
        }
        window_text = window_map.get(preferred_window, "протягом дня")

    slot_lines = []
    for slot in info.slots:
        if isinstance(slot, FreeSlot):
            slot_lines.append(f"• {slot.start:%d.%m %H:%M} — {slot.end:%H:%M}")
        elif isinstance(slot, str):
            slot_lines.append(slot)

    reply = [
        f"Пошук виконувався на {duration} хв {range_text}",
        f"з пріоритетом {window_text}.",
        "Вільні вікна:",
    ]
    reply.extend(slot_lines)
    reply.append("Інші часові відрізки виявились зайнятими у календарі.")
    return "\n".join(reply)


def build_window_range(date_dt: datetime, window: str) -> tuple[datetime, datetime, str]:
    tz = date_dt.tzinfo or ZoneInfo("UTC")
    day_start = date_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    mapping = {
        "morning": (day_start.replace(hour=6), day_start.replace(hour=12), "зранку"),
        "day": (day_start.replace(hour=12), day_start.replace(hour=18), "вдень"),
        "evening": (day_start.replace(hour=18), day_start.replace(hour=22), "увечері"),
        "night": (day_start.replace(hour=22), day_start.replace(hour=23, minute=59), "у вечірньо-нічному проміжку"),
    }
    start_dt, end_dt, label = mapping.get(window, (day_start, day_start.replace(hour=23, minute=59), "протягом дня"))
    return start_dt.astimezone(tz), end_dt.astimezone(tz), label


def _extract_date_range(
    metadata: dict[str, Any],
    text: str,
    now: datetime,
    last_request: dict[str, Any] | None = None,
) -> tuple[datetime, datetime]:
    tz = now.tzinfo or ZoneInfo("UTC")
    start = _parse_iso_datetime(metadata.get("date_from"), tz)
    end = _parse_iso_datetime(metadata.get("date_to"), tz)

    if not start and last_request:
        start = _parse_iso_datetime(last_request.get("date_from"), tz)
    if not end and last_request:
        end = _parse_iso_datetime(last_request.get("date_to"), tz)

    inferred_start, inferred_end = _detect_range_from_text(text, now)

    start = start or inferred_start or now
    if not end:
        if inferred_end:
            end = inferred_end
        elif inferred_start:
            end = inferred_start + timedelta(days=1)
        else:
            end = now + timedelta(days=7)

    if end <= start:
        end = start + timedelta(days=1)
    return start, end


def _parse_iso_datetime(value: Any, tz: ZoneInfo) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    if isinstance(value, str):
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
    return None


def _detect_range_from_text(text: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    lower = text.lower()
    tz = now.tzinfo or ZoneInfo("UTC")

    def day_start(dt: datetime) -> datetime:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)

    def day_end(dt: datetime) -> datetime:
        return dt.replace(hour=23, minute=59, second=0, microsecond=0)

    start = end = None
    if "сьогодні" in lower:
        start = day_start(now)
        end = day_end(now)
    if "завтра" in lower:
        tomorrow = now + timedelta(days=1)
        start = day_start(tomorrow)
        end = day_end(tomorrow)
    if "післязавтра" in lower:
        day = now + timedelta(days=2)
        start = day_start(day)
        end = day_end(day)

    weekdays = {
        "понеділ": 0,
        "вівтор": 1,
        "серед": 2,
        "четвер": 3,
        "п'ятн": 4,
        "пятн": 4,
        "субот": 5,
        "неділ": 6,
    }
    for word, target in weekdays.items():
        if word in lower:
            day = _next_weekday(now, target)
            if not start:
                start = day_start(day)
            end = day_end(day)

    return start, end


def _next_weekday(now: datetime, target: int) -> datetime:
    days_ahead = target - now.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return now + timedelta(days=days_ahead)


def _extract_custom_time_range(text: str, duration_minutes: int) -> tuple[int, int] | None:
    lower = text.lower()
    match = re.search(r"з\s*(\d{1,2})(?::(\d{2}))?\s*(?:до|по)\s*(\d{1,2})", lower)
    if match:
        start_hour = int(match.group(1))
        end_hour = int(match.group(3))
        if 0 <= start_hour < 24 and 0 <= end_hour <= 24 and end_hour > start_hour:
            return start_hour, end_hour
    match = re.search(r"з\s*(\d{1,2})(?::(\d{2}))?", lower)
    if match:
        start_hour = int(match.group(1))
        if 0 <= start_hour < 24:
            hours = max(1, (duration_minutes + 59) // 60)
            end_hour = min(24, start_hour + hours)
            return start_hour, end_hour
    return None


def _resolve_preferred_hours(
    explicit_range: tuple[int, int] | None,
    preferred_window: str | None,
    fallback_start: int | None,
    fallback_end: int | None,
) -> tuple[int | None, int | None]:
    if explicit_range:
        return explicit_range
    window_map = {
        "morning": (6, 12),
        "day": (12, 18),
        "evening": (18, 22),
        "night": (21, 24),
    }
    if preferred_window in window_map:
        return window_map[preferred_window]
    return fallback_start, fallback_end


def _extract_duration_minutes(metadata: dict[str, Any], text: str) -> int | None:
    duration = metadata.get("duration_minutes")
    if duration:
        try:
            return int(duration)
        except (TypeError, ValueError):
            pass

    lower = text.lower()
    if "півтори" in lower or "полтора" in lower:
        return 90
    match = re.search(r"(\d+)(?:\s*год|\s*h)", lower)
    if match:
        return int(match.group(1)) * 60
    match = re.search(r"(\d+)(?:\s*хв|\s*min)", lower)
    if match:
        return int(match.group(1))
    word_map = {"одну": 60, "один": 60, "дві": 120, "две": 120, "три": 180}
    for word, minutes in word_map.items():
        if word in lower and "год" in lower:
            return minutes
    return None


def _detect_window_from_text(text: str) -> str | None:
    lower = text.lower()
    if any(word in lower for word in ("веч", "ніч")):
        return "evening"
    if any(word in lower for word in ("ран", "утр")):
        return "morning"
    if "день" in lower or "вдень" in lower:
        return "day"
    return None