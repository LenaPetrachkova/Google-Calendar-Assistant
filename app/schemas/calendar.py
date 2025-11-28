from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class ReminderOverride:
    minutes: int
    method: str = "popup"

    def __post_init__(self) -> None:
        if self.minutes < 0:
            raise ValueError("reminder_minutes_negative")
        if self.method not in {"popup", "email"}:
            raise ValueError("reminder_method_invalid")

    def to_api(self) -> dict[str, Any]:
        return {"method": self.method, "minutes": int(self.minutes)}

    @classmethod
    def from_api(cls, payload: dict[str, Any] | None) -> "ReminderOverride":
        data = payload or {}
        return cls(
            minutes=int(data.get("minutes", 0)),
            method=data.get("method", "popup"),
        )


@dataclass(slots=True)
class RemindersConfig:
    overrides: list[ReminderOverride] = field(default_factory=list)
    use_default: bool = False

    def to_api(self) -> dict[str, Any]:
        if self.use_default:
            return {"useDefault": True}
        return {
            "useDefault": False,
            "overrides": [override.to_api() for override in self.overrides],
        }

    def first_override_minutes(self) -> int | None:
        if self.use_default:
            return None
        return self.overrides[0].minutes if self.overrides else None

    @classmethod
    def from_minutes(cls, minutes: int | None) -> "RemindersConfig | None":
        if minutes is None:
            return None
        if minutes <= 0:
            return cls(overrides=[], use_default=False)
        return cls(overrides=[ReminderOverride(minutes=int(minutes))])

    @classmethod
    def from_api(cls, payload: dict[str, Any] | None) -> "RemindersConfig | None":
        if not payload:
            return None
        if payload.get("useDefault"):
            return cls(use_default=True)
        overrides = [ReminderOverride.from_api(item) for item in payload.get("overrides", [])]
        return cls(overrides=overrides, use_default=False)


@dataclass(slots=True)
class EventDraft:
    summary: str
    start: dict[str, Any]
    end: dict[str, Any]
    description: str | None = None
    location: str | None = None
    recurrence: list[str] | None = None
    conference_data: dict[str, Any] | None = None
    color_id: str | None = None
    reminders: RemindersConfig | None = None

    def __post_init__(self) -> None:
        if not self.summary or not self.summary.strip():
            raise ValueError("summary_required")
        self._validate_datetime_payload(self.start, "start")
        self._validate_datetime_payload(self.end, "end")

    @staticmethod
    def _validate_datetime_payload(payload: dict[str, Any], field_name: str) -> None:
        if not isinstance(payload, dict):
            raise ValueError(f"{field_name}_payload_invalid")
        date_time = payload.get("dateTime")
        time_zone = payload.get("timeZone")
        if not date_time:
            raise ValueError(f"{field_name}_datetime_missing")
        if not time_zone:
            raise ValueError(f"{field_name}_timezone_missing")
        try:
            datetime.fromisoformat(date_time)
        except ValueError as exc:  # pragma: no cover - defensive guard
            raise ValueError(f"{field_name}_datetime_invalid") from exc

    def to_calendar_kwargs(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "summary": self.summary,
            "start": self.start,
            "end": self.end,
        }
        if self.description:
            payload["description"] = self.description
        if self.location:
            payload["location"] = self.location
        if self.recurrence:
            payload["recurrence"] = self.recurrence
        if self.conference_data:
            payload["conference_data"] = self.conference_data
        if self.color_id:
            payload["color_id"] = self.color_id
        if self.reminders is not None:
            payload["reminders"] = self.reminders
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EventDraft":
        return cls(
            summary=data.get("summary") or "(без назви)",
            start=dict(data.get("start") or {}),
            end=dict(data.get("end") or {}),
            description=data.get("description"),
            location=data.get("location"),
            recurrence=data.get("recurrence"),
            conference_data=data.get("conference_data"),
            color_id=data.get("color_id"),
            reminders=data.get("reminders") if isinstance(data.get("reminders"), RemindersConfig) else RemindersConfig.from_api(data.get("reminders")),
        )


@dataclass(slots=True)
class EventUpdatePayload:
    patch: dict[str, Any]
    add_meet: bool = False
    remove_meet: bool = False
    color_id: str | None = None
    reminder_minutes: int | None = None

    def has_effect(self) -> bool:
        return (
            bool(self.patch)
            or self.add_meet
            or self.remove_meet
            or self.color_id is not None
            or self.reminder_minutes is not None
        )

    def to_storage(self) -> dict[str, Any]:
        return {
            "patch": dict(self.patch),
            "add_meet": self.add_meet,
            "remove_meet": self.remove_meet,
            "color_id": self.color_id,
            "reminder_minutes": self.reminder_minutes,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "EventUpdatePayload":
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, dict):
            return cls(patch={})

        patch = cls._extract_patch(raw)
        return cls(
            patch=patch,
            add_meet=bool(raw.get("add_meet", raw.get("add_meet_requested", False))),
            remove_meet=bool(raw.get("remove_meet", raw.get("remove_meet_requested", False))),
            color_id=raw.get("color_id"),
            reminder_minutes=cls._safe_int(raw.get("reminder_minutes")),
        )

    @staticmethod
    def _extract_patch(raw: dict[str, Any]) -> dict[str, Any]:
        for key in ("patch", "update_data", "update"):
            candidate = raw.get(key)
            if isinstance(candidate, dict):
                return dict(candidate)
        excluded = {
            "event_id",
            "add_meet",
            "remove_meet",
            "color_id",
            "reminder_minutes",
            "original_event",
            "conflict",
        }
        return {k: v for k, v in raw.items() if k not in excluded}

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


@dataclass(slots=True)
class CalendarEvent:
    id: str
    summary: str
    start: dict[str, Any]
    end: dict[str, Any]
    description: str | None
    location: str | None
    html_link: str | None
    hangout_link: str | None
    reminders: RemindersConfig | None
    color_id: str | None
    raw: dict[str, Any]

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "CalendarEvent":
        return cls(
            id=payload.get("id", ""),
            summary=payload.get("summary", "(без назви)"),
            start=dict(payload.get("start") or {}),
            end=dict(payload.get("end") or {}),
            description=payload.get("description"),
            location=payload.get("location"),
            html_link=payload.get("htmlLink"),
            hangout_link=payload.get("hangoutLink"),
            reminders=RemindersConfig.from_api(payload.get("reminders")),
            color_id=payload.get("colorId"),
            raw=payload,
        )

    def as_dict(self) -> dict[str, Any]:
        return self.raw

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)


