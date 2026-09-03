"""Shared interface for site monitors.

All monitors are synchronous (blocking HTTP calls) so the same code runs
unmodified inside a Vercel serverless function, a one-shot GitHub Actions
script, and (via ``asyncio.to_thread``) the optional standalone ``bot.py``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MonitorAlert:
    """One notification to broadcast to subscribers."""

    text: str
    photo_url: str | None = None


@dataclass
class MonitorCheckResult:
    name: str
    ok: bool
    changed: bool
    is_first_run: bool
    status_html: str
    alerts: list[MonitorAlert] = field(default_factory=list)
    error: str | None = None
    raw: dict | None = None  # monitor-specific extra data for job-level logic


class BaseMonitor(ABC):
    name: str

    @abstractmethod
    def check(self, settings, db) -> MonitorCheckResult:
        """Run one check cycle, persist any new state, and return the result."""
        raise NotImplementedError


def safe_check(monitor: BaseMonitor, settings, db) -> MonitorCheckResult:
    """Run ``monitor.check`` and never let it raise.

    One monitor's crash (network error, site markup change, blocked
    request) must never stop another monitor's check or take down the bot.
    """
    try:
        return monitor.check(settings, db)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, isolation boundary
        logger.exception("%s monitor check failed", monitor.name)
        return MonitorCheckResult(
            name=monitor.name,
            ok=False,
            changed=False,
            is_first_run=False,
            status_html=f"⚠️ Ошибка проверки «{monitor.name}»: {exc}",
            error=str(exc),
        )
