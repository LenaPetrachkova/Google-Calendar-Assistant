from __future__ import annotations

import asyncio
import json
from typing import Any, Iterable
from datetime import datetime
from uuid import uuid4

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config.settings import Settings, get_settings
from app.db.base import init_db
from app.db.repository import UserRepository, get_session
from app.schemas.calendar import CalendarEvent, RemindersConfig, ReminderOverride
from app.services.async_executor import run_in_executor

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
USERINFO_SERVICE = ("oauth2", "v2")


class GoogleCalendarService:

    def __init__(
        self,
        settings: Settings | None = None,
        user_repository: UserRepository | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.user_repository = user_repository or UserRepository()
        init_db()


    def ensure_credentials(self, telegram_id: int) -> Credentials:
        with get_session() as session:
            user = self.user_repository.get_by_telegram_id(session, telegram_id)
            if user and user.credentials_json:
                credentials = self._load_credentials(user.credentials_json)
                if credentials and credentials.expired and credentials.refresh_token:
                    credentials.refresh(Request())
                    self._store_credentials(session, telegram_id, user.google_email or "", credentials)
                if credentials:
                    return credentials

        credentials, email = self._run_local_oauth_flow()
        with get_session() as session:
            self._store_credentials(session, telegram_id, email, credentials)
        return credentials

    def get_calendar_client(self, telegram_id: int):
        credentials = self.ensure_credentials(telegram_id)
        return build("calendar", "v3", credentials=credentials)

    async def list_upcoming_events(
        self, telegram_id: int, *, max_results: int = 5
    ) -> list[CalendarEvent]:
        def _sync() -> list[CalendarEvent]:
            from zoneinfo import ZoneInfo
            service = self.get_calendar_client(telegram_id)
            now = datetime.now(ZoneInfo(self.settings.timezone))
            result = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=now.isoformat(),
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            return [CalendarEvent.from_api(item) for item in result.get("items", [])]
        
        return await run_in_executor(_sync)

    async def list_events_between(
        self,
        telegram_id: int,
        start: datetime,
        end: datetime,
        *,
        max_results: int = 50,
    ) -> list[CalendarEvent]:
        def _sync() -> list[CalendarEvent]:
            service = self.get_calendar_client(telegram_id)
            result = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=max_results,
                )
                .execute()
            )
            return [CalendarEvent.from_api(item) for item in result.get("items", [])]
        
        return await run_in_executor(_sync)

    async def search_events(
        self,
        telegram_id: int,
        query: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        max_results: int = 10,
    ) -> list[CalendarEvent]:
        def _sync() -> list[CalendarEvent]:
            service = self.get_calendar_client(telegram_id)
            params: dict[str, Any] = {
                "calendarId": "primary",
                "q": query,
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": max_results,
            }
            if start is not None:
                params["timeMin"] = start.isoformat()
            if end is not None:
                params["timeMax"] = end.isoformat()
            result = service.events().list(**params).execute()
            return [CalendarEvent.from_api(item) for item in result.get("items", [])]
        
        return await run_in_executor(_sync)

    async def create_event(
        self,
        telegram_id: int,
        *,
        summary: str,
        start: dict[str, Any],
        end: dict[str, Any],
        description: str | None = None,
        recurrence: list[str] | None = None,
        conference_data: dict[str, Any] | None = None,
        color_id: str | None = None,
        reminders: RemindersConfig | Iterable[ReminderOverride] | list[dict[str, Any]] | None = None,
        **extra: Any,
    ) -> CalendarEvent:
        def _sync() -> CalendarEvent:
            service = self.get_calendar_client(telegram_id)
            body: dict[str, Any] = {
                "summary": summary,
                "start": start,
                "end": end,
            }
            if description:
                body["description"] = description
            if recurrence:
                body["recurrence"] = recurrence
            if conference_data:
                body["conferenceData"] = conference_data
            if color_id:
                body["colorId"] = color_id
            reminders_payload = self._prepare_reminders_payload(reminders)
            if reminders_payload is not None:
                body["reminders"] = reminders_payload
            body.update(extra)
            insert_kwargs: dict[str, Any] = {}
            if conference_data:
                insert_kwargs["conferenceDataVersion"] = 1
            created_raw = (
                service.events()
                .insert(calendarId="primary", body=body, **insert_kwargs)
                .execute()
            )
            return CalendarEvent.from_api(created_raw)
        
        return await run_in_executor(_sync)

    async def update_event(
        self,
        telegram_id: int,
        event_id: str,
        *,
        summary: str | None = None,
        start: dict[str, Any] | None = None,
        end: dict[str, Any] | None = None,
        description: str | None = None,
        recurrence: list[str] | None = None,
        conference_data: dict[str, Any] | None = None,
        remove_conference: bool = False,
        color_id: str | None = None,
        reminders: RemindersConfig | Iterable[ReminderOverride] | list[dict[str, Any]] | None = None,
        clear_reminders: bool = False,
        **extra: Any,
    ) -> CalendarEvent:
        def _sync() -> CalendarEvent:
            service = self.get_calendar_client(telegram_id)
            event = service.events().get(calendarId="primary", eventId=event_id).execute()

            if summary is not None:
                event["summary"] = summary
            if start is not None:
                event["start"] = start
            if end is not None:
                event["end"] = end
            if description is not None:
                event["description"] = description
            if recurrence is not None:
                event["recurrence"] = recurrence

            if remove_conference:
                event.pop("conferenceData", None)
            elif conference_data is not None:
                event["conferenceData"] = conference_data

            if color_id is not None:
                event["colorId"] = color_id

            if clear_reminders:
                event["reminders"] = {"useDefault": False, "overrides": []}
            else:
                reminders_payload = self._prepare_reminders_payload(reminders)
                if reminders_payload is not None:
                    event["reminders"] = reminders_payload

            event.update(extra)

            update_kwargs: dict[str, Any] = {}
            if conference_data is not None or remove_conference:
                update_kwargs["conferenceDataVersion"] = 1

            updated_raw = (
                service.events()
                .update(calendarId="primary", eventId=event_id, body=event, **update_kwargs)
                .execute()
            )
            return CalendarEvent.from_api(updated_raw)
        
        return await run_in_executor(_sync)

    def build_conference_data(self) -> dict[str, Any]:
        return {
            "createRequest": {
                "requestId": uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }

    async def delete_event(
        self,
        telegram_id: int,
        event_id: str,
    ) -> None:
        def _sync() -> None:
            service = self.get_calendar_client(telegram_id)
            service.events().delete(calendarId="primary", eventId=event_id).execute()
        
        await run_in_executor(_sync)

    async def get_event(
        self,
        telegram_id: int,
        event_id: str,
    ) -> CalendarEvent:
        def _sync() -> CalendarEvent:
            service = self.get_calendar_client(telegram_id)
            data = service.events().get(calendarId="primary", eventId=event_id).execute()
            return CalendarEvent.from_api(data)
        
        return await run_in_executor(_sync)

    def _prepare_reminders_payload(
        self,
        reminders: RemindersConfig | Iterable[ReminderOverride] | list[dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        if reminders is None:
            return None
        if isinstance(reminders, RemindersConfig):
            return reminders.to_api()
        overrides: list[dict[str, Any]] = []
        for item in reminders:
            if isinstance(item, ReminderOverride):
                overrides.append(item.to_api())
            elif isinstance(item, dict):
                overrides.append(item)
        return {"useDefault": False, "overrides": overrides}

    def _run_local_oauth_flow(self) -> tuple[Credentials, str]:
        flow = InstalledAppFlow.from_client_config(self._client_config, SCOPES)
        credentials = flow.run_local_server(
            port=self.settings.google_oauth_port,
            prompt="consent",
        )
        email = self._fetch_google_email(credentials)
        return credentials, email

    def _fetch_google_email(self, credentials: Credentials) -> str:
        try:
            service = build(*USERINFO_SERVICE, credentials=credentials)
            profile = service.userinfo().get().execute()
            return profile.get("email", "")
        except HttpError:
            return ""

    def _store_credentials(
        self,
        session,
        telegram_id: int,
        google_email: str,
        credentials: Credentials,
    ) -> None:
        self.user_repository.create_or_update_credentials(
            session,
            telegram_id=telegram_id,
            google_email=google_email,
            credentials_json=credentials.to_json(),
        )

    def _load_credentials(self, credentials_json: str) -> Credentials | None:
        try:
            data = json.loads(credentials_json)
        except json.JSONDecodeError:
            return None
        return Credentials.from_authorized_user_info(data, scopes=SCOPES)

    @property
    def _client_config(self) -> dict[str, Any]:
        return {
            "installed": {
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "auth_uri": AUTH_URI,
                "token_uri": TOKEN_URI,
                "project_id": self.settings.google_project_id,
                "redirect_uris": [f"http://localhost:{self.settings.google_oauth_port}/"],
            }
        }
