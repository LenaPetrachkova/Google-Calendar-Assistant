from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.bot.context import ServiceContainer, get_services
from app.services.series_planner import (
    SeriesPlanBlock,
    SeriesPlanPreview,
    SeriesPlanRequest,
)

SERIES_TIME_RANGES = {
    "morning": ("08:00-12:00", 8, 12),
    "day": ("12:00-17:00", 12, 17),
    "evening": ("17:00-22:00", 17, 22),
    "any": ("08:00-22:00", 8, 22),
}

MONTH_KEYWORDS = {
    "січ": 1,
    "лют": 2,
    "бер": 3,
    "квіт": 4,
    "трав": 5,
    "чер": 6,
    "лип": 7,
    "сер": 8,
    "вер": 9,
    "жовт": 10,
    "лист": 11,
    "груд": 12,
}

WEEKDAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


async def series_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    await handle_series_shortcut(update, context, services)


async def handle_series_shortcut(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
) -> bool:
    telegram_id = update.effective_user.id

    context.user_data["expecting_series_goal"] = True
    await update.effective_message.reply_text(
        "Опиши, що потрібно запланувати. Наприклад: 'Підготовка до презентації'."
    )
    return True


async def handle_series_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    services = get_services(context)
    query = update.callback_query
    data = (query.data or "").strip()

    if data.startswith("series_time_"):
        _, _, key = data.partition("series_time_")
        if key not in SERIES_TIME_RANGES:
            await query.edit_message_text("Не розпізнав проміжок. Обери ще раз.")
            await _prompt_time_choice(query.edit_message_text)
            return True
        label, start_hour, end_hour = SERIES_TIME_RANGES[key]
        context.user_data["series_start_hour"] = start_hour
        context.user_data["series_end_hour"] = end_hour
        context.user_data.pop("series_allow_weekends", None)
        await query.edit_message_text(
            f"Обрано діапазон {label}. Використовуємо вихідні?",
            reply_markup=_weekend_keyboard(),
        )
        return True

    if data in {"series_weekend_yes", "series_weekend_no"}:
        context.user_data["series_allow_weekends"] = data.endswith("yes")
        await query.edit_message_text("🔍 Підбираю блоки…")
        await _show_series_preview(
            update,
            context,
            services,
            edit_func=query.edit_message_text,
        )
        return True

    if data == "series_change_time":
        await query.edit_message_text("Оберемо інший проміжок:")
        await _prompt_time_choice(query.edit_message_text)
        return True

    if data == "series_cancel":
        await query.edit_message_text("Планування серії скасовано.")
        _clear_series_context(context)
        return True

    if data == "series_confirm":
        payload = context.user_data.get("pending_series_plan")
        if not payload:
            await query.edit_message_text("Немає плану для створення. Спробуй /plan ще раз.")
            return True
        try:
            preview = _payload_to_preview(payload)
        except ValueError as exc:
            await query.edit_message_text(str(exc))
            return True
        try:
            result = await services.series_planner.commit_plan(preview)
        except Exception as exc:  # pragma: no cover
            await query.edit_message_text(f"Не вдалося створити блоки: {exc}")
            return True

        lines = [
            f"✅ Створено {len(result.created_blocks)} блоків серії.",
            "Посилання на події:",
        ]
        for idx, (block, link) in enumerate(zip(result.created_blocks, result.event_links), start=1):
            weekday = WEEKDAY_SHORT[block.start.weekday()]
            label = f"{weekday} {block.start:%d.%m %H:%M}"
            suffix = f" — {link}" if link else ""
            lines.append(f"• #{idx} {label}{suffix}")
        deadline_label = preview.request.deadline.astimezone(ZoneInfo(services.settings.timezone))
        lines.append("")
        if result.deadline_event_link:
            lines.append(
                f"⌛️ Дедлайн {deadline_label:%d.%m %H:%M} — {result.deadline_event_link}"
            )
        else:
            lines.append(f"⌛️ Дедлайн: {deadline_label:%d.%m %H:%M}")
        await query.edit_message_text("\n".join(lines))
        _clear_series_context(context)
        return True

    return False


