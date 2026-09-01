"""One-shot entrypoint run by the GitHub Actions cron workflow.

Does a single check of the target URL, updates Supabase, and broadcasts a
Telegram notification on real change. If ticket sales just transitioned to
OPEN, it stays alive in this same job run and fires the full burst-alert
sequence (N messages, pause, repeat, for up to an hour) before exiting —
that's simpler and more reliable on GitHub Actions than trying to schedule
sub-minute cron triggers.
"""

from __future__ import annotations

import logging
import sys
import time

from lib.config import load_settings
from lib.db import Database
from lib.scraper_core import FetchError, check_target, describe_sale_status, format_price_list
from lib.telegram_api import broadcast, target_keyboard

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger(__name__)


def build_notification(summary: str, matched_keywords: list[str], sale_status: str, prices: list[float]) -> str:
    header = "🚨 <b>Обновление по Суперкубку Испании 2027 (Стамбул)</b>"
    conclusion = describe_sale_status(sale_status)
    price_line = f"\n\n💶 <b>Цены:</b> {format_price_list(prices)}"
    keywords_line = f"\n\n<b>Ключевые слова:</b> {', '.join(matched_keywords)}" if matched_keywords else ""
    return f"{header}\n\n{conclusion}{price_line}\n\n{summary}{keywords_line}"


def build_open_alert_message(prices: list[float]) -> str:
    return (
        "🎟🚨 <b>ПРОДАЖА БИЛЕТОВ ОТКРЫТА!</b> 🚨🎟\n\n"
        "Финал Суперкубка Испании 2027 (Стамбул, стадион Atatürk Olympic, "
        "7 февраля) — билеты можно покупать прямо сейчас!\n\n"
        f"💶 <b>Цены:</b> {format_price_list(prices)}\n\n"
        "Не теряйте время — переходите по ссылке ниже."
    )


def send_open_alert_burst(settings, db, chat_ids: list[int], prices: list[float]) -> None:
    """10 messages back to back, 3-minute pause, repeated for a total of 1 hour."""
    message = build_open_alert_message(prices)
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


def main() -> int:
    settings = load_settings()
    db = Database(settings)
    url = settings.target_url

    try:
        previous = db.get_snapshot(url)
        result = check_target(
            url, previous, settings.keywords,
            timeout=settings.request_timeout_seconds, max_retries=settings.max_retries,
        )
        db.save_snapshot(url, result.parsed.content_hash, result.parsed.clean_text, result.parsed.sale_status)

        newly_open = result.parsed.sale_status == "OPEN" and (
            previous is None or previous.sale_status != "OPEN"
        )

        if newly_open:
            logger.warning("Tickets just went OPEN on %s (via %s)", url, result.fetch_method)
            chat_ids = db.get_all_subscribers()
            if chat_ids:
                send_open_alert_burst(settings, db, chat_ids, result.parsed.prices)
            else:
                logger.info("No subscribers to notify.")
        elif result.changed and not result.is_first_run:
            logger.info("Change detected on %s (via %s)", url, result.fetch_method)
            chat_ids = db.get_all_subscribers()
            if chat_ids:
                message = build_notification(
                    result.summary, result.parsed.matched_keywords, result.parsed.sale_status, result.parsed.prices
                )
                broadcast(settings.telegram_bot_token, chat_ids, message, target_keyboard(url), db)
            else:
                logger.info("No subscribers to notify.")
        elif result.is_first_run:
            logger.info("Initial snapshot stored for %s (via %s)", url, result.fetch_method)
        else:
            logger.info("No changes detected on %s", url)

        return 0

    except FetchError as exc:
        logger.error("Scrape failed after all fallback strategies: %s", exc)
        return 1
    except Exception:  # noqa: BLE001 - never crash the workflow with a stack trace only
        logger.exception("Unexpected error in scraper job")
        return 1


if __name__ == "__main__":
    sys.exit(main())
