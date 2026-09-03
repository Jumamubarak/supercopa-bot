"""Supercopa de España 2027 (Istanbul) ticket-sale monitor."""

from __future__ import annotations

from datetime import datetime, timezone

from lib.monitors.base import BaseMonitor, MonitorAlert, MonitorCheckResult
from lib.scraper_core import check_target, describe_sale_status, format_price_list


class SupercopaMonitor(BaseMonitor):
    name = "Supercopa (Estambul)"

    def check(self, settings, db) -> MonitorCheckResult:
        url = settings.target_url
        previous = db.get_snapshot(url)
        result = check_target(
            url, previous, settings.keywords,
            timeout=settings.request_timeout_seconds, max_retries=settings.max_retries,
        )
        db.save_snapshot(
            url, result.parsed.content_hash, result.parsed.clean_text, result.parsed.sale_status
        )

        newly_open = result.parsed.sale_status == "OPEN" and (
            previous is None or previous.sale_status != "OPEN"
        )

        alerts: list[MonitorAlert] = []
        if not result.is_first_run and result.changed:
            alerts.append(MonitorAlert(text=self._format_change_alert(result)))

        return MonitorCheckResult(
            name=self.name,
            ok=True,
            changed=result.changed,
            is_first_run=result.is_first_run,
            status_html=self._format_status(result),
            alerts=alerts,
            raw={
                "newly_open": newly_open,
                "prices": result.parsed.prices,
                "sale_status": result.parsed.sale_status,
                "fetch_method": result.fetch_method,
            },
        )

    @staticmethod
    def _format_status(result) -> str:
        keywords = ", ".join(result.parsed.matched_keywords) or "не найдены"
        buttons = (
            "\n".join(f"• {label}" for label in result.parsed.button_states.values()) or "не обнаружены"
        )
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return (
            "🎟 <b>Supercopa (Estambul)</b>\n\n"
            f"{describe_sale_status(result.parsed.sale_status)}\n\n"
            f"<b>💶 Цены:</b> {format_price_list(result.parsed.prices)}\n\n"
            f"<b>Кнопки/статусы:</b>\n{buttons}\n\n"
            f"<b>Ключевые слова на странице:</b> {keywords}\n\n"
            f"<b>Метод получения:</b> {result.fetch_method}\n"
            f"<b>Изменилось с прошлой проверки:</b> {'Да' if result.changed and not result.is_first_run else 'Нет'}\n"
            f"<i>Проверено: {checked_at}</i>"
        )

    @staticmethod
    def _format_change_alert(result) -> str:
        header = "🚨 <b>Обновление по Суперкубку Испании 2027 (Стамбул)</b>"
        conclusion = describe_sale_status(result.parsed.sale_status)
        price_line = f"\n\n💶 <b>Цены:</b> {format_price_list(result.parsed.prices)}"
        keywords_line = (
            f"\n\n<b>Ключевые слова:</b> {', '.join(result.parsed.matched_keywords)}"
            if result.parsed.matched_keywords else ""
        )
        return f"{header}\n\n{conclusion}{price_line}\n\n{result.summary}{keywords_line}"

    @staticmethod
    def build_open_alert_message(prices: list[float], target_url: str) -> str:
        return (
            "🎟🚨 <b>ПРОДАЖА БИЛЕТОВ ОТКРЫТА!</b> 🚨🎟\n\n"
            "Финал Суперкубка Испании 2027 (Стамбул, стадион Atatürk Olympic, "
            "7 февраля) — билеты можно покупать прямо сейчас!\n\n"
            f"💶 <b>Цены:</b> {format_price_list(prices)}\n\n"
            "Не теряйте время — переходите по ссылке ниже."
        )
