from __future__ import annotations

import math
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, List
from zoneinfo import ZoneInfo

from app.config.settings import Settings, get_settings
from app.db.repository import SeriesPlanRepository, UserRepository, get_session
from app.services.free_slots import FreeSlotRequest, FreeSlotService
from app.services.google_calendar import GoogleCalendarService
from app.schemas.calendar import RemindersConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SeriesPlanRequest:
    telegram_id: int
    title: str
    deadline: datetime
    total_minutes: int
    block_minutes: int
    preferred_start_hour: int | None
    preferred_end_hour: int | None
    allow_weekends: bool
    description: str | None = None


@dataclass(slots=True)
class SeriesPlanBlock:
    index: int
    label: str
    start: datetime
    end: datetime


@dataclass(slots=True)
class SeriesPlanPreview:
    request: SeriesPlanRequest
    blocks: list[SeriesPlanBlock]
    missing_blocks: int
    warnings: list[str]


@dataclass(slots=True)
class SeriesCommitResult:
    plan_id: int
    created_blocks: list[SeriesPlanBlock]
    event_links: list[str]
    deadline_event_link: str | None = None


class SeriesPlannerService:
    """Планування серій подій (time blocking під дедлайн)."""

    def __init__(
        self,
        *,
        calendar_service: GoogleCalendarService | None = None,
        free_slot_service: FreeSlotService | None = None,
        user_repository: UserRepository | None = None,
        plan_repository: SeriesPlanRepository | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.calendar = calendar_service or GoogleCalendarService(settings=self.settings)
        self.free_slots = free_slot_service or FreeSlotService(
            calendar_service=self.calendar,
            settings=self.settings,
        )
        self.user_repo = user_repository or UserRepository()
        self.plan_repo = plan_repository or SeriesPlanRepository()

    async def plan_series(self, request: SeriesPlanRequest) -> SeriesPlanPreview:
        tz = ZoneInfo(self.settings.timezone)
        now = datetime.now(tz)
        deadline = request.deadline.astimezone(tz)
        if deadline <= now:
            raise ValueError("Дедлайн вже минув або надто близько.")

        blocks_needed = max(1, math.ceil(request.total_minutes / request.block_minutes))
        fs_request = FreeSlotRequest(
            telegram_id=request.telegram_id,
            duration_minutes=request.block_minutes,
            date_from=now,
            date_to=deadline,
            preferred_start=request.preferred_start_hour,
            preferred_end=request.preferred_end_hour,
        )

        slots = await self.free_slots.find_slots(
            fs_request,
            max_suggestions=max(blocks_needed * 3, 6),
        )

        filtered_slots = self._filter_slots(slots, request.allow_weekends, deadline, blocks_needed)

        warnings: list[str] = []
        missing = max(0, blocks_needed - len(filtered_slots))
        if missing:
            warnings.append(
                f"Знайдено лише {blocks_needed - missing} з {blocks_needed} блоків. "
                "Можеш змінити діапазон часу або дозволити вихідні."
            )

        blocks = [
            SeriesPlanBlock(
                index=idx,
                label=f"Блок {idx + 1}",
                start=slot.start,
                end=slot.end,
            )
            for idx, slot in enumerate(filtered_slots[:blocks_needed])
        ]
        return SeriesPlanPreview(
            request=request,
            blocks=blocks,
            missing_blocks=missing,
            warnings=warnings,
        )

    async def commit_plan(self, preview: SeriesPlanPreview) -> SeriesCommitResult:
        if not preview.blocks:
            raise ValueError("Немає запланованих блоків для створення.")

        request = preview.request
        with get_session() as session:
            user = self.user_repo.get_by_telegram_id(session, request.telegram_id)
            if not user:
                raise RuntimeError("Користувача не знайдено. Перевір авторизацію Google.")

            plan = self.plan_repo.create_plan(
                session,
                user_id=user.id,
                title=request.title,
                deadline=request.deadline,
                total_minutes=request.total_minutes,
                block_minutes=request.block_minutes,
                preferred_start_hour=request.preferred_start_hour,
                preferred_end_hour=request.preferred_end_hour,
                allow_weekends=request.allow_weekends,
                description=request.description,
            )

            created_blocks: list[SeriesPlanBlock] = []
            event_links: list[str] = []
            tz = ZoneInfo(self.settings.timezone)

            for block in preview.blocks:
                start_payload = {
                    "dateTime": block.start.astimezone(tz).isoformat(),
                    "timeZone": self.settings.timezone,
                }
                end_payload = {
                    "dateTime": block.end.astimezone(tz).isoformat(),
                    "timeZone": self.settings.timezone,
                }
                summary = f"[Series] {request.title}: блок {block.index + 1}"
                description_lines = [
                    f"Series: {request.title}",
                    f"Блок №{block.index + 1} з {len(preview.blocks)}",
                    f"Дедлайн: {request.deadline.astimezone(tz):%d.%m %H:%M}",
                ]
                if request.description:
                    description_lines.append(request.description)
                description_lines.append("Створено Calendar Assist.")
                description = "\n".join(description_lines)

                event = await self.calendar.create_event(
                    request.telegram_id,
                    summary=summary,
                    start=start_payload,
                    end=end_payload,
                    description=description,
                )
                event_links.append(event.html_link or "")

                self.plan_repo.add_block(
                    session,
                    plan_id=plan.id,
                    order_index=block.index,
                    label=summary,
                    scheduled_start=block.start,
                    scheduled_end=block.end,
                    calendar_event_id=event.id,
                )
                created_blocks.append(block)

            deadline_event_link = await self._ensure_deadline_reminder(plan, request, tz)

        return SeriesCommitResult(
            plan_id=plan.id,
            created_blocks=created_blocks,
            event_links=event_links,
            deadline_event_link=deadline_event_link,
        )

    @staticmethod
    def _filter_slots(
        slots: Iterable,
        allow_weekends: bool,
        deadline: datetime,
        limit: int,
    ) -> list:
        filtered: list = []
        for slot in slots:
            if slot.end > deadline:
                continue
            if not allow_weekends and slot.start.weekday() >= 5:
                continue
            filtered.append(slot)
            if len(filtered) >= limit:
                break
        return filtered

    async def _ensure_deadline_reminder(
        self,
        plan,
        request: SeriesPlanRequest,
        tz: ZoneInfo,
    ) -> str | None:
        reminder_start = request.deadline.astimezone(tz)
        reminder_end = reminder_start + timedelta(minutes=30)
        summary = f"[Series] Дедлайн: {request.title}"
        description_lines = [
            f"Series: {request.title}",
            "Нагадування про фінальний дедлайн.",
            "Створено Calendar Assist.",
        ]
        if request.description:
            description_lines.append(request.description)
        reminders = RemindersConfig.from_minutes(30)
        try:
            event = await self.calendar.create_event(
                request.telegram_id,
                summary=summary,
                start={"dateTime": reminder_start.isoformat(), "timeZone": self.settings.timezone},
                end={"dateTime": reminder_end.isoformat(), "timeZone": self.settings.timezone},
                description="\n".join(description_lines),
                reminders=reminders,
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("Не вдалося створити нагадування про дедлайн серії %s: %s", plan.id, exc)
            return None
        return event.html_link

