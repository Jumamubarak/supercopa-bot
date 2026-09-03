"""Vercel serverless function: Telegram webhook endpoint.

Handles /start, /help, /status, /status_supercopa, /status_yeezy,
/subscribe, /unsubscribe instantly on incoming updates. Uses
``BaseHTTPRequestHandler`` directly (Vercel's standard Python runtime
entrypoint) rather than a framework, and plain synchronous calls throughout
— a serverless function is stateless and short-lived, so there's no event
loop to share.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler

from lib.config import load_settings
from lib.db import Database
from lib.monitors.base import safe_check
from lib.monitors.supercopa import SupercopaMonitor
from lib.monitors.yeezy import YeezyMonitor
from lib.telegram_api import link_keyboard, send_message, target_keyboard

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SUPERCOPA_MONITOR = SupercopaMonitor()
YEEZY_MONITOR = YeezyMonitor()

HELP_TEXT = (
    "🇪🇸👟 <b>Supercopa &amp; Yeezy Watch Bot</b>\n\n"
    "Слежу за двумя сайтами и мгновенно сообщаю об изменениях:\n"
    "• tickets.rfef.es — продажа билетов на финал Supercopa de España 2027 в Стамбуле\n"
    "• yeezy.com — новые товары в магазине\n\n"
    "<b>Команды:</b>\n"
    "/start — приветствие и краткое описание бота\n"
    "/status — проверить оба сайта сразу\n"
    "/status_supercopa — проверить только билеты на Supercopa\n"
    "/status_yeezy — проверить только магазин Yeezy\n"
    "/subscribe — подписаться на уведомления в этом чате\n"
    "/unsubscribe — отписаться от уведомлений\n"
    "/help — это сообщение"
)


def handle_start(settings, db, chat_id: int, chat_type: str) -> None:
    send_message(settings.telegram_bot_token, chat_id, "👋 Привет!\n\n" + HELP_TEXT)


def handle_help(settings, chat_id: int) -> None:
    send_message(settings.telegram_bot_token, chat_id, HELP_TEXT)


def _reply_with_check(settings, db, chat_id: int, monitor, keyboard: dict | None) -> None:
    send_message(settings.telegram_bot_token, chat_id, f"🔍 Проверяю «{monitor.name}», подождите пару секунд...")
    result = safe_check(monitor, settings, db)
    send_message(settings.telegram_bot_token, chat_id, result.status_html, keyboard)


def handle_status_supercopa(settings, db, chat_id: int) -> None:
    _reply_with_check(settings, db, chat_id, SUPERCOPA_MONITOR, target_keyboard(settings.target_url))


def handle_status_yeezy(settings, db, chat_id: int) -> None:
    _reply_with_check(
        settings, db, chat_id, YEEZY_MONITOR, link_keyboard("👟 Открыть yeezy.com", settings.yeezy_target_url)
    )


def handle_status(settings, db, chat_id: int) -> None:
    send_message(settings.telegram_bot_token, chat_id, "🔍 Проверяю оба сайта, подождите пару секунд...")
    supercopa_result = safe_check(SUPERCOPA_MONITOR, settings, db)
    yeezy_result = safe_check(YEEZY_MONITOR, settings, db)
    combined = (
        "✅ <b>Сводный отчёт</b>\n\n"
        f"{supercopa_result.status_html}\n\n"
        "————————————————\n\n"
        f"{yeezy_result.status_html}"
    )
    send_message(settings.telegram_bot_token, chat_id, combined)


def handle_subscribe(settings, db, chat_id: int, chat_type: str) -> None:
    added = db.add_subscriber(chat_id, chat_type)
    if added:
        send_message(
            settings.telegram_bot_token, chat_id,
            "✅ Готово! Этот чат подписан на уведомления по Supercopa и Yeezy.",
        )
    else:
        send_message(settings.telegram_bot_token, chat_id, "ℹ️ Этот чат уже подписан на уведомления.")


def handle_unsubscribe(settings, db, chat_id: int) -> None:
    removed = db.remove_subscriber(chat_id)
    if removed:
        send_message(settings.telegram_bot_token, chat_id, "🔕 Вы отписаны от уведомлений.")
    else:
        send_message(settings.telegram_bot_token, chat_id, "ℹ️ Этот чат не был подписан.")


COMMANDS = {
    "/start": handle_start,
    "/help": handle_help,
    "/status": handle_status,
    "/status_supercopa": handle_status_supercopa,
    "/status_yeezy": handle_status_yeezy,
    "/subscribe": handle_subscribe,
    "/unsubscribe": handle_unsubscribe,
}

_NEEDS_CHAT_TYPE = (handle_start, handle_subscribe)
_CHAT_ID_ONLY = (handle_status, handle_status_supercopa, handle_status_yeezy, handle_unsubscribe)


def process_update(settings, db, update: dict) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type", "unknown")
    text = (message.get("text") or "").strip()
    if not text.startswith("/") or chat_id is None:
        return

    command = text.split()[0].split("@")[0]  # strip bot mention e.g. /start@mybot
    handler = COMMANDS.get(command)
    if handler is None:
        return

    try:
        if handler in _NEEDS_CHAT_TYPE:
            handler(settings, db, chat_id, chat_type)
        elif handler is handle_help:
            handler(settings, chat_id)
        else:
            handler(settings, db, chat_id)
    except Exception:  # noqa: BLE001 - one failing command must not break the webhook response
        logger.exception("Command %s failed", command)
        send_message(settings.telegram_bot_token, chat_id, "⚠️ Произошла ошибка при обработке команды. Попробуйте ещё раз чуть позже.")


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - required name by Vercel/BaseHTTPRequestHandler
        try:
            settings = load_settings()

            secret_header = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if secret_header != settings.telegram_webhook_secret:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"unauthorized")
                return

            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b"{}"
            update = json.loads(body or b"{}")

            db = Database(settings)
            process_update(settings, db, update)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
        except Exception:  # noqa: BLE001 - never let Telegram see a 500 loop
            logger.exception("Webhook processing failed")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Supercopa/Yeezy bot webhook is alive.")
