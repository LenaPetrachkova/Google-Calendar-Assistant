from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from telegram.ext import ContextTypes

from app.config.settings import Settings
from app.db.repository import HabitRepository, UserRepository, get_session
from app.services.free_slots import FreeSlot, FreeSlotService
from app.schemas.calendar import EventDraft, EventUpdatePayload
from app.services.gemini import GeminiService
from app.services.google_calendar import GoogleCalendarService
from app.services.habit_planner import HabitPlannerService
from app.services.analytics import AnalyticsService
from app.services.series_planner import SeriesPlannerService

FREE_SLOT_EXPECTATION_KEY = "expecting_window_query"

RESET_KEYWORDS = (
    "стоп",
    "відміна",
    "відміни",
    "відмінити",
    "заново",
    "почнемо спочатку",
    "почни спочатку",
    "скасуй все",
    "скасувати все",
    "припини",
    "досить",
    "/cancel",
    "/stop",
)


class ContextKey(str, Enum):
    LAST_EVENT = "last_event_context"
    LAST_EVENT_LEGACY = "last_created_event"
    AGENDA = "agenda_context"
    PENDING_CREATE_CONFLICT = "pending_conflict"
    PENDING_UPDATE_CONFLICT = "pending_update_conflict"
    LAST_FREE_SLOTS = "last_free_slots"
    LAST_EVENT_QUERY = "last_event_query"
    PENDING_DELETE = "pending_delete"
    PENDING_DELETE_LIST = "pending_delete_list"
    PENDING_UPDATE_LIST = "pending_update_list"
    PENDING_UPDATE_DATA = "pending_update_data"
    PENDING_UPDATE_DETAIL = "pending_update_detail"


@dataclass(slots=True)
class EventContext:
    event_id: str | None
    summary: str


@dataclass(slots=True)
class AgendaContext:
    date: str
    date_dt: datetime
    time_window: str


@dataclass(slots=True)
class PendingCreateConflict:
    draft: EventDraft
    conflict: dict[str, Any]
    reply_text: str = "Подію створено."


@dataclass(slots=True)
class PendingUpdateConflict:
    event_id: str | None
    update: EventUpdatePayload
    original_event: dict[str, Any]
    conflict: dict[str, Any] | None = None


@dataclass(slots=True)
class LastFreeSlotsRequest:
    duration: int
    date_from: str
    date_to: str
    preferred_window: str | None = None
    preferred_start: int | None = None
    preferred_end: int | None = None
    next_start: str | None = None
    cursor_history: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "duration": self.duration,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "preferred_window": self.preferred_window,
            "preferred_start": self.preferred_start,
            "preferred_end": self.preferred_end,
            "next_start": self.next_start,
            "cursor_history": list(self.cursor_history),
        }


@dataclass(slots=True)
class LastFreeSlotsContext:
    slots: list[FreeSlot | str]
    request: LastFreeSlotsRequest
    awaiting_use: bool = False


@dataclass(slots=True)
class PendingDeleteItem:
    event_id: str
    summary: str
    start: str


@dataclass(slots=True)
class PendingDeleteContext:
    event_id: str
    summary: str
    start: str


@dataclass(slots=True)
class PendingUpdateListItem:
    event_id: str
    summary: str
    start: str
    event_data: dict[str, Any]


@dataclass(slots=True)
class PendingUpdateListContext:
    items: list[PendingUpdateListItem]
    update_data: EventUpdatePayload


@dataclass(slots=True)
class PendingUpdateDetail:
    keywords: str


@dataclass(slots=True)
class ServiceContainer:
    settings: Settings
    gemini: GeminiService
    calendar: GoogleCalendarService
    user_repo: UserRepository
    habit_repo: HabitRepository
    habit_planner: HabitPlannerService
    free_slot_service: FreeSlotService
    analytics: AnalyticsService
    series_planner: SeriesPlannerService

    def has_credentials(self, telegram_id: int) -> bool:
        with get_session() as session:
            user = self.user_repo.get_by_telegram_id(session, telegram_id)
            return bool(user and user.credentials_json)


def get_services(context: ContextTypes.DEFAULT_TYPE) -> ServiceContainer:
    return context.application.bot_data["services"]


