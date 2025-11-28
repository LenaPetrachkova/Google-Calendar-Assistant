from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import SessionLocal
from app.db.models import Habit, HabitSession, SeriesBlock, SeriesPlan, User


@contextmanager
def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class UserRepository:

    def get_by_telegram_id(self, session: Session, telegram_id: int) -> User | None:
        return session.scalar(select(User).where(User.telegram_id == telegram_id))

    def create_or_update_credentials(
        self,
        session: Session,
        telegram_id: int,
        google_email: str,
        credentials_json: str,
    ) -> User:
        user = self.get_by_telegram_id(session, telegram_id)
        if user is None:
            user = User(
                telegram_id=telegram_id,
                google_email=google_email,
                credentials_json=credentials_json,
            )
            session.add(user)
        else:
            user.google_email = google_email
            user.credentials_json = credentials_json
        session.flush()
        return user


class HabitRepository:

    def list_habits(self, session: Session, user_id: int) -> list[Habit]:
        return session.scalars(
            select(Habit).where(Habit.user_id == user_id, Habit.is_active.is_(True))
        ).all()

    def create_habit(
        self,
        session: Session,
        user_id: int,
        name: str,
        duration_minutes: int,
        preferred_time_of_day: str | None,
        target_sessions_per_week: int,
        start_date: datetime | None = None,
    ) -> Habit:
        habit = Habit(
            user_id=user_id,
            name=name,
            duration_minutes=duration_minutes,
            preferred_time_of_day=preferred_time_of_day,
            target_sessions_per_week=target_sessions_per_week,
            start_date=start_date,
        )
        session.add(habit)
        session.flush()
        return habit

    def add_session(
        self,
        session: Session,
        habit_id: int,
        scheduled_start: datetime,
        scheduled_end: datetime,
        calendar_event_id: str | None = None,
    ) -> HabitSession:
        item = HabitSession(
            habit_id=habit_id,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            calendar_event_id=calendar_event_id,
        )
        session.add(item)
        session.flush()
        return item

    def mark_session_status(
        self,
        session: Session,
        session_id: int,
        status: str,
        note: str | None = None,
    ) -> HabitSession | None:
        record = session.get(HabitSession, session_id)
        if not record:
            return None
        record.status = status
        record.check_in_note = note
        record.completion_time = datetime.utcnow() if status == "completed" else None
        session.flush()
        return record

    def upcoming_sessions(self, session: Session, user_id: int, within_days: int = 7) -> list[HabitSession]:
        cutoff = datetime.utcnow() + timedelta(days=within_days)
        return session.scalars(
            select(HabitSession)
            .join(Habit)
            .where(
                Habit.user_id == user_id,
                HabitSession.scheduled_start >= datetime.utcnow(),
                HabitSession.scheduled_start <= cutoff,
            )
            .order_by(HabitSession.scheduled_start)
        ).all()


class SeriesPlanRepository:

    def create_plan(
        self,
        session: Session,
        *,
        user_id: int,
        title: str,
        deadline: datetime,
        total_minutes: int,
        block_minutes: int,
        preferred_start_hour: int | None,
        preferred_end_hour: int | None,
        allow_weekends: bool,
        description: str | None = None,
        metadata_json: str | None = None,
    ) -> SeriesPlan:
        plan = SeriesPlan(
            user_id=user_id,
            title=title,
            deadline=deadline,
            total_minutes=total_minutes,
            block_minutes=block_minutes,
            preferred_start_hour=preferred_start_hour,
            preferred_end_hour=preferred_end_hour,
            allow_weekends=allow_weekends,
            description=description,
            metadata_json=metadata_json,
        )
        session.add(plan)
        session.flush()
        return plan

    def add_block(
        self,
        session: Session,
        *,
        plan_id: int,
        order_index: int,
        label: str,
        scheduled_start: datetime,
        scheduled_end: datetime,
        calendar_event_id: str | None,
        status: str = "scheduled",
        notes: str | None = None,
    ) -> SeriesBlock:
        block = SeriesBlock(
            plan_id=plan_id,
            order_index=order_index,
            label=label,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            calendar_event_id=calendar_event_id,
            status=status,
            notes=notes,
        )
        session.add(block)
        session.flush()
        return block

    def mark_block_status(
        self,
        session: Session,
        block_id: int,
        status: str,
        notes: str | None = None,
    ) -> SeriesBlock | None:
        block = session.get(SeriesBlock, block_id)
        if not block:
            return None
        block.status = status
        if notes is not None:
            block.notes = notes
        session.flush()
        return block

    def get_plan(self, session: Session, plan_id: int, user_id: int) -> SeriesPlan | None:
        return session.scalar(
            select(SeriesPlan).where(SeriesPlan.id == plan_id, SeriesPlan.user_id == user_id)
        )