async def process_series_state_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    text: str,
) -> bool:
    message = update.effective_message
    tz = ZoneInfo(services.settings.timezone)

    if context.user_data.pop("expecting_series_goal", False):
        context.user_data["series_goal"] = text.strip()
        context.user_data["expecting_series_deadline"] = True
        await message.reply_text(
            "Коли дедлайн? Напиши дату у форматі '25.11 10:00' або '2025-11-25'. "
            "Можна вказати слова 'завтра', 'наступний понеділок'."
        )
        return True

    if context.user_data.pop("expecting_series_deadline", False):
        deadline = _parse_deadline(text, tz)
        if not deadline:
            context.user_data["expecting_series_deadline"] = True
            await message.reply_text(
                "Не вдалося зрозуміти дату. Приклад: '25.11 13:00' або 'понеділок 18:00'."
            )
            return True
        context.user_data["series_deadline"] = deadline.isoformat()
        context.user_data["expecting_series_hours"] = True
        await message.reply_text(
            "Скільки годин потрібно на підготовку загалом? Наприклад: 6 або 8.5."
        )
        return True

    if context.user_data.pop("expecting_series_hours", False):
        try:
            hours = float(text.replace(",", "."))
        except ValueError:
            context.user_data["expecting_series_hours"] = True
            await message.reply_text("Введи число годин, наприклад 6 або 10.5.")
            return True
        minutes = max(30, int(hours * 60))
        context.user_data["series_total_minutes"] = minutes
        context.user_data["expecting_series_block_duration"] = True
        suggestion = max(45, min(120, int(minutes / max(1, round(hours)))))
        await message.reply_text(
            f"Яка тривалість одного блоку (у хвилинах)? Наприклад {suggestion}."
        )
        return True

    if context.user_data.pop("expecting_series_block_duration", False):
        try:
            block_minutes = int(text.strip())
        except ValueError:
            context.user_data["expecting_series_block_duration"] = True
            await message.reply_text("Вкажи тривалість у хвилинах, наприклад 60 або 90.")
            return True
        block_minutes = max(30, min(block_minutes, 240))
        context.user_data["series_block_minutes"] = block_minutes
        await message.reply_text("Оберемо проміжок дня для блоків:")
        await _prompt_time_choice(message.reply_text)
        return True

    return False


async def handle_series_intent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    metadata: dict,
) -> None:
    title = metadata.get("title") or "Серія"
    deadline_str = metadata.get("deadline")
    total_hours = metadata.get("total_hours") or 4
    block_minutes = metadata.get("block_minutes") or 90
    preferred = metadata.get("preferred_window") or "any"
    allow_weekends = bool(metadata.get("allow_weekends", True))

    tz = ZoneInfo(services.settings.timezone)
    deadline = _parse_deadline(deadline_str or "", tz)
    if not deadline:
        context.user_data["expecting_series_goal"] = True
        await update.effective_message.reply_text(
            "Потрібно уточнити дедлайн. Напиши дату, наприклад '25.11 10:00'."
        )
        return

    context.user_data.update(
        {
            "series_goal": title,
            "series_deadline": deadline.isoformat(),
            "series_total_minutes": int(float(total_hours) * 60),
            "series_block_minutes": int(block_minutes),
            "series_start_hour": SERIES_TIME_RANGES.get(preferred, SERIES_TIME_RANGES["any"])[1],
            "series_end_hour": SERIES_TIME_RANGES.get(preferred, SERIES_TIME_RANGES["any"])[2],
            "series_allow_weekends": allow_weekends,
        }
    )
    await update.effective_message.reply_text("🔍 Будую попередній план…")
    await _show_series_preview(update, context, services)