def reset_user_context(context: ContextTypes.DEFAULT_TYPE, preserve: tuple[str, ...] = ()) -> None:
    preserved = {key: context.user_data.get(key) for key in preserve}
    context.user_data.clear()
    for key, value in preserved.items():
        if value is not None:
            context.user_data[key] = value


def _coerce_event_context(raw: Any) -> EventContext | None:
    if isinstance(raw, EventContext):
        return raw
    if isinstance(raw, dict):
        return EventContext(
            event_id=raw.get("id"),
            summary=raw.get("summary") or "(без назви)",
        )
    return None


def set_last_event_context(
    context: ContextTypes.DEFAULT_TYPE,
    event_id: str | None,
    summary: str | None,
) -> None:
    if not (event_id or summary):
        return
    payload = EventContext(event_id=event_id, summary=summary or "(без назви)")
    context.user_data[ContextKey.LAST_EVENT.value] = payload
    context.user_data[ContextKey.LAST_EVENT_LEGACY.value] = payload  # зворотна сумісність


def get_last_event_context(context: ContextTypes.DEFAULT_TYPE) -> EventContext | None:
    raw = context.user_data.get(ContextKey.LAST_EVENT.value) or context.user_data.get(
        ContextKey.LAST_EVENT_LEGACY.value
    )
    return _coerce_event_context(raw)


def set_agenda_context(
    context: ContextTypes.DEFAULT_TYPE,
    agenda: AgendaContext | None,
) -> None:
    if agenda is None:
        context.user_data.pop(ContextKey.AGENDA.value, None)
    else:
        context.user_data[ContextKey.AGENDA.value] = agenda


def get_agenda_context(context: ContextTypes.DEFAULT_TYPE) -> AgendaContext | None:
    raw = context.user_data.get(ContextKey.AGENDA.value)
    if isinstance(raw, AgendaContext):
        return raw
    if isinstance(raw, dict):
        date = raw.get("date") or ""
        date_dt = raw.get("date_dt")
        time_window = raw.get("time_window") or "full"
        if isinstance(date_dt, str):
            try:
                date_dt = datetime.fromisoformat(date_dt)
            except ValueError:
                date_dt = None
        if date_dt is None:
            return None
        return AgendaContext(date=date, date_dt=date_dt, time_window=time_window)
    return None


def set_pending_create_conflict(
    context: ContextTypes.DEFAULT_TYPE,
    conflict: PendingCreateConflict | None,
) -> None:
    key = ContextKey.PENDING_CREATE_CONFLICT.value
    if conflict is None:
        context.user_data.pop(key, None)
    else:
        context.user_data[key] = conflict


def pop_pending_create_conflict(context: ContextTypes.DEFAULT_TYPE) -> PendingCreateConflict | None:
    key = ContextKey.PENDING_CREATE_CONFLICT.value
    raw = context.user_data.pop(key, None)
    if isinstance(raw, PendingCreateConflict):
        return raw
    if isinstance(raw, dict) and raw.get("event_payload"):
        try:
            draft = EventDraft.from_dict(raw.get("event_payload", {}))
        except Exception:
            return None
        return PendingCreateConflict(
            draft=draft,
            conflict=raw.get("conflict", {}),
            reply_text=raw.get("analysis_reply") or "Подію створено.",
        )
    return None


def set_pending_update_conflict(
    context: ContextTypes.DEFAULT_TYPE,
    conflict: PendingUpdateConflict | None,
) -> None:
    key = ContextKey.PENDING_UPDATE_CONFLICT.value
    if conflict is None:
        context.user_data.pop(key, None)
    else:
        context.user_data[key] = conflict


def pop_pending_update_conflict(context: ContextTypes.DEFAULT_TYPE) -> PendingUpdateConflict | None:
    key = ContextKey.PENDING_UPDATE_CONFLICT.value
    raw = context.user_data.pop(key, None)
    if isinstance(raw, PendingUpdateConflict):
        return raw
    if isinstance(raw, dict):
        update_payload = EventUpdatePayload.from_dict(raw)
        return PendingUpdateConflict(
            event_id=raw.get("event_id"),
            update=update_payload,
            original_event=raw.get("original_event") or {},
            conflict=raw.get("conflict"),
        )
    return None


