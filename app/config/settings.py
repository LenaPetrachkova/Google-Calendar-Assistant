from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = BASE_DIR / ".env"

if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
else:
    load_dotenv(BASE_DIR / "app" / "config" / "env.example")


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    google_client_id: str
    google_client_secret: str
    google_project_id: str
    gemini_api_key: str
    gemini_model: str
    database_url: str
    google_oauth_port: int
    timezone: str = "Europe/Kyiv"


def get_settings() -> Settings:
    import os

    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
        google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        google_project_id=os.getenv("GOOGLE_PROJECT_ID", ""),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "models/gemini-2.5-flash"),
        database_url=os.getenv("DATABASE_URL", "sqlite:///calendarassist.db"),
        google_oauth_port=int(os.getenv("GOOGLE_OAUTH_PORT", "8080")),
        timezone=os.getenv("TZ", "Europe/Kyiv"),
    )
