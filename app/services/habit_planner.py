from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config.settings import Settings, get_settings
from app.db.repository import HabitRepository, UserRepository, get_session
from app.services.google_calendar import GoogleCalendarService

PREFERRED_WINDOWS = {
    "morning": (6, 12),
    "day": (12, 18),
    "evening": (18, 22),
}


@dataclass(slots=True)
class HabitSetup:
    name: str
    duration_minutes: int
    preferred_time_of_day: str | None
    target_sessions_per_week: int
    use_recurrence: bool = False
    fixed_time: str | None = None


class HabitPlannerService:

    def __init__(
        self,
        calendar_service: GoogleCalendarService | None = None,
        habit_repository: HabitRepository | None = None,
        user_repository: UserRepository | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.calendar_service = calendar_service or GoogleCalendarService(settings=self.settings)
        self.habit_repository = habit_repository or HabitRepository()
        self.user_repository = user_repository or UserRepository()

    async def setup_habit(self, telegram_id: int, habit_setup: HabitSetup) -> str:
        with get_session() as session:
            user = self.user_repository.get_by_telegram_id(session, telegram_id)
            if not user:
                raise RuntimeError("user_not_registered")

            habit = self.habit_repository.create_habit(
                session,
                user_id=user.id,
                name=habit_setup.name,
                duration_minutes=habit_setup.duration_minutes,
                preferred_time_of_day=habit_setup.preferred_time_of_day,
                target_sessions_per_week=habit_setup.target_sessions_per_week,
                start_date=datetime.now(),
            )

        if habit_setup.use_recurrence and habit_setup.fixed_time:
            return await self._setup_recurring_habit(telegram_id, habit_setup, habit.id)
        else:
            return await self._setup_flexible_habit(telegram_id, habit_setup, habit.id)

    async def _setup_recurring_habit(self, telegram_id: int, habit_setup: HabitSetup, habit_id: int) -> str:
        tz = ZoneInfo(self.settings.timezone)
        now = datetime.now(tz)
        
        hour, minute = map(int, habit_setup.fixed_time.split(":"))
        start_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        
        if start_time < now:
            start_time += timedelta(days=1)
        
        end_time = start_time + timedelta(minutes=habit_setup.duration_minutes)
        
        if habit_setup.target_sessions_per_week == 7:
            recurrence_rule = "RRULE:FREQ=DAILY;COUNT=30"
            description = "щодня"
        else:
            days_map = {1: "MO", 2: "TU", 3: "WE", 4: "TH", 5: "FR", 6: "SA", 7: "SU"}
            step = 7 // habit_setup.target_sessions_per_week
            weekdays = [days_map[i % 7 + 1] for i in range(0, 7, step)][:habit_setup.target_sessions_per_week]
            byday = ",".join(weekdays)
            recurrence_rule = f"RRULE:FREQ=WEEKLY;COUNT=12;BYDAY={byday}"
            description = f"{habit_setup.target_sessions_per_week} рази на тиждень"
        
        event = await self.calendar_service.create_event(
            telegram_id,
            summary=habit_setup.name,
            start={
                "dateTime": start_time.isoformat(),
                "timeZone": self.settings.timezone,
            },
            end={
                "dateTime": end_time.isoformat(),
                "timeZone": self.settings.timezone,
            },
            recurrence=[recurrence_rule],
            description=f"Звичка: {habit_setup.name} ({description})",
        )
        
        return (
            f"✅ Створено повторювану подію \"{habit_setup.name}\"\n"
            f"⏰ {habit_setup.fixed_time}, {description}\n"
            f"📅 Посилання: {event.html_link or ''}"
        )

    async def _setup_flexible_habit(self, telegram_id: int, habit_setup: HabitSetup, habit_id: int) -> str:
        slots = await self._calculate_slots(
            telegram_id,
            habit_setup.target_sessions_per_week,
            habit_setup.duration_minutes,
            habit_setup.preferred_time_of_day,
        )

        if not slots:
            return "Не вдалося знайти вільні слоти для звички на наступний тиждень. Спробуй пізніше."

        created_events = []
        for slot in slots:
            event = await self.calendar_service.create_event(
                telegram_id,
                summary=habit_setup.name,
                start={
                    "dateTime": slot[0].isoformat(),
                    "timeZone": self.settings.timezone,
                },
                end={
                    "dateTime": slot[1].isoformat(),
                    "timeZone": self.settings.timezone,
                },
                description="Сесія звички",
            )
            created_events.append(event)

        lines = ["✅ Заплановано сесії звички:"]
        for event in created_events:
            start_str = event.start.get("dateTime") or event.start.get("date")
            if start_str:
                dt = datetime.fromisoformat(start_str)
                lines.append(f"• {habit_setup.name} — {dt:%d.%m %H:%M}")
        lines.append("\nВони додані до твого Google Calendar.")
        return "\n".join(lines)

    async def _calculate_slots(
        self,
        telegram_id: int,
        sessions_per_week: int,
        duration_minutes: int,
        preferred_time_of_day: str | None,
    ) -> list[tuple[datetime, datetime]]:
        tz = ZoneInfo(self.settings.timezone)
        now = datetime.now(tz)
        end_period = now + timedelta(days=7)
        busy = await self._fetch_busy_intervals(telegram_id, now, end_period)

        slots: list[tuple[datetime, datetime]] = []
        cursor = now
        window = PREFERRED_WINDOWS.get(preferred_time_of_day, (6, 22))

        while cursor < end_period and len(slots) < sessions_per_week:
            day_start = cursor.replace(hour=window[0], minute=0, second=0, microsecond=0)
            day_end = cursor.replace(hour=window[1], minute=0, second=0, microsecond=0)
            candidate_start = day_start
            while candidate_start + timedelta(minutes=duration_minutes) <= day_end:
                candidate_end = candidate_start + timedelta(minutes=duration_minutes)
                if self._is_free(candidate_start, candidate_end, busy):
                    slots.append((candidate_start, candidate_end))
                    break
                candidate_start += timedelta(minutes=30)
            cursor = (cursor + timedelta(days=1)).replace(hour=window[0])

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
    def _is_free(start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
        for b_start, b_end in busy:
            if start < b_end and end > b_start:
                return False
        return True

    @staticmethod
    def _format_summary(events: list[dict[str, Any]]) -> str:
        if not events:
            return "Не вдалося знайти вільні слоти на цьому тижні."
        lines = ["Заплановано сесії звички:"]
        for event in events:
            summary = event.get("summary", "Сесія")
            start = event.get("start", {}).get("dateTime")
            lines.append(f"• {summary} — {start}")
        return "\n".join(lines)
