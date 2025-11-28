from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class User(Base):   

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(unique=True, index=True)
    google_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    credentials_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    habits: Mapped[list["Habit"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    series_plans: Mapped[list["SeriesPlan"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Habit(Base):

    __tablename__ = "habits"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    preferred_time_of_day: Mapped[str | None] = mapped_column(String(20), nullable=True)
    target_sessions_per_week: Mapped[int] = mapped_column(Integer, default=3)
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped[User] = relationship(back_populates="habits")
    sessions: Mapped[list["HabitSession"]] = relationship(back_populates="habit", cascade="all, delete-orphan")


class HabitSession(Base):

    __tablename__ = "habit_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    habit_id: Mapped[int] = mapped_column(ForeignKey("habits.id", ondelete="CASCADE"))
    calendar_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="planned")
    completion_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    check_in_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    habit: Mapped[Habit] = relationship(back_populates="sessions")


class SeriesPlan(Base):

    __tablename__ = "series_plans"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    total_minutes: Mapped[int] = mapped_column(Integer)
    block_minutes: Mapped[int] = mapped_column(Integer)
    preferred_start_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preferred_end_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allow_weekends: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="planned")
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user: Mapped[User] = relationship(back_populates="series_plans")
    blocks: Mapped[list["SeriesBlock"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="SeriesBlock.order_index",
    )


class SeriesBlock(Base):

    __tablename__ = "series_blocks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("series_plans.id", ondelete="CASCADE"))
    order_index: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(255))
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    calendar_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    plan: Mapped[SeriesPlan] = relationship(back_populates="blocks")
