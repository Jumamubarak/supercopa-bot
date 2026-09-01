"""Vercel serverless function: Telegram webhook endpoint.

Handles /start, /help, /status, /subscribe, /unsubscribe instantly on
incoming updates. Uses ``BaseHTTPRequestHandler`` directly (Vercel's
standard Python runtime entrypoint) rather than a framework, and plain
synchronous calls throughout — a serverless function is stateless and
short-lived, so there's no event loop to share.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

from lib.config import load_settings
from lib.db import Database
from lib.scraper_core import FetchError, check_target, describe_sale_status, format_price_list
from lib.telegram_api import send_message, target_keyboard

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HELP_TEXT = (
    "🇪🇸 <b>Supercopa de España 2027 — Estambul Watch Bot</b>\n\n"
    "Этот бот следит за сайтом продажи билетов RFEF "
    "(tickets.rfef.es) и присылает уведомления о любых изменениях, "
    "связанных с Суперкубком Испании 2027 в Стамбуле (стадион Atatürk Olympic).\n\n"
    "<b>Команды:</b>\n"
    "/start — приветствие и краткое описание бота\n"
    "/status — мгновенная проверка сайта прямо сейчас\n"
    "/subscribe — подписаться на уведомления в этом чате\n"
    "/unsubscribe — отписаться от уведомлений\n"
    "/help — это сообщение"
)


def handle_start(settings, db, chat_id: int, chat_type: str) -> None:
    text = (
        "👋 Привет! Я слежу за официальным сайтом продажи билетов на "
        "<b>Supercopa de España 2027</b> в Стамбуле (стадион Atatürk Olympic) "
        "и мгновенно сообщаю о любых изменениях: открытии продаж, "
        "новых карточках событий, изменении цен и статусов кнопок "
        "(«Próximamente» → «Comprar»).\n\n" + HELP_TEXT
    )
    send_message(settings.telegram_bot_token, chat_id, text, target_keyboard(settings.target_url))


def handle_help(settings, chat_id: int) -> None:
    send_message(settings.telegram_bot_token, chat_id, HELP_TEXT)


def handle_status(settings, db, chat_id: int) -> None:
    send_message(settings.telegram_bot_token, chat_id, "🔍 Проверяю сайт, подождите пару секунд...")
    try:
        previous = db.get_snapshot(settings.target_url)
        result = check_target(
            settings.target_url, previous, settings.keywords,
            timeout=settings.request_timeout_seconds, max_retries=settings.max_retries,
        )
        db.save_snapshot(
            settings.target_url, result.parsed.content_hash, result.parsed.clean_text, result.parsed.sale_status
        )

        keywords = ", ".join(result.parsed.matched_keywords) or "не найдены"
        buttons = (
            "\n".join(f"• {label}" for label in result.parsed.button_states.values()) or "не обнаружены"
        )
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        text = (
            "✅ <b>Проверка завершена</b>\n\n"
            f"{describe_sale_status(result.parsed.sale_status)}\n\n"
            f"<b>💶 Цены (финал в Стамбуле):</b> {format_price_list(result.parsed.prices)}\n\n"
            f"<b>Кнопки/статусы про финал в Стамбуле:</b>\n{buttons}\n\n"
            f"<b>Все ключевые слова на странице (не обязательно про Стамбул):</b> {keywords}\n\n"
            f"<b>Метод получения:</b> {result.fetch_method}\n"
            f"<b>Изменилось с прошлой проверки:</b> {'Да' if result.changed and not result.is_first_run else 'Нет'}\n"
            f"<i>Проверено: {checked_at}</i>"
        )
        send_message(settings.telegram_bot_token, chat_id, text, target_keyboard(settings.target_url))
    except FetchError as exc:
        send_message(settings.telegram_bot_token, chat_id, f"⚠️ Не удалось проверить сайт: {exc}\nПопробуйте ещё раз чуть позже.")
    except Exception:  # noqa: BLE001
        logger.exception("Manual /status check failed")
        send_message(settings.telegram_bot_token, chat_id, "⚠️ Не удалось проверить сайт. Попробуйте ещё раз чуть позже.")


def handle_subscribe(settings, db, chat_id: int, chat_type: str) -> None:
    added = db.add_subscriber(chat_id, chat_type)
    if added:
        send_message(
            settings.telegram_bot_token, chat_id,
            "✅ Готово! Этот чат подписан на уведомления об обновлениях по Суперкубку Испании 2027 в Стамбуле.",
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
    "/subscribe": handle_subscribe,
    "/unsubscribe": handle_unsubscribe,
}


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

    if handler in (handle_start, handle_subscribe):
        handler(settings, db, chat_id, chat_type)
    elif handler is handle_help:
        handler(settings, chat_id)
    else:
        handler(settings, db, chat_id)


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
        self.wfile.write(b"Supercopa bot webhook is alive.")