def set_last_free_slots(
    context: ContextTypes.DEFAULT_TYPE,
    state: LastFreeSlotsContext | None,
) -> None:
    key = ContextKey.LAST_FREE_SLOTS.value
    if state is None:
        context.user_data.pop(key, None)
    else:
        context.user_data[key] = state


def get_last_free_slots(context: ContextTypes.DEFAULT_TYPE) -> LastFreeSlotsContext | None:
    raw = context.user_data.get(ContextKey.LAST_FREE_SLOTS.value)
    return _coerce_last_free_slots(raw)


def _coerce_last_free_slots(raw: Any) -> LastFreeSlotsContext | None:
    if isinstance(raw, LastFreeSlotsContext):
        return raw
    if isinstance(raw, dict):
        request_raw = raw.get("request") or {}
        try:
            duration = int(request_raw.get("duration"))
        except (TypeError, ValueError):
            return None
        request = LastFreeSlotsRequest(
            duration=duration,
            date_from=request_raw.get("date_from") or "",
            date_to=request_raw.get("date_to") or "",
            preferred_window=request_raw.get("preferred_window"),
            preferred_start=_safe_int(request_raw.get("preferred_start")),
            preferred_end=_safe_int(request_raw.get("preferred_end")),
            next_start=request_raw.get("next_start"),
            cursor_history=list(request_raw.get("cursor_history") or []),
        )
        slots = list(raw.get("slots") or [])
        awaiting = bool(raw.get("awaiting_use"))
        return LastFreeSlotsContext(slots=slots, request=request, awaiting_use=awaiting)
    return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def should_reset_context(lower_text: str) -> bool:
    normalized = lower_text.strip()
    if normalized in RESET_KEYWORDS:
        return True
    for phrase in RESET_KEYWORDS:
        if " " in phrase and phrase in lower_text:
            return True
        if (
            lower_text.startswith(f"{phrase} ")
            or lower_text.endswith(f" {phrase}")
            or f" {phrase} " in lower_text
        ):
            return True
    return False


def set_last_event_query(context: ContextTypes.DEFAULT_TYPE, query: str | None) -> None:
    key = ContextKey.LAST_EVENT_QUERY.value
    if query:
        context.user_data[key] = query
    else:
        context.user_data.pop(key, None)


