"""Environment configuration shared by the Vercel webhook and the GitHub
Actions scraper job. Both platforms inject real environment variables
directly (Vercel project settings / GitHub Actions secrets); ``load_dotenv``
is only useful for local testing and is a harmless no-op otherwise.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _get_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_webhook_secret: str
    supabase_url: str
    supabase_service_key: str

    target_url: str = "https://tickets.rfef.es/"
    request_timeout_seconds: int = 20
    max_retries: int = 3

    open_alert_burst_count: int = 10
    open_alert_burst_interval_seconds: int = 180
    open_alert_burst_duration_seconds: int = 3600
    open_alert_message_delay_seconds: float = 1.5

    keywords: tuple[str, ...] = (
        "Supercopa",
        "Final",
        "Estambul",
        "Atatürk",
        "Ataturk",
        "Istanbul",
        "7 de febrero",
        "Entradas",
        "Tickets",
        "Comprar",
    )


def load_settings() -> Settings:
    return Settings(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        telegram_webhook_secret=_require("TELEGRAM_WEBHOOK_SECRET"),
        supabase_url=_require("SUPABASE_URL").rstrip("/"),
        supabase_service_key=_require("SUPABASE_SERVICE_KEY"),
        target_url=_get_str("TARGET_URL", "https://tickets.rfef.es/"),
        request_timeout_seconds=_get_int("REQUEST_TIMEOUT_SECONDS", 20),
        max_retries=_get_int("MAX_RETRIES", 3),
        open_alert_burst_count=_get_int("OPEN_ALERT_BURST_COUNT", 10),
        open_alert_burst_interval_seconds=_get_int("OPEN_ALERT_BURST_INTERVAL_SECONDS", 180),
        open_alert_burst_duration_seconds=_get_int("OPEN_ALERT_BURST_DURATION_SECONDS", 3600),
        open_alert_message_delay_seconds=_get_float("OPEN_ALERT_MESSAGE_DELAY_SECONDS", 1.5),
    )
