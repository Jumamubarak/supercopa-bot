"""Yeezy (yeezy.com) new-product-drop monitor.

yeezy.com is NOT a Shopify store (no public ``/products.json``) — it's a
custom SvelteKit storefront on the Swell commerce platform
(cdn.swell.store). The full product catalog is nonetheless present in the
server-rendered HTML of the homepage, embedded inline as a JS object
literal inside SvelteKit's hydration payload (a
``data:{products:[{id:...,name:...,sku:...,price:...,...}]}`` blob). A
plain synchronous fetch of ``https://yeezy.com/`` already contains it —
no headless-browser rendering is needed. Fetch strategy (cheapest first,
shared with the Supercopa monitor): ``requests`` -> ``curl_cffi`` browser
TLS impersonation, since the site sits behind Cloudflare.

Each entry in that embedded array is one SKU (a specific color/size
variant), not one product; ``pId`` is the stable id shared by every
variant of the same product, so that's what's used to detect genuinely
new product drops.

State (which product IDs have already been seen) is the source of truth in
Supabase — the same stateless-friendly pattern as the Supercopa snapshot —
because neither Vercel functions nor GitHub Actions runners persist a local
filesystem between invocations. A local ``yeezy_products.json`` mirror is
also written on every check as a convenience for local development/
inspection; it's best-effort and never required for correctness.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from lib.monitors.base import BaseMonitor, MonitorAlert, MonitorCheckResult
from lib.scraper_core import FetchError, fetch_page

logger = logging.getLogger(__name__)

LOCAL_STATE_FILE = Path("yeezy_products.json")

# Matches the start of the embedded hydration payload; see module docstring.
_PRODUCTS_MARKER = "data:{products:["
_UNQUOTED_KEY = re.compile(r'([{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*)\s*:')


def _extract_products_array(html: str) -> list[dict]:
    start_marker = html.find(_PRODUCTS_MARKER)
    if start_marker == -1:
        raise FetchError("Yeezy product data marker not found in page HTML (site layout may have changed)")

    start = start_marker + len(_PRODUCTS_MARKER) - 1  # index of the opening '['
    depth = 0
    in_string = False
    escape = False
    end = None
    for i, ch in enumerate(html[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end is None:
        raise FetchError("Yeezy product data array was not properly closed (unbalanced brackets)")

    array_text = html[start:end]
    # The payload is a JS object literal (unquoted keys), not strict JSON.
    json_text = _UNQUOTED_KEY.sub(r'\1"\2":', array_text)
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise FetchError(f"Failed to parse Yeezy embedded product data: {exc}") from exc


def _dedupe_by_product(entries: list[dict]) -> list[dict]:
    """Collapse per-SKU/color/size entries into one row per stable product (``pId``)."""
    by_pid: dict[str, dict] = {}
    for entry in entries:
        pid = str(entry.get("pId") or entry.get("id"))
        if pid in by_pid:
            continue
        by_pid[pid] = {
            "id": pid,
            "title": entry.get("name", "Untitled"),
            "price": entry.get("price"),
            # The site is a single-page storefront (clicking a product opens an
            # in-page overlay, not a distinct URL) — no confirmed deep-link
            # scheme exists, so alerts link to the store itself.
            "url": "https://yeezy.com/",
            "image_url": entry.get("image"),
        }
    return list(by_pid.values())


def _write_local_mirror(products: list[dict]) -> None:
    try:
        LOCAL_STATE_FILE.write_text(json.dumps(products, indent=2, ensure_ascii=False))
    except OSError as exc:  # read-only/ephemeral filesystem (Vercel, some CI runners)
        logger.debug("Could not write local yeezy_products.json mirror: %s", exc)


class YeezyMonitor(BaseMonitor):
    name = "Yeezy Store"

    def check(self, settings, db) -> MonitorCheckResult:
        base_url = settings.yeezy_target_url.rstrip("/")
        fetch_result = fetch_page(
            f"{base_url}/", timeout=settings.request_timeout_seconds, max_retries=settings.max_retries
        )
        entries = _extract_products_array(fetch_result.html)
        products = _dedupe_by_product(entries)
        _write_local_mirror(products)

        known_ids = db.get_known_yeezy_product_ids()
        is_first_run = len(known_ids) == 0
        new_products = [p for p in products if str(p["id"]) not in known_ids]

        db.save_yeezy_products(products)

        alerts: list[MonitorAlert] = []
        if not is_first_run:
            for product in new_products:
                alerts.append(MonitorAlert(
                    text=self._format_drop_alert(product),
                    photo_url=product.get("image_url"),
                ))

        return MonitorCheckResult(
            name=self.name,
            ok=True,
            changed=bool(new_products) and not is_first_run,
            is_first_run=is_first_run,
            status_html=self._format_status(products, new_products, is_first_run, fetch_result.method),
            alerts=alerts,
            raw={"item_count": len(products), "new_count": len(new_products)},
        )

    @staticmethod
    def _format_status(products: list[dict], new_products: list[dict], is_first_run: bool, fetch_method: str) -> str:
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [
            "👟 <b>Yeezy Store</b>\n",
            f"<b>Товаров в каталоге:</b> {len(products)}",
        ]
        if is_first_run:
            lines.append("Первый запуск мониторинга — снимок сохранён, оповещений о «новинках» пока нет.")
        elif new_products:
            lines.append(f"🆕 <b>Новых товаров с прошлой проверки:</b> {len(new_products)}")
            for p in new_products[:10]:
                price = f" — ${p['price']}" if p.get("price") is not None else ""
                lines.append(f"• <a href=\"{p['url']}\">{p['title']}</a>{price}")
        else:
            lines.append("Новых товаров с прошлой проверки не появилось.")
        lines.append(f"\n<b>Метод получения:</b> {fetch_method}")
        lines.append(f"<i>Проверено: {checked_at}</i>")
        return "\n".join(lines)

    @staticmethod
    def _format_drop_alert(product: dict) -> str:
        price = f"\n💰 <b>Цена:</b> ${product['price']}" if product.get("price") is not None else ""
        return (
            "🆕👟 <b>Новый товар на Yeezy!</b>\n\n"
            f"<b>{product['title']}</b>{price}\n"
            f"🔗 <a href=\"{product['url']}\">Открыть yeezy.com</a>"
        )
