"""Запуск Telegram-бота Calendar Assist."""
from __future__ import annotations

import logging
import sys

from telegram import BotCommand
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.bot.handlers import (
    fallback,
    handle_callback_query,
    help_command,
    list_events,
    start,
    window_command,
)
from app.bot.analytics import insights_command
from app.bot.series import series_start_command
from app.bot.context import ServiceContainer
from app.bot.habits import (
    HABIT_DURATION,
    HABIT_FREQUENCY,
    HABIT_NAME,
    habit_cancel,
    habit_set_duration,
    habit_set_frequency,
    habit_set_name,
    habit_start,
)
from app.config.settings import get_settings
from app.db.repository import HabitRepository, SeriesPlanRepository, UserRepository
from app.services.free_slots import FreeSlotService
from app.services.analytics import AnalyticsService
from app.services.gemini import GeminiService
from app.services.google_calendar import GoogleCalendarService
from app.services.habit_planner import HabitPlannerService
from app.services.series_planner import SeriesPlannerService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def _post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Запуск бота"),
            BotCommand("help", "Список можливостей"),
            BotCommand("events", "Найближчі події"),
            BotCommand("window", "Знайти вільний час"),
            BotCommand("habit", "Налаштувати звичку"),
            BotCommand("insights", "Коротка аналітика тижня"),
            BotCommand("plan", "Розкласти задачу на блоки"),
        ]
    )


def build_application() -> Application:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN відсутній у .env")

    user_repo = UserRepository()
    habit_repo = HabitRepository()
    calendar_service = GoogleCalendarService(settings=settings)
    gemini_service = GeminiService(settings)
    habit_planner = HabitPlannerService(
        calendar_service=calendar_service,
        habit_repository=habit_repo,
        user_repository=user_repo,
        settings=settings,
    )
    free_slot_service = FreeSlotService(calendar_service=calendar_service, settings=settings)
    analytics_service = AnalyticsService(calendar_service=calendar_service, settings=settings)
    series_planner = SeriesPlannerService(
        calendar_service=calendar_service,
        free_slot_service=free_slot_service,
        user_repository=user_repo,
        plan_repository=SeriesPlanRepository(),
        settings=settings,
    )

    services = ServiceContainer(
        settings=settings,
        gemini=gemini_service,
        calendar=calendar_service,
        user_repo=user_repo,
        habit_repo=habit_repo,
        habit_planner=habit_planner,
        free_slot_service=free_slot_service,
        analytics=analytics_service,
        series_planner=series_planner,
    )

    application = ApplicationBuilder().token(settings.telegram_bot_token).post_init(_post_init).build()
    application.bot_data["services"] = services

    habit_conv = ConversationHandler(
        entry_points=[CommandHandler("habit", habit_start)],
        states={
            HABIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, habit_set_name)],
            HABIT_FREQUENCY: [MessageHandler(filters.TEXT & ~filters.COMMAND, habit_set_frequency)],
            HABIT_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, habit_set_duration)],
        },
        fallbacks=[CommandHandler("cancel", habit_cancel)],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("events", list_events))
    application.add_handler(CommandHandler("window", window_command))
    application.add_handler(CommandHandler("insights", insights_command))
    application.add_handler(CommandHandler("plan", series_start_command))
    application.add_handler(habit_conv)
    
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, fallback)
    )
    return application


def main() -> None:
    application = build_application()
    logger.info("Запускаю Telegram-бота…")
    application.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()
