"""Supabase (Postgres via PostgREST) persistence — replaces the old SQLite
layer so state survives across stateless Vercel function invocations and
one-shot GitHub Actions runs.

Uses plain HTTP calls to Supabase's auto-generated REST API with the
service-role key, so no extra SDK dependency is needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

from lib.config import Settings


@dataclass(frozen=True)
class Snapshot:
    target_url: str
    content_hash: str
    raw_text: str
    sale_status: str
    updated_at: str


class Database:
    def __init__(self, settings: Settings) -> None:
        self._base = f"{settings.supabase_url}/rest/v1"
        self._headers = {
            "apikey": settings.supabase_service_key,
            "Authorization": f"Bearer {settings.supabase_service_key}",
            "Content-Type": "application/json",
        }

    # --- Subscribers -----------------------------------------------------

    def add_subscriber(self, chat_id: int, chat_type: str) -> bool:
        """Add a chat to the broadcast list. Returns True if newly added."""
        resp = requests.post(
            f"{self._base}/subscribers",
            headers={**self._headers, "Prefer": "resolution=ignore-duplicates,return=representation"},
            json={"chat_id": chat_id, "chat_type": chat_type},
            timeout=10,
        )
        resp.raise_for_status()
        return len(resp.json()) > 0

    def remove_subscriber(self, chat_id: int) -> bool:
        resp = requests.delete(
            f"{self._base}/subscribers",
            headers={**self._headers, "Prefer": "return=representation"},
            params={"chat_id": f"eq.{chat_id}"},
            timeout=10,
        )
        resp.raise_for_status()
        return len(resp.json()) > 0

    def get_all_subscribers(self) -> list[int]:
        resp = requests.get(
            f"{self._base}/subscribers",
            headers=self._headers,
            params={"select": "chat_id"},
            timeout=10,
        )
        resp.raise_for_status()
        return [row["chat_id"] for row in resp.json()]

    # --- Snapshots ---------------------------------------------------------

    def get_snapshot(self, target_url: str) -> Optional[Snapshot]:
        resp = requests.get(
            f"{self._base}/snapshots",
            headers=self._headers,
            params={"target_url": f"eq.{target_url}", "select": "*"},
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        row = rows[0]
        return Snapshot(
            target_url=row["target_url"],
            content_hash=row["content_hash"],
            raw_text=row["raw_text"],
            sale_status=row["sale_status"],
            updated_at=row["updated_at"],
        )

    def save_snapshot(
        self, target_url: str, content_hash: str, raw_text: str, sale_status: str
    ) -> None:
        resp = requests.post(
            f"{self._base}/snapshots",
            headers={
                **self._headers,
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            params={"on_conflict": "target_url"},
            json={
                "target_url": target_url,
                "content_hash": content_hash,
                "raw_text": raw_text,
                "sale_status": sale_status,
            },
            timeout=10,
        )
        resp.raise_for_status()

    # --- Yeezy product state ----------------------------------------------

    def get_known_yeezy_product_ids(self) -> set[str]:
        resp = requests.get(
            f"{self._base}/yeezy_products",
            headers=self._headers,
            params={"select": "product_id"},
            timeout=10,
        )
        resp.raise_for_status()
        return {str(row["product_id"]) for row in resp.json()}

    def save_yeezy_products(self, products: list[dict]) -> None:
        """Upsert products (id, title, price, url, image_url) into state."""
        if not products:
            return
        resp = requests.post(
            f"{self._base}/yeezy_products",
            headers={**self._headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
            params={"on_conflict": "product_id"},
            json=[
                {
                    "product_id": str(p["id"]),
                    "title": p.get("title", ""),
                    "price": p.get("price"),
                    "url": p.get("url", ""),
                    "image_url": p.get("image_url"),
                }
                for p in products
            ],
            timeout=15,
        )
        resp.raise_for_status()
