"""Network client, HTML parsing, and change-detection for tickets.rfef.es.

Synchronous version (ported from the original asyncio implementation) so it
runs unmodified inside both a Vercel serverless function (for the on-demand
``/status`` command) and a one-shot GitHub Actions script (for the periodic
scrape). Fetch strategy (cheapest first):

1. ``requests`` with rotated browser-like headers.
2. ``curl_cffi`` impersonating a real browser TLS fingerprint, used when the
   plain client looks blocked (403/503, Cloudflare interstitial markers).

The Playwright fallback from the original design was dropped: it needs a
headless-Chromium binary that's awkward and slow to install on both
platforms, and the two tiers above already succeed against the real site.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

from lib.db import Snapshot

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

CLOUDFLARE_MARKERS = (
    "cf-browser-verification",
    "cf_chl_",
    "Just a moment...",
    "Attention Required! | Cloudflare",
    "challenge-platform",
)

BUY_STATE_PHRASES = ("Comprar", "Entradas", "Próximamente", "Proximamente", "Agotado", "Sold out")


def _random_headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": random.choice(["es-ES,es;q=0.9,en;q=0.8", "en-US,en;q=0.9,es;q=0.7"]),
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }


def _looks_blocked(status_code: int, text: str) -> bool:
    if status_code in (403, 503, 429):
        return True
    lowered = text[:5000]
    return any(marker.lower() in lowered.lower() for marker in CLOUDFLARE_MARKERS)


@dataclass
class FetchResult:
    html: str
    status_code: int
    method: str  # "requests" | "curl_cffi"


class FetchError(RuntimeError):
    """Raised when every fetch strategy fails or is blocked."""


def _fetch_requests(url: str, timeout: int, max_retries: int) -> FetchResult:
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=_random_headers(), timeout=timeout, allow_redirects=True)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} server error")
            return FetchResult(html=resp.text, status_code=resp.status_code, method="requests")
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            last_exc = exc
            time.sleep(min(10, 2 ** attempt))
    raise last_exc or FetchError("requests fetch failed")


def _fetch_curl_cffi(url: str, timeout: int) -> FetchResult:
    from curl_cffi import requests as curl_requests

    response = curl_requests.get(
        url,
        impersonate="chrome124",
        timeout=timeout,
        headers={"Accept-Language": "es-ES,es;q=0.9,en;q=0.8"},
    )
    return FetchResult(html=response.text, status_code=response.status_code, method="curl_cffi")


def fetch_page(url: str, timeout: int = 20, max_retries: int = 3) -> FetchResult:
    """Fetch ``url`` trying the cheap method first, escalating only if blocked."""
    try:
        result = _fetch_requests(url, timeout, max_retries)
        if not _looks_blocked(result.status_code, result.html):
            return result
        logger.warning("requests fetch looked blocked (status=%s), escalating to curl_cffi", result.status_code)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, we escalate on any failure
        logger.warning("requests fetch failed (%s), escalating to curl_cffi", exc)

    try:
        result = _fetch_curl_cffi(url, timeout)
        if not _looks_blocked(result.status_code, result.html):
            return result
        logger.warning("curl_cffi fetch looked blocked (status=%s)", result.status_code)
        raise FetchError(f"curl_cffi fetch looked blocked for {url}")
    except Exception as exc:  # noqa: BLE001
        logger.error("curl_cffi fetch failed (%s)", exc)
        raise FetchError(f"All fetch strategies failed for {url}") from exc


# Matches "25,00 €", "150€", "€ 45.00", "Desde 45 EUR", etc.
_PRICE_PATTERN = re.compile(
    r"(?:€\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?))"
    r"|(?:(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?)\s*(?:€|EUR\b))",
    re.IGNORECASE,
)


def _parse_price_number(raw: str) -> float | None:
    """Convert a matched price string (European or plain format) to a float."""
    cleaned = raw.strip()
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_prices(text: str) -> list[float]:
    """Extract unique ticket prices (in EUR) mentioned in ``text``, ascending."""
    found: set[float] = set()
    for match in _PRICE_PATTERN.finditer(text):
        raw = match.group(1) or match.group(2)
        if not raw:
            continue
        value = _parse_price_number(raw)
        if value is not None and 0 < value < 100_000:
            found.add(round(value, 2))
    return sorted(found)


def format_price_list(prices: list[float]) -> str:
    if not prices:
        return "Цены пока не опубликованы на странице."
    formatted = " | ".join(f"{p:g} €" for p in prices)
    if len(prices) == 1:
        return f"Цена: {formatted}"
    return f"От {prices[0]:g} € до {prices[-1]:g} €\nВсе цены: {formatted}"


SOLD_OUT_PHRASES = ("agotado", "sold out")
OPEN_PHRASES = ("comprar",)
NOT_STARTED_PHRASES = ("próximamente", "proximamente", "coming soon")

SALE_STATUS_DESCRIPTIONS = {
    "OPEN": "🟢 <b>Продажа билетов открыта</b> — кнопка «Comprar» активна.",
    "SOLD_OUT": "🔴 <b>Билеты распроданы</b> — сайт показывает «Agotado» / «Sold out».",
    "NOT_STARTED": "🟡 <b>Продажа ещё не началась</b> — сайт показывает «Próximamente».",
    "UNKNOWN": "⚪️ <b>Статус продаж не определён</b> — явных признаков «Comprar»/«Próximamente»/«Agotado» на странице не найдено.",
    "NOT_FOUND": (
        "⚪️ <b>Информации о финале Суперкубка Испании в Стамбуле (7 февраля) "
        "пока нет на сайте</b> — упоминаний Estambul/Atatürk не найдено. Другие "
        "упоминания «Supercopa» на странице относятся к другим турнирам "
        "(например, футзальному Кубку)."
    ),
}

EXCLUDE_CONTEXT_PHRASES = ("futbol sala", "fútbol sala", "palau blaugrana")
ISTANBUL_SIGNAL_PHRASES = ("estambul", "atatürk", "ataturk", "istanbul", "7 de febrero", "7 febrero")
_BLOCK_TAGS = ("article", "section", "li", "div")


def find_relevant_blocks(soup: BeautifulSoup) -> list:
    """Find the smallest enclosing block around each Istanbul-signal mention.

    Walking up from the exact text node (rather than scanning a fixed
    character window in the flattened page text) keeps unrelated sibling
    cards — e.g. the futsal Supercopa's own "Comprar" button — from ever
    being pulled into the Istanbul match's context.
    """
    blocks = []
    seen_ids: set[int] = set()

    for node in soup.find_all(string=True):
        node_lower = node.lower()
        if not any(sig in node_lower for sig in ISTANBUL_SIGNAL_PHRASES):
            continue

        block = node.parent
        while block is not None and getattr(block, "name", None) not in _BLOCK_TAGS:
            block = block.parent
        if block is None or id(block) in seen_ids:
            continue

        block_text = block.get_text(" ", strip=True)
        if any(ex in block_text.lower() for ex in EXCLUDE_CONTEXT_PHRASES):
            continue

        seen_ids.add(id(block))
        blocks.append(block)

    return blocks


def determine_sale_status(button_states: dict[str, str], clean_text: str) -> str:
    haystacks = list(button_states.values()) or [clean_text]
    lowered = " ".join(haystacks).lower()

    if any(phrase in lowered for phrase in SOLD_OUT_PHRASES):
        return "SOLD_OUT"
    if any(phrase in lowered for phrase in OPEN_PHRASES):
        return "OPEN"
    if any(phrase in lowered for phrase in NOT_STARTED_PHRASES):
        return "NOT_STARTED"
    return "UNKNOWN"


def describe_sale_status(status: str) -> str:
    return SALE_STATUS_DESCRIPTIONS.get(status, SALE_STATUS_DESCRIPTIONS["UNKNOWN"])


@dataclass
class ParsedPage:
    clean_text: str
    content_hash: str
    matched_keywords: list[str]
    button_states: dict[str, str] = field(default_factory=dict)
    sale_status: str = "UNKNOWN"
    prices: list[float] = field(default_factory=list)
    target_found: bool = False


def parse_page(html: str, keywords: tuple[str, ...]) -> ParsedPage:
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    text = soup.get_text(separator=" | ", strip=True)
    normalized = " ".join(text.split())

    matched = [kw for kw in keywords if kw.lower() in normalized.lower()]

    content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def extract_buttons(scope) -> dict[str, str]:
        found: dict[str, str] = {}
        for element in scope.find_all(["button", "a"]):
            label = element.get_text(strip=True)
            if not label:
                continue
            if any(phrase.lower() in label.lower() for phrase in BUY_STATE_PHRASES):
                key = element.get("id") or element.get("class") and " ".join(element.get("class")) or label
                found[str(key)] = label
        return found

    relevant_blocks = find_relevant_blocks(soup)
    target_found = bool(relevant_blocks)

    if target_found:
        relevant_text = " … ".join(
            " ".join(block.get_text(" ", strip=True).split()) for block in relevant_blocks
        )
        button_states: dict[str, str] = {}
        for block in relevant_blocks:
            button_states.update(extract_buttons(block))
        sale_status = determine_sale_status(button_states, relevant_text)
        prices = extract_prices(relevant_text)
    else:
        button_states = {}
        sale_status = "NOT_FOUND"
        prices = []

    return ParsedPage(
        clean_text=normalized,
        content_hash=content_hash,
        matched_keywords=matched,
        button_states=button_states,
        sale_status=sale_status,
        prices=prices,
        target_found=target_found,
    )


@dataclass
class ScrapeResult:
    changed: bool
    is_first_run: bool
    parsed: ParsedPage
    summary: str
    fetch_method: str


def diff_snapshots(previous: Snapshot | None, parsed: ParsedPage) -> str:
    if previous is None:
        return "Первый запуск мониторинга — сохранён начальный снимок страницы."

    lines: list[str] = []

    if previous.sale_status != parsed.sale_status:
        lines.append(f"⚡️ Статус продаж изменился: {previous.sale_status} → {parsed.sale_status}")

    if parsed.matched_keywords:
        lines.append("Найденные ключевые слова: " + ", ".join(parsed.matched_keywords))

    old_len = len(previous.raw_text)
    new_len = len(parsed.clean_text)
    if abs(new_len - old_len) > 40:
        direction = "увеличился" if new_len > old_len else "уменьшился"
        lines.append(f"Объём текста на странице {direction} ({old_len} → {new_len} символов).")

    if not lines:
        lines.append("Обнаружено изменение содержимого страницы.")

    return "\n".join(lines)


def check_target(
    url: str, previous: Snapshot | None, keywords: tuple[str, ...], timeout: int = 20, max_retries: int = 3
) -> ScrapeResult:
    fetch_result = fetch_page(url, timeout=timeout, max_retries=max_retries)
    parsed = parse_page(fetch_result.html, keywords)

    is_first_run = previous is None
    changed = is_first_run or previous.content_hash != parsed.content_hash

    summary = diff_snapshots(previous, parsed) if changed else "Изменений не обнаружено."

    return ScrapeResult(
        changed=changed,
        is_first_run=is_first_run,
        parsed=parsed,
        summary=summary,
        fetch_method=fetch_result.method,
    )
