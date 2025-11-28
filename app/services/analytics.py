from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from app.config.settings import Settings, get_settings
from app.schemas.calendar import CalendarEvent
from app.services.google_calendar import GoogleCalendarService


@dataclass(slots=True)
class CategoryStat:
    label: str
    hours: float


@dataclass(slots=True)
class AnalyticsSnapshot:
    telegram_id: int
    days: int
    total_hours: float
    busy_ratio: float
    category_stats: list[CategoryStat]
    busiest_day: tuple[str, float] | None
    long_blocks: int
    avg_block_minutes: float
    habit_sessions: int
    series_blocks: int
    recommendations: list[str]


class AnalyticsService:

    CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
        "Навчання": ("лекц", "пара", "іспит", "семінар", "практик", "курсов", "диплом"),
        "Робота": ("зустріч", "мітинг", "менедж", "проєкт", "standup", "client", "demo"),
        "Особисте": ("сім", "друзі", "вечер", "кава", "прогулян", "спорт", "йога"),
        "Фокус": ("focus", "deep work", "writing", "code", "аналіз"),
    }

    def __init__(
        self,
        calendar_service: GoogleCalendarService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.calendar = calendar_service or GoogleCalendarService(settings=self.settings)

    async def compute_snapshot(self, telegram_id: int, days: int = 7) -> AnalyticsSnapshot:
        tz = ZoneInfo(self.settings.timezone)
        now = datetime.now(tz)
        start = now - timedelta(days=days)
        end = now

        events = await self.calendar.list_events_between(
            telegram_id,
            start=start,
            end=end,
            max_results=250,
        )

        day_totals: dict[str, float] = {}
        category_totals: dict[str, float] = {}
        total_minutes = 0.0
        block_lengths: list[float] = []
        habit_sessions = 0
        series_blocks = 0

        for item in events:
            start_dt = _extract_datetime(item.start)
            end_dt = _extract_datetime(item.end)
            if not start_dt or not end_dt:
                continue
            duration = (end_dt - start_dt).total_seconds() / 60
            if duration <= 0:
                continue

            total_minutes += duration
            block_lengths.append(duration)

            day_key = start_dt.strftime("%a %d.%m")
            day_totals[day_key] = day_totals.get(day_key, 0.0) + duration / 60

            category_label = self._detect_category(item)
            category_totals[category_label] = category_totals.get(category_label, 0.0) + duration / 60

            description = (item.description or "").lower() if item.description else ""
            summary = (item.summary or "").lower()
            if "сесія звички" in description:
                habit_sessions += 1
            if "series:" in description or summary.startswith("[series"):
                series_blocks += 1

        total_hours = round(total_minutes / 60, 1)
        possible_hours = days * 24
        busy_ratio = min(1.0, total_hours / possible_hours) if possible_hours else 0.0
        category_stats = [
            CategoryStat(label=name, hours=round(value, 1))
            for name, value in sorted(category_totals.items(), key=lambda kv: kv[1], reverse=True)
        ]
        busiest_day = None
        if day_totals:
            day, hours = max(day_totals.items(), key=lambda kv: kv[1])
            busiest_day = (day, round(hours, 1))

        long_blocks = sum(1 for length in block_lengths if length >= 90)
        avg_block_minutes = round(sum(block_lengths) / len(block_lengths), 1) if block_lengths else 0.0

        recommendations = self._build_recommendations(
            total_hours=total_hours,
            busy_ratio=busy_ratio,
            long_blocks=long_blocks,
            avg_block_minutes=avg_block_minutes,
            habit_sessions=habit_sessions,
            series_blocks=series_blocks,
        )

        return AnalyticsSnapshot(
            telegram_id=telegram_id,
            days=days,
            total_hours=total_hours,
            busy_ratio=busy_ratio,
            category_stats=category_stats,
            busiest_day=busiest_day,
            long_blocks=long_blocks,
            avg_block_minutes=avg_block_minutes,
            habit_sessions=habit_sessions,
            series_blocks=series_blocks,
            recommendations=recommendations,
        )

    def _detect_category(self, event: CalendarEvent | dict[str, Any]) -> str:
        if isinstance(event, CalendarEvent):
            summary = event.summary or ""
            description = event.description or ""
            color_id = event.color_id
        else:
            summary = event.get("summary", "")
            description = event.get("description", "")
            color_id = event.get("colorId")
        text = f"{summary} {description}".lower()
        for label, keywords in self.CATEGORY_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                return label
        if color_id == "10":
            return "Особисте"
        if color_id == "11":
            return "Навчання"
        return "Інше"

    @staticmethod
    def _build_recommendations(
        *,
        total_hours: float,
        busy_ratio: float,
        long_blocks: int,
        avg_block_minutes: float,
        habit_sessions: int,
        series_blocks: int,
    ) -> list[str]:
        hints: list[str] = []
        if busy_ratio > 0.6:
            hints.append("Спробуй залишити хоча б один повністю вільний вечір для відпочинку.")
        if long_blocks < 2 and total_hours > 10:
            hints.append("Додай 1-2 довгі фокус-сесії (>90 хв), щоб рухати великі задачі.")
        if avg_block_minutes < 45:
            hints.append("Багато коротких зустрічей — згрупуй їх або заблокуй \"фокус-час\".")
        if habit_sessions < 1:
            hints.append("Жодної сесії звичок — перевір, чи не час відновити тренування або навчання.")
        if series_blocks < 1:
            hints.append("Немає блоків підготовки — можна запустити /plan для важливих дедлайнів.")
        return hints


def _extract_datetime(payload: dict[str, Any] | None) -> datetime | None:
    if not payload:
        return None
    value = payload.get("dateTime")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

