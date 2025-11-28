from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from app.config.settings import Settings, get_settings
from app.services.google_calendar import GoogleCalendarService


@dataclass(slots=True)
class FreeSlotRequest:
    telegram_id: int
    duration_minutes: int
    date_from: datetime
    date_to: datetime
    preferred_start: int | None = None      
    preferred_end: int | None = None


@dataclass(slots=True)
class FreeSlot:
    start: datetime
    end: datetime

    def to_message_line(self) -> str:
        return f"• {self.start:%d.%m %H:%M} — {self.end:%H:%M}"


class FreeSlotService:

    def __init__(self, calendar_service: GoogleCalendarService | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.calendar_service = calendar_service or GoogleCalendarService(settings=self.settings)

    async def find_slots(self, request: FreeSlotRequest, max_suggestions: int = 3) -> list[FreeSlot]:
        busy = await self._fetch_busy_intervals(request.telegram_id, request.date_from, request.date_to)
        tz = ZoneInfo(self.settings.timezone)
        duration = timedelta(minutes=request.duration_minutes)
        start_bound = request.date_from.astimezone(tz)
        end_bound = request.date_to.astimezone(tz)

        slots: list[FreeSlot] = []
        cursor = start_bound
        while cursor < end_bound and len(slots) < max_suggestions:
            day_start_hour = request.preferred_start if request.preferred_start is not None else 8
            day_end_hour = request.preferred_end if request.preferred_end is not None else 20
            day_start = cursor.replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
            day_end = cursor.replace(hour=day_end_hour, minute=0, second=0, microsecond=0)
            if day_start < start_bound:
                day_start = start_bound
            if day_end > end_bound:
                day_end = end_bound

            candidate_start = max(cursor, day_start)
            while candidate_start + duration <= day_end:
                candidate_end = candidate_start + duration
                if candidate_end > end_bound:
                    break
                if self._is_free(candidate_start, candidate_end, busy):
                    slots.append(FreeSlot(candidate_start, candidate_end))
                    break
                candidate_start += timedelta(minutes=30)

            next_day = (cursor + timedelta(days=1)).replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
            cursor = max(next_day, start_bound)
        return slots

    async def _fetch_busy_intervals(
        self,
        telegram_id: int,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, datetime]]:
        try:
            events = await self.calendar_service.list_events_between(
                telegram_id,
                start,
                end,
                max_results=250,
            )
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("calendar_fetch_failed") from exc

        busy: list[tuple[datetime, datetime]] = []
        for event in events:
            start_str = event.start.get("dateTime")
            end_str = event.end.get("dateTime")
            if not start_str or not end_str:
                continue
            busy.append((datetime.fromisoformat(start_str), datetime.fromisoformat(end_str)))
        return busy

    @staticmethod
    def _is_free(candidate_start: datetime, candidate_end: datetime, busy: Iterable[tuple[datetime, datetime]]) -> bool:
        for b_start, b_end in busy:
            if candidate_start < b_end and candidate_end > b_start:
                return False
        return True

    @staticmethod
    def format_slots(slots: list[FreeSlot]) -> str:
        if not slots:
            return "Вільних вікон у зазначеному проміжку не знайдено."
        lines = ["Доступні варіанти:"]
        lines.extend(slot.to_message_line() for slot in slots)
        return "\n".join(lines)
