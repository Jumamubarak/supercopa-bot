"""One-off local script: point your Telegram bot at the deployed Vercel
webhook. Run this once after each Vercel deploy (or whenever the URL
changes) — not part of the running system.

Usage:
    python set_webhook.py https://your-project.vercel.app
"""

from __future__ import annotations

import sys

from lib.config import load_settings
from lib.telegram_api import get_webhook_info, set_webhook


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python set_webhook.py https://your-project.vercel.app")
        return 1

    base_url = sys.argv[1].rstrip("/")
    webhook_url = f"{base_url}/api/webhook"

    settings = load_settings()
    result = set_webhook(settings.telegram_bot_token, webhook_url, settings.telegram_webhook_secret)
    print("setWebhook result:", result)

    info = get_webhook_info(settings.telegram_bot_token)
    print("getWebhookInfo:", info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
