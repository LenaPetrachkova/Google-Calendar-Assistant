from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import google.generativeai as genai

from app.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = (
    "Ти — асистент планування Calendar Assist. Аналізуй повідомлення українською. "
    "Підтримувані наміри: create_event, list_events, find_free_slot, habit_setup, agenda_day, event_lookup, event_update, event_delete, productivity_report, analytics_overview, series_plan, small_talk. "
    "Відповідай ТІЛЬКИ JSON. Поле assistant_reply — коротка відповідь українською у нейтральному стилі. "
    "У полі event вказуй дані якщо потрібно створити подію. Якщо подія повторювана (щодня, щотижня, щомісяця), додай поле recurrence: 'daily', 'weekly', 'monthly'. "
    "Поле event.category (один з work, study, meeting, personal, health, sport, hobby, travel, focus, other) обов'язкове — обери найближчу категорію за змістом. "
    "Якщо користувач просить онлайн-формат ('Google Meet', 'дзвінок', 'онлайн', 'потрібно посилання'), встанови event.needs_meet=true. "
    "У free_slot описуй параметри для пошуку вільного часу. "
    "Якщо користувач просить розклад, повертай intent agenda_day та поля agenda. Для пошуку події за назвою використовуй intent event_lookup та поля event_query. "
    "Для видалення події використовуй intent event_delete та поле event_query з назвою події (ключові слова з запиту: 'видали', 'скасуй', 'прибери', 'cancel'). "
    "Для редагування події використовуй intent event_update, у event_query вказуй назву/опис події, а у event_update — що саме змінити (title, date, start_time, end_time, duration_minutes, category, reminder_minutes, add_meet, remove_meet). "
    "Якщо користувач просить аналіз продуктивності ('як я справляюсь', 'покажи статистику'), використовуй intent=analytics_overview (аналог productivity_report). "
    "Якщо користувач просить спланувати серію блоків під великий дедлайн ('допоможи підготуватися до іспиту', 'розбий диплом на кроки'), повертай intent=series_plan та передавай деталі у полі series_plan (назва, дедлайн, сумарні години, бажаний слот дня, чи дозволені вихідні). "
    "Фрази на кшталт 'перенеси', 'пересунь', 'зміни час', 'зроби пізніше/раніше' мають повертати intent=event_update; якщо згадується 'на 2 години пізніше' (або подібні вирази), зафіксуй поле shift_minutes у хвилинах (позитивне значення — пізніше, негативне — раніше). "
    "Якщо користувач називає точний час (наприклад, 'о 16:30'), обов'язково заповнюй event_update.start_time у форматі HH:MM. "
    "Фрази 'додай meet', 'створи посилання', 'видали meet' повинні встановлювати відповідно add_meet=true або remove_meet=true у event_update. "
    "Час у форматі HH:MM (24h), дата — YYYY-MM-DD."
)


@dataclass(slots=True)
class EventProposal:

    title: str | None = None
    date: str | None = None  # ISO date (YYYY-MM-DD)
    start_time: str | None = None  # HH:MM (24h)
    end_time: str | None = None  # HH:MM (24h)
    duration_minutes: int | None = None
    recurrence: str | None = None  # "weekly", "daily"
    location: str | None = None
    notes: str | None = None
    needs_meet: bool = False
    category: str | None = None
    reminder_minutes: int | None = None