async def _prompt_time_choice(send_func: Callable):
    buttons = [
        [
            InlineKeyboardButton("Ранок (08-12)", callback_data="series_time_morning"),
            InlineKeyboardButton("День (12-17)", callback_data="series_time_day"),
        ],
        [
            InlineKeyboardButton("Вечір (17-22)", callback_data="series_time_evening"),
            InlineKeyboardButton("Будь-коли (08-22)", callback_data="series_time_any"),
        ],
    ]
    await send_func(
        "Оберіть бажаний час доби для блоків:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _weekend_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("Так, можна", callback_data="series_weekend_yes"),
            InlineKeyboardButton("Лише будні", callback_data="series_weekend_no"),
        ]
    ]
    return InlineKeyboardMarkup(buttons)


async def _show_series_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    edit_func=None,
) -> None:
    telegram_id = update.effective_user.id
    request = _build_request_from_context(context, services, telegram_id)
    message_func = edit_func or update.effective_message.reply_text
    try:
        preview = await services.series_planner.plan_series(request)
    except ValueError as exc:
        await message_func(str(exc))
        return
    except Exception as exc:  # pragma: no cover
        await message_func(f"Не вдалося знайти блоки: {exc}")
        return

    if not preview.blocks:
        await message_func(
            "Не знайшов жодних вікон до дедлайну. Спробуй дозволити вихідні або зменшити тривалість одного блоку."
        )
        return

    context.user_data["pending_series_plan"] = {
        "request": {
            "telegram_id": request.telegram_id,
            "title": request.title,
            "deadline": request.deadline.isoformat(),
            "total_minutes": request.total_minutes,
            "block_minutes": request.block_minutes,
            "preferred_start_hour": request.preferred_start_hour,
            "preferred_end_hour": request.preferred_end_hour,
            "allow_weekends": request.allow_weekends,
            "description": request.description or "",
        },
        "blocks": [
            {
                "index": block.index,
                "label": block.label,
                "start": block.start.isoformat(),
                "end": block.end.isoformat(),
            }
            for block in preview.blocks
        ],
        "warnings": preview.warnings,
    }

    lines = [
        f"📚 План «{request.title}»",
        f"Дедлайн: {request.deadline:%d.%m %H:%M}",
        f"Блоків: {len(preview.blocks)} по {request.block_minutes} хв",
        "",
    ]
    for block in preview.blocks:
        weekday = WEEKDAY_SHORT[block.start.weekday()]
        lines.append(f"• #{block.index + 1} {weekday} {block.start:%d.%m %H:%M} → {block.end:%H:%M}")
    if preview.warnings:
        lines.append("\n⚠️ " + " ".join(preview.warnings))

    buttons = [
        [InlineKeyboardButton("✅ Створити", callback_data="series_confirm")],
        [InlineKeyboardButton("🔄 Змінити діапазон", callback_data="series_change_time")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="series_cancel")],
    ]

    await message_func("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


def _build_request_from_context(
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    telegram_id: int,
) -> SeriesPlanRequest:
    try:
        deadline = datetime.fromisoformat(context.user_data["series_deadline"])
    except (KeyError, ValueError):
        raise ValueError("Не вказано дедлайн.")

    return SeriesPlanRequest(
        telegram_id=telegram_id,
        title=context.user_data.get("series_goal", "Серія"),
        deadline=deadline,
        total_minutes=context.user_data.get("series_total_minutes", 240),
        block_minutes=context.user_data.get("series_block_minutes", 90),
        preferred_start_hour=context.user_data.get("series_start_hour"),
        preferred_end_hour=context.user_data.get("series_end_hour"),
        allow_weekends=context.user_data.get("series_allow_weekends", True),
        description=context.user_data.get("series_notes"),
    )


def _payload_to_preview(payload: dict) -> SeriesPlanPreview:
    req_data = payload.get("request")
    block_data = payload.get("blocks", [])
    if not req_data or not block_data:
        raise ValueError("План відсутній. Спробуй побудувати його знову.")

    request = SeriesPlanRequest(
        telegram_id=int(req_data.get("telegram_id", 0)),
        title=req_data.get("title", "Серія"),
        deadline=datetime.fromisoformat(req_data["deadline"]),
        total_minutes=int(req_data.get("total_minutes", 0)),
        block_minutes=int(req_data.get("block_minutes", 60)),
        preferred_start_hour=req_data.get("preferred_start_hour"),
        preferred_end_hour=req_data.get("preferred_end_hour"),
        allow_weekends=bool(req_data.get("allow_weekends", True)),
        description=req_data.get("description") or None,
    )
    blocks = [
        SeriesPlanBlock(
            index=int(item["index"]),
            label=item.get("label", f"Блок {idx + 1}"),
            start=datetime.fromisoformat(item["start"]),
            end=datetime.fromisoformat(item["end"]),
        )
        for idx, item in enumerate(block_data)
    ]
    return SeriesPlanPreview(
        request=request,
        blocks=blocks,
        missing_blocks=int(payload.get("missing_blocks", 0)),
        warnings=payload.get("warnings", []),
    )


def _parse_deadline(text: str, tz: ZoneInfo) -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    lower = text.lower()
    now = datetime.now(tz)
    now_naive = now.replace(tzinfo=None)
    keyword_map = {
        "сьогодні": 0,
        "завтра": 1,
        "післязавтра": 2,
    }
    for key, delta in keyword_map.items():
        if key in lower:
            hour = _extract_hour(lower) or 18
            minute = _extract_minute(lower) or 0
            target = (now + timedelta(days=delta)).replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
            return target

    month_phrase = _parse_month_phrase(text, tz, now)
    if month_phrase:
        return month_phrase

    patterns = [
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
        "%d.%m %H:%M",
        "%d.%m",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ]
    text_clean = text.replace(" о ", " ").replace(" о", " ")

    for pattern in patterns:
        try:
            parsed = datetime.strptime(text_clean, pattern)
            if "%Y" not in pattern:
                parsed = parsed.replace(year=now.year)
                if parsed < now_naive:
                    parsed = parsed.replace(year=now.year + 1)
            if "%H" not in pattern:
                parsed = parsed.replace(hour=18, minute=0)
            parsed = parsed.replace(tzinfo=tz)
            return parsed
        except ValueError:
            continue
    return None


def _parse_month_phrase(text: str, tz: ZoneInfo, now: datetime) -> datetime | None:
    match = re.search(
        r"(?P<day>\d{1,2})\s+(?P<month>[а-яіїє]+)(?:\s+(?P<year>\d{4}))?(?:\s+(?P<time>\d{1,2}:\d{2}))?",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    day = int(match.group("day"))
    month_word = match.group("month").lower()
    month = None
    for key, value in MONTH_KEYWORDS.items():
        if month_word.startswith(key):
            month = value
            break
    if not month:
        return None
    year = int(match.group("year")) if match.group("year") else now.year
    hour = 18
    minute = 0
    if match.group("time"):
        hour_str, minute_str = match.group("time").split(":")
        hour = int(hour_str)
        minute = int(minute_str)
    try:
        parsed = datetime(year, month, day, hour, minute)
    except ValueError:
        return None
    if not match.group("year"):
        if parsed < now.replace(tzinfo=None):
            parsed = parsed.replace(year=now.year + 1)
    return parsed.replace(tzinfo=tz)


def _extract_hour(text: str) -> int | None:
    match = re.search(r"(\d{1,2}):\d{2}", text)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d{1,2})\s*год", text)
    if match:
        return int(match.group(1))
    return None


def _extract_minute(text: str) -> int | None:
    match = re.search(r"\d{1,2}:(\d{2})", text)
    if match:
        return int(match.group(1))
    return None


def _clear_series_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in list(context.user_data.keys()):
        if key.startswith("series_") or key.startswith("pending_series"):
            context.user_data.pop(key, None)

