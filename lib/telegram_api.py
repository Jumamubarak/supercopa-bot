"""Minimal synchronous Telegram Bot API client (plain HTTP via ``requests``).

Deliberately avoids python-telegram-bot: that library expects a long-lived
event loop / polling process, which doesn't fit stateless Vercel functions
or a one-shot GitHub Actions script.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"


def _call(token: str, method: str, payload: dict) -> dict:
    url = API_BASE.format(token=token, method=method)
    resp = requests.post(url, json=payload, timeout=15)
    data = resp.json()
    if not data.get("ok"):
        raise TelegramApiError(method, data)
    return data.get("result", {})


class TelegramApiError(RuntimeError):
    def __init__(self, method: str, data: dict) -> None:
        self.method = method
        self.data = data
        super().__init__(f"Telegram API call {method} failed: {data}")


def link_keyboard(label: str, url: str) -> dict:
    return {"inline_keyboard": [[{"text": label, "url": url}]]}


def target_keyboard(url: str) -> dict:
    return link_keyboard("🎟 Открыть tickets.rfef.es", url)


def send_message(token: str, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _call(token, "sendMessage", payload)


def send_photo(
    token: str, chat_id: int, photo_url: str, caption: str, reply_markup: dict | None = None
) -> dict:
    payload = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _call(token, "sendPhoto", payload)


def _prune_if_blocked(db, chat_id: int, exc: "TelegramApiError") -> bool:
    error_code = exc.data.get("error_code")
    description = str(exc.data.get("description", ""))
    if error_code == 403 or "bot was blocked" in description.lower():
        logger.warning("Chat %s blocked the bot; removing subscriber.", chat_id)
        db.remove_subscriber(chat_id)
        return True
    return False


def broadcast(token: str, chat_ids: list[int], text: str, reply_markup: dict | None, db) -> None:
    """Send ``text`` to every chat in ``chat_ids``, pruning blocked chats from ``db``."""
    for chat_id in chat_ids:
        try:
            send_message(token, chat_id, text, reply_markup)
        except TelegramApiError as exc:
            if not _prune_if_blocked(db, chat_id, exc):
                logger.warning("Failed to notify chat %s: %s", chat_id, exc)


def broadcast_alert(
    token: str,
    chat_ids: list[int],
    text: str,
    photo_url: str | None,
    reply_markup: dict | None,
    db,
) -> None:
    """Broadcast one alert, sending it as a photo+caption when a photo URL is given.

    Falls back to a plain text message if the photo itself fails to send
    (e.g. an invalid/expired image URL) so the alert is never silently lost.
    """
    for chat_id in chat_ids:
        try:
            if photo_url:
                try:
                    send_photo(token, chat_id, photo_url, text, reply_markup)
                except TelegramApiError as photo_exc:
                    if _prune_if_blocked(db, chat_id, photo_exc):
                        continue
                    logger.warning("sendPhoto failed for chat %s (%s), falling back to text", chat_id, photo_exc)
                    send_message(token, chat_id, text, reply_markup)
            else:
                send_message(token, chat_id, text, reply_markup)
        except TelegramApiError as exc:
            if not _prune_if_blocked(db, chat_id, exc):
                logger.warning("Failed to notify chat %s: %s", chat_id, exc)


def set_webhook(token: str, url: str, secret_token: str) -> dict:
    return _call(token, "setWebhook", {"url": url, "secret_token": secret_token})


def get_webhook_info(token: str) -> dict:
    return _call(token, "getWebhookInfo", {})