@dataclass(slots=True)
class GeminiAnalysisResult:

    intent: str
    confidence: float
    reply: str
    event: EventProposal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class GeminiService:

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.gemini_api_key:
            raise RuntimeError(
                "Gemini API key is missing"
            )
        genai.configure(api_key=self.settings.gemini_api_key)
        self.model = genai.GenerativeModel(
            self.settings.gemini_model,
            system_instruction=SYSTEM_PROMPT,
        )


    def analyze_user_message(self, message: str) -> GeminiAnalysisResult:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        prompt_text = self._build_prompt_text(message, now)
        response = self.model.generate_content(
            [{"role": "user", "parts": [{"text": prompt_text}]}]
        )
        raw_text = response.text or ""
        logger.debug("Gemini raw response: %s", raw_text)

        payload = self._extract_json(raw_text)
        if payload is None:
            logger.warning("Gemini повернув не-JSON відповідь, використовую fallback")
            return GeminiAnalysisResult(
                intent="small_talk",
                confidence=0.1,
                reply=raw_text or "Не вдалося зрозуміти запит. Спробуйте сформулювати інакше.",
            )

        intent = payload.get("intent", "unknown")
        confidence = float(payload.get("confidence", 0))
        reply = payload.get("assistant_reply") or "Прийнято."
        event = None
        if payload.get("event"):
            event = self._parse_event(payload["event"])

        metadata = {k: v for k, v in payload.items() if k not in {"intent", "confidence", "assistant_reply", "event"}}

        free_slot = metadata.pop("free_slot", None)
        if isinstance(free_slot, dict):
            metadata.update({k: v for k, v in free_slot.items() if v is not None})

        agenda = metadata.pop("agenda", None)
        if isinstance(agenda, dict):
            metadata["agenda"] = agenda

        event_query = metadata.pop("event_query", None)
        if isinstance(event_query, dict):
            metadata["event_query"] = event_query

        event_update = metadata.pop("event_update", None)
        if isinstance(event_update, dict):
            metadata["event_update"] = event_update

        logger.debug("Gemini parsed intent=%s event=%s metadata=%s", intent, event, metadata)
        return GeminiAnalysisResult(
            intent=intent,
            confidence=confidence,
            reply=reply,
            event=event,
            metadata=metadata,
        )


    def _build_prompt_text(self, message: str, now: datetime) -> str:
        current_date = now.date().isoformat()
        current_time = now.strftime("%H:%M")
        schema_hint = {
            "intent": "create_event | list_events | find_free_slot | habit_setup | agenda_day | event_lookup | event_update | event_delete | productivity_report | analytics_overview | series_plan | small_talk",
            "confidence": "float від 0 до 1",
            "assistant_reply": "Текст відповіді для користувача",
            "event": {
                "title": "Назва події (рядок)",
                "date": "YYYY-MM-DD або null",
                "start_time": "HH:MM або null",
                "end_time": "HH:MM або null",
                "duration_minutes": "int або null",
                "recurrence": "опис повторення або null",
                "location": "де відбувається",
                "notes": "будь-які уточнення",
                "needs_meet": "bool, true якщо потрібне Google Meet",
                "category": "work | study | meeting | personal | health | sport | hobby | travel | focus | other",
                "reminder_minutes": "int або null (наприклад 10, 30, 60)",
            },
            "free_slot": {
                "date_from": "YYYY-MM-DD або null",
                "date_to": "YYYY-MM-DD або null",
                "duration_minutes": "int",
                "preferred_window": "morning | day | evening | night | any",
            },
            "agenda": {
                "date": "YYYY-MM-DD або null",
                "time_window": "full | morning | day | evening | night",
            },
            "event_query": {
                "keywords": "рядок з ключовими словами",
                "date": "YYYY-MM-DD або null",
            },
            "event_update": {
                "title": "нова назва або null",
                "date": "нова дата YYYY-MM-DD або null",
                "start_time": "новий час HH:MM або null",
                "end_time": "новий час HH:MM або null",
                "duration_minutes": "нова тривалість або null",
                "shift_minutes": "зсув у хвилинах (+ пізніше, - раніше)",
                "add_meet": "bool, додати Google Meet",
                "remove_meet": "bool, прибрати Google Meet",
                "category": "нова категорія або null",
                "reminder_minutes": "оновити нагадування (int) або null",
            },
            "series_plan": {
                "title": "Назва задачі або null",
                "deadline": "YYYY-MM-DD або YYYY-MM-DD HH:MM",
                "total_hours": "float або int, скільки годин потрібно",
                "block_minutes": "тривалість одного блоку у хвилинах",
                "preferred_window": "morning | day | evening | any",
                "allow_weekends": "bool",
            },
        }
        instructions = (
            "Поточна дата: "
            + current_date
            + ", поточний час: "
            + current_time
            + ". Якщо користувач використовує слова на кшталт 'завтра', 'післязавтра', 'через два дні', 'наступного вівторка', "
            "обчисли конкретну дату у форматі YYYY-MM-DD. Якщо згадується тривалість, конвертуй її у хвилини. "
            "Якщо відсутня точна дата/час навіть після інтерпретації, залиш ці поля null. Для intent=find_free_slot обов'язково заповнюй free_slot.duration_minutes і, якщо можливо, date_from/date_to та preferred_window. "
            "Для agenda_day старайся вказувати конкретну дату і, якщо задано, часовий інтервал. Для event_lookup повертай ключові слова у field event_query.keywords. "
            "Для intent=event_update: якщо користувач описує зсув типу 'на 2 години пізніше/раніше', заповнюй event_update.shift_minutes у хвилинах; якщо названо точний час — event_update.start_time."
        )
        return (
            instructions
            + "\nСформуй JSON відповіді за схемою: "
            + json.dumps(schema_hint, ensure_ascii=False)
            + "\nПовідомлення користувача: "
            + message
        )

    @staticmethod
    def _extract_json(raw_text: str) -> dict[str, Any] | None:
        raw_text = raw_text.strip()
        if not raw_text:
            return None
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start == -1 or end == -1:
            return None
        snippet = raw_text[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            logger.exception("Не вдалося розпарсити JSON від Gemini: %s", snippet)
            return None

    @staticmethod
    def _parse_event(event_data: dict[str, Any]) -> EventProposal:
        return EventProposal(
            title=event_data.get("title"),
            date=event_data.get("date"),
            start_time=event_data.get("start_time"),
            end_time=event_data.get("end_time"),
            duration_minutes=_safe_int(event_data.get("duration_minutes")),
            recurrence=event_data.get("recurrence"),
            location=event_data.get("location"),
            notes=event_data.get("notes"),
            needs_meet=bool(event_data.get("needs_meet")),
            category=event_data.get("category"),
            reminder_minutes=_safe_int(event_data.get("reminder_minutes")),
        )


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
