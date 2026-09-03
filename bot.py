"""Standalone always-on bot for LOCAL DEVELOPMENT / TESTING ONLY.

Production runs on Vercel (api/webhook.py) + GitHub Actions cron
(scraper_job.py + .github/workflows/scrape_*.yml) so nothing needs to stay
running on your machine — see README.md. This script exists because you
explicitly asked for an aiogram-style bot with asyncio background loops;
run it locally with ``python bot.py`` if you want live long-polling plus
in-process periodic checks instead of the serverless split.

Requires ``pip install -r requirements-bot.txt`` (aiogram is NOT part of
the Vercel deployment's dependencies, to keep that function lean).

Background loops run each monitor's blocking ``check()`` via
``asyncio.to_thread`` so one slow/blocked site can never stall the other
loop or the command handlers.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from lib.config import load_settings
from lib.db import Database
from lib.monitors.base import MonitorAlert, safe_check
from lib.monitors.supercopa import SupercopaMonitor
from lib.monitors.yeezy import YeezyMonitor
from scraper_job import send_open_alert_burst

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger(__name__)

settings = load_settings()
db = Database(settings)
bot = Bot(token=settings.telegram_bot_token, default_parse_mode=ParseMode.HTML)
dp = Dispatcher()

supercopa_monitor = SupercopaMonitor()
yeezy_monitor = YeezyMonitor()


def _link_keyboard(label: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=url)]])


HELP_TEXT = (
    "🇪🇸👟 <b>Supercopa &amp; Yeezy Watch Bot</b> (local dev mode)\n\n"
    "<b>Команды:</b>\n"
    "/start — приветствие\n"
    "/status — проверить оба сайта сразу\n"
    "/status_supercopa — проверить только билеты на Supercopa\n"
    "/status_yeezy — проверить только магазин Yeezy\n"
    "/subscribe — подписаться на уведомления в этом чате\n"
    "/unsubscribe — отписаться от уведомлений\n"
    "/help — это сообщение"
)


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer("👋 Привет!\n\n" + HELP_TEXT)


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@dp.message(Command("status_supercopa"))
async def cmd_status_supercopa(message: Message) -> None:
    await message.answer(f"🔍 Проверяю «{supercopa_monitor.name}»...")
    result = await asyncio.to_thread(safe_check, supercopa_monitor, settings, db)
    await message.answer(result.status_html, reply_markup=_link_keyboard("🎟 Открыть tickets.rfef.es", settings.target_url))


@dp.message(Command("status_yeezy"))
async def cmd_status_yeezy(message: Message) -> None:
    await message.answer(f"🔍 Проверяю «{yeezy_monitor.name}»...")
    result = await asyncio.to_thread(safe_check, yeezy_monitor, settings, db)
    await message.answer(result.status_html, reply_markup=_link_keyboard("👟 Открыть yeezy.com", settings.yeezy_target_url))


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    await message.answer("🔍 Проверяю оба сайта...")
    supercopa_result, yeezy_result = await asyncio.gather(
        asyncio.to_thread(safe_check, supercopa_monitor, settings, db),
        asyncio.to_thread(safe_check, yeezy_monitor, settings, db),
    )
    combined = (
        "✅ <b>Сводный отчёт</b>\n\n"
        f"{supercopa_result.status_html}\n\n"
        "————————————————\n\n"
        f"{yeezy_result.status_html}"
    )
    await message.answer(combined)


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message) -> None:
    added = await asyncio.to_thread(db.add_subscriber, message.chat.id, message.chat.type)
    text = "✅ Готово! Этот чат подписан на уведомления." if added else "ℹ️ Этот чат уже подписан."
    await message.answer(text)


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message) -> None:
    removed = await asyncio.to_thread(db.remove_subscriber, message.chat.id)
    text = "🔕 Вы отписаны от уведомлений." if removed else "ℹ️ Этот чат не был подписан."
    await message.answer(text)


async def _broadcast_alerts(alerts: list[MonitorAlert], keyboard: InlineKeyboardMarkup | None) -> None:
    chat_ids = await asyncio.to_thread(db.get_all_subscribers)
    for alert in alerts:
        for chat_id in chat_ids:
            try:
                if alert.photo_url:
                    await bot.send_photo(chat_id, alert.photo_url, caption=alert.text, reply_markup=keyboard)
                else:
                    await bot.send_message(chat_id, alert.text, reply_markup=keyboard)
            except Exception:  # noqa: BLE001 - one blocked/broken chat must not stop the rest
                logger.exception("Failed to notify chat %s", chat_id)


async def supercopa_loop() -> None:
    keyboard = _link_keyboard("🎟 Открыть tickets.rfef.es", settings.target_url)
    while True:
        try:
            result = await asyncio.to_thread(safe_check, supercopa_monitor, settings, db)
            if result.ok:
                newly_open = bool(result.raw and result.raw.get("newly_open"))
                if newly_open:
                    chat_ids = await asyncio.to_thread(db.get_all_subscribers)
                    if chat_ids:
                        await asyncio.to_thread(
                            send_open_alert_burst, settings, db, chat_ids, result.raw.get("prices", [])
                        )
                elif result.alerts:
                    await _broadcast_alerts(result.alerts, keyboard)
        except Exception:  # noqa: BLE001 - keep the loop alive across failures
            logger.exception("Supercopa background loop iteration failed")
        await asyncio.sleep(settings.supercopa_check_interval_seconds)


async def yeezy_loop() -> None:
    while True:
        try:
            result = await asyncio.to_thread(safe_check, yeezy_monitor, settings, db)
            if result.ok and result.alerts:
                await _broadcast_alerts(result.alerts, None)
        except Exception:  # noqa: BLE001 - keep the loop alive across failures
            logger.exception("Yeezy background loop iteration failed")
        await asyncio.sleep(settings.yeezy_check_interval_seconds)


async def main() -> None:
    logger.info(
        "Starting bot.py (local dev): Supercopa every %ss, Yeezy every %ss",
        settings.supercopa_check_interval_seconds, settings.yeezy_check_interval_seconds,
    )
    await asyncio.gather(
        dp.start_polling(bot),
        supercopa_loop(),
        yeezy_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
