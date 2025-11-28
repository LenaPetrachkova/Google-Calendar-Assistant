"""Intent Router для централізованої обробки інтентів."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes

from app.bot.context import ServiceContainer
from app.services.gemini import GeminiAnalysisResult

logger = logging.getLogger(__name__)


IntentHandler = Callable[
    [Update, ContextTypes.DEFAULT_TYPE, ServiceContainer, GeminiAnalysisResult, str],
    Awaitable[bool],
]


class IntentRouter:

    def __init__(self) -> None:
        self.handlers: dict[str, IntentHandler] = {}

    def register(self, intent: str, handler: IntentHandler) -> None:
        self.handlers[intent] = handler
        logger.debug("Зареєстровано handler для інтенту: %s", intent)

    async def route(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        services: ServiceContainer,
        analysis: GeminiAnalysisResult,
        original_text: str,
    ) -> bool:
        normalized_analysis = self._normalize_metadata(analysis)

        handler = self.handlers.get(normalized_analysis.intent)
        if not handler:
            logger.debug("Handler для інтенту '%s' не знайдено", normalized_analysis.intent)
            return False

        try:
            handled = await handler(update, context, services, normalized_analysis, original_text)
            if handled:
                logger.debug("Інтент '%s' оброблено handler'ом", normalized_analysis.intent)
            return handled
        except Exception as exc:
            logger.exception("Помилка в handler для інтенту '%s': %s", normalized_analysis.intent, exc)
            return False

    def _normalize_metadata(self, analysis: GeminiAnalysisResult) -> GeminiAnalysisResult:
        if not analysis.metadata:
            return analysis

        normalized_metadata: dict[str, Any] = {}

        event_query = analysis.metadata.get("event_query")
        if isinstance(event_query, dict):
            normalized_metadata["event_query"] = {
                "keywords": event_query.get("keywords") or "",
                "date": event_query.get("date"),
            }
        elif event_query is None and analysis.intent in ("event_lookup", "event_delete", "event_update"):
            normalized_metadata["event_query"] = {"keywords": "", "date": None}

        event_update = analysis.metadata.get("event_update")
        if isinstance(event_update, dict):
            normalized_metadata["event_update"] = {
                "title": event_update.get("title"),
                "date": event_update.get("date"),
                "start_time": event_update.get("start_time"),
                "end_time": event_update.get("end_time"),
                "duration_minutes": event_update.get("duration_minutes"),
                "shift_minutes": event_update.get("shift_minutes"),
                "add_meet": bool(event_update.get("add_meet", False)),
                "remove_meet": bool(event_update.get("remove_meet", False)),
                "category": event_update.get("category"),
                "reminder_minutes": event_update.get("reminder_minutes"),
            }

        free_slot = analysis.metadata.get("free_slot")
        if isinstance(free_slot, dict):
            normalized_metadata["free_slot"] = {
                "date_from": free_slot.get("date_from"),
                "date_to": free_slot.get("date_to"),
                "duration_minutes": free_slot.get("duration_minutes"),
                "preferred_window": free_slot.get("preferred_window", "any"),
            }
        if "date_from" in analysis.metadata or "date_to" in analysis.metadata:
            normalized_metadata["free_slot"] = {
                "date_from": analysis.metadata.get("date_from"),
                "date_to": analysis.metadata.get("date_to"),
                "duration_minutes": analysis.metadata.get("duration_minutes"),
                "preferred_window": analysis.metadata.get("preferred_window", "any"),
            }

        agenda = analysis.metadata.get("agenda")
        if isinstance(agenda, dict):
            normalized_metadata["agenda"] = {
                "date": agenda.get("date"),
                "time_window": agenda.get("time_window", "full"),
            }

        series_plan = analysis.metadata.get("series_plan")
        if isinstance(series_plan, dict):
            normalized_metadata["series_plan"] = {
                "title": series_plan.get("title"),
                "deadline": series_plan.get("deadline"),
                "total_hours": series_plan.get("total_hours"),
                "block_minutes": series_plan.get("block_minutes"),
                "preferred_window": series_plan.get("preferred_window", "any"),
                "allow_weekends": bool(series_plan.get("allow_weekends", False)),
            }

        for key, value in analysis.metadata.items():
            if key not in normalized_metadata:
                normalized_metadata[key] = value

        return GeminiAnalysisResult(
            intent=analysis.intent,
            confidence=analysis.confidence,
            reply=analysis.reply,
            event=analysis.event,
            metadata=normalized_metadata,
        )


async def _create_event_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
    original_text: str,
) -> bool:
    from app.bot.events import handle_create_event

    expecting_event = context.user_data.get("expecting_event", False)
    if not analysis.event and not expecting_event:
        return False

    await handle_create_event(update, context, services, analysis, original_text)
    return True


async def _event_update_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
    original_text: str,
) -> bool:
    from app.bot.events import handle_event_update

    await handle_event_update(update, context, services, analysis, original_text)
    return True


async def _event_delete_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
    original_text: str,
) -> bool:
    from app.bot.events import handle_event_delete

    await handle_event_delete(update, context, services, analysis)
    return True


async def _event_lookup_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
    original_text: str,
) -> bool:
    from app.bot.events import handle_event_lookup

    await handle_event_lookup(update, context, services, analysis, original_text)
    return True


async def _agenda_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
    original_text: str,
) -> bool:
    from app.bot.events import handle_agenda

    await handle_agenda(update, context, services, analysis, original_text)
    return True


async def _free_slot_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
    original_text: str,
) -> bool:
    from app.bot.free_slots import handle_free_slots as _handle_free_slots
    from app.bot.context import FREE_SLOT_EXPECTATION_KEY

    expecting_window = context.user_data.get(FREE_SLOT_EXPECTATION_KEY, False)
    if expecting_window or analysis.intent == "find_free_slot":
        await _handle_free_slots(update, context, services, analysis, original_text)
        return True
    return False


async def _habit_setup_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
    original_text: str,
) -> bool:
    message = update.effective_message
    await message.reply_text(
        "Можемо налаштувати звичку через /habit. Напиши коротко: 'Хочу читати по 30 хвилин 4 рази на тиждень'."
    )
    return True


async def _series_plan_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
    original_text: str,
) -> bool:
    from app.bot.series import handle_series_intent

    metadata = analysis.metadata.get("series_plan") if analysis.metadata else None
    await handle_series_intent(update, context, services, metadata or analysis.metadata or {})
    return True


async def _analytics_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
    original_text: str,
) -> bool:
    from app.bot.analytics import handle_analytics_intent

    await handle_analytics_intent(update, context, services)
    return True


async def _small_talk_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: ServiceContainer,
    analysis: GeminiAnalysisResult,
    original_text: str,
) -> bool:
    message = update.effective_message
    await message.reply_text(analysis.reply)
    return True


def create_router() -> IntentRouter:
    router = IntentRouter()
    router.register("create_event", _create_event_handler)
    router.register("event_update", _event_update_handler)
    router.register("event_delete", _event_delete_handler)
    router.register("event_lookup", _event_lookup_handler)
    router.register("agenda_day", _agenda_handler)
    router.register("find_free_slot", _free_slot_handler)
    router.register("habit_setup", _habit_setup_handler)
    router.register("series_plan", _series_plan_handler)
    router.register("analytics_overview", _analytics_handler)
    router.register("productivity_report", _analytics_handler)
    router.register("small_talk", _small_talk_handler)

    logger.info("IntentRouter створено та налаштовано з %d handlers", len(router.handlers))
    return router