def get_last_event_query(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get(ContextKey.LAST_EVENT_QUERY.value, "")


def pop_last_event_query(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.pop(ContextKey.LAST_EVENT_QUERY.value, "")


def set_pending_delete(
    context: ContextTypes.DEFAULT_TYPE,
    delete_context: PendingDeleteContext | None,
) -> None:
    key = ContextKey.PENDING_DELETE.value
    if delete_context is None:
        context.user_data.pop(key, None)
    else:
        context.user_data[key] = delete_context


def get_pending_delete(context: ContextTypes.DEFAULT_TYPE) -> PendingDeleteContext | None:
    raw = context.user_data.get(ContextKey.PENDING_DELETE.value)
    if isinstance(raw, PendingDeleteContext):
        return raw
    if isinstance(raw, dict):
        return PendingDeleteContext(
            event_id=raw.get("event_id", ""),
            summary=raw.get("summary", "(без назви)"),
            start=raw.get("start", ""),
        )
    return None


def pop_pending_delete(context: ContextTypes.DEFAULT_TYPE) -> PendingDeleteContext | None:
    key = ContextKey.PENDING_DELETE.value
    raw = context.user_data.pop(key, None)
    if isinstance(raw, PendingDeleteContext):
        return raw
    if isinstance(raw, dict):
        return PendingDeleteContext(
            event_id=raw.get("event_id", ""),
            summary=raw.get("summary", "(без назви)"),
            start=raw.get("start", ""),
        )
    return None


def set_pending_delete_list(
    context: ContextTypes.DEFAULT_TYPE,
    items: list[PendingDeleteItem] | None,
) -> None:
    key = ContextKey.PENDING_DELETE_LIST.value
    if items is None:
        context.user_data.pop(key, None)
    else:
        context.user_data[key] = items


def get_pending_delete_list(context: ContextTypes.DEFAULT_TYPE) -> list[PendingDeleteItem]:
    raw = context.user_data.get(ContextKey.PENDING_DELETE_LIST.value, [])
    if isinstance(raw, list) and raw:
        result: list[PendingDeleteItem] = []
        for item in raw:
            if isinstance(item, PendingDeleteItem):
                result.append(item)
            elif isinstance(item, dict):
                result.append(
                    PendingDeleteItem(
                        event_id=item.get("event_id", ""),
                        summary=item.get("summary", "(без назви)"),
                        start=item.get("start", ""),
                    )
                )
        return result
    return []


def pop_pending_delete_list(context: ContextTypes.DEFAULT_TYPE) -> list[PendingDeleteItem]:
    key = ContextKey.PENDING_DELETE_LIST.value
    raw = context.user_data.pop(key, None)
    if isinstance(raw, list) and raw:
        result: list[PendingDeleteItem] = []
        for item in raw:
            if isinstance(item, PendingDeleteItem):
                result.append(item)
            elif isinstance(item, dict):
                result.append(
                    PendingDeleteItem(
                        event_id=item.get("event_id", ""),
                        summary=item.get("summary", "(без назви)"),
                        start=item.get("start", ""),
                    )
                )
        return result
    return []


def set_pending_update_list(
    context: ContextTypes.DEFAULT_TYPE,
    list_context: PendingUpdateListContext | None,
) -> None:
    key = ContextKey.PENDING_UPDATE_LIST.value
    if list_context is None:
        context.user_data.pop(key, None)
    else:
        context.user_data[key] = list_context


def get_pending_update_list(context: ContextTypes.DEFAULT_TYPE) -> PendingUpdateListContext | None:
    raw_list = context.user_data.get(ContextKey.PENDING_UPDATE_LIST.value)
    raw_data = context.user_data.get(ContextKey.PENDING_UPDATE_DATA.value)
    
    if raw_list is None:
        return None
    
    if isinstance(raw_list, PendingUpdateListContext):
        return raw_list
    
    items: list[PendingUpdateListItem] = []
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, PendingDeleteItem):  # type: ignore
                continue
            elif isinstance(item, dict):
                items.append(
                    PendingUpdateListItem(
                        event_id=item.get("event_id", ""),
                        summary=item.get("summary", "(без назви)"),
                        start=item.get("start", ""),
                        event_data=item.get("event_data") or {},
                    )
                )
    
    update_data = EventUpdatePayload.from_dict(raw_data) if raw_data else EventUpdatePayload(patch={})
    
    if items or raw_data:
        return PendingUpdateListContext(items=items, update_data=update_data)
    return None


def pop_pending_update_list(context: ContextTypes.DEFAULT_TYPE) -> PendingUpdateListContext | None:
    result = get_pending_update_list(context)
    context.user_data.pop(ContextKey.PENDING_UPDATE_LIST.value, None)
    context.user_data.pop(ContextKey.PENDING_UPDATE_DATA.value, None)
    return result


def set_pending_update_detail(
    context: ContextTypes.DEFAULT_TYPE,
    detail: PendingUpdateDetail | None,
) -> None:
    key = ContextKey.PENDING_UPDATE_DETAIL.value
    if detail is None:
        context.user_data.pop(key, None)
    else:
        context.user_data[key] = detail


def get_pending_update_detail(context: ContextTypes.DEFAULT_TYPE) -> PendingUpdateDetail | None:
    raw = context.user_data.get(ContextKey.PENDING_UPDATE_DETAIL.value)
    if isinstance(raw, PendingUpdateDetail):
        return raw
    if isinstance(raw, dict):
        return PendingUpdateDetail(keywords=raw.get("keywords", ""))
    return None


def pop_pending_update_detail(context: ContextTypes.DEFAULT_TYPE) -> PendingUpdateDetail | None:
    key = ContextKey.PENDING_UPDATE_DETAIL.value
    raw = context.user_data.pop(key, None)
    if isinstance(raw, PendingUpdateDetail):
        return raw
    if isinstance(raw, dict):
        return PendingUpdateDetail(keywords=raw.get("keywords", ""))
    return None

