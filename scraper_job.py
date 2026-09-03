"""One-shot entrypoint run by the GitHub Actions cron workflows.

Usage: ``python scraper_job.py [supercopa|yeezy|all]`` (default: ``all``).
Each monitor is wrapped so its failure can never take down the other — see
``lib.monitors.base.safe_check``. Supercopa and Yeezy run on separate
GitHub Actions workflows/schedules (``SUPERCOPA_CHECK_INTERVAL`` /
``YEEZY_CHECK_INTERVAL`` describe the intended cadence; GitHub Actions cron
syntax is static, so the actual schedule lives in the workflow YAML and
should be kept in sync with those env vars by hand).

If Supercopa ticket sales just transitioned to OPEN, this stays alive in
the same job run and fires the full burst-alert sequence (N messages,
pause, repeat, for up to an hour) before exiting — simpler and more
reliable on GitHub Actions than trying to schedule sub-minute cron
triggers.
"""

from __future__ import annotations

import logging
import sys
import time

from lib.config import load_settings
from lib.db import Database
from lib.monitors.base import safe_check
from lib.monitors.supercopa import SupercopaMonitor
from lib.monitors.yeezy import YeezyMonitor
from lib.telegram_api import broadcast, broadcast_alert, target_keyboard

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger(__name__)


def send_open_alert_burst(settings, db, chat_ids: list[int], prices: list[float]) -> None:
    """10 messages back to back, 3-minute pause, repeated for a total of 1 hour."""
    message = SupercopaMonitor.build_open_alert_message(prices, settings.target_url)
    keyboard = target_keyboard(settings.target_url)
    count = settings.open_alert_burst_count
    interval = settings.open_alert_burst_interval_seconds
    per_message_delay = settings.open_alert_message_delay_seconds
    deadline = time.monotonic() + settings.open_alert_burst_duration_seconds

    logger.warning(
        "Tickets are OPEN — starting alert burst: %d msgs every %ds for %ds total",
        count, interval, settings.open_alert_burst_duration_seconds,
    )

    batch = 0
    while time.monotonic() < deadline:
        batch += 1
        logger.info("Open-alert burst batch #%d: sending %d messages", batch, count)
        for i in range(count):
            broadcast(settings.telegram_bot_token, chat_ids, message, keyboard, db)
            if i < count - 1:
                time.sleep(per_message_delay)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    logger.info("Open-alert burst finished after %d batches", batch)


def run_supercopa(settings, db) -> bool:
    """Returns True on success (ok), False on failure."""
    result = safe_check(SupercopaMonitor(), settings, db)
    if not result.ok:
        logger.error("Supercopa check failed: %s", result.error)
        return False

    chat_ids = db.get_all_subscribers()
    newly_open = bool(result.raw and result.raw.get("newly_open"))

    if newly_open:
        logger.warning("Tickets just went OPEN on %s", settings.target_url)
        if chat_ids:
            send_open_alert_burst(settings, db, chat_ids, result.raw.get("prices", []))
        else:
            logger.info("No subscribers to notify.")
    elif result.alerts and chat_ids:
        for alert in result.alerts:
            broadcast_alert(
                settings.telegram_bot_token, chat_ids, alert.text, alert.photo_url,
                target_keyboard(settings.target_url), db,
            )
    elif result.is_first_run:
        logger.info("Initial Supercopa snapshot stored.")
    else:
        logger.info("No Supercopa changes detected.")

    return True


def run_yeezy(settings, db) -> bool:
    result = safe_check(YeezyMonitor(), settings, db)
    if not result.ok:
        logger.error("Yeezy check failed: %s", result.error)
        return False

    if result.alerts:
        chat_ids = db.get_all_subscribers()
        if chat_ids:
            logger.info("Broadcasting %d new Yeezy product alert(s)", len(result.alerts))
            for alert in result.alerts:
                broadcast_alert(settings.telegram_bot_token, chat_ids, alert.text, alert.photo_url, None, db)
        else:
            logger.info("No subscribers to notify.")
    elif result.is_first_run:
        logger.info("Initial Yeezy product state stored (%s items).", (result.raw or {}).get("item_count"))
    else:
        logger.info("No new Yeezy products detected.")

    return True


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    if target not in ("supercopa", "yeezy", "all"):
        logger.error("Unknown target %r (expected supercopa|yeezy|all)", target)
        return 2

    settings = load_settings()
    db = Database(settings)

    ok = True
    if target in ("supercopa", "all"):
        ok = run_supercopa(settings, db) and ok
    if target in ("yeezy", "all"):
        ok = run_yeezy(settings, db) and ok

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
