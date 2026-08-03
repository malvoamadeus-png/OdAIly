from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.paths import get_paths


def load_meme_scanner_database_path() -> Path:
    default_path = get_paths().processed_dir / "meme_scanner.sqlite3"
    return Path(os.getenv("MEME_SCANNER_DB_PATH") or default_path).expanduser().resolve()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


class MemeDashboardStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or load_meme_scanner_database_path()).expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def dashboard(self, *, limit: int = 100) -> dict[str, Any]:
        generated_at = datetime.now(UTC).isoformat()
        if not self.path.exists():
            return {
                "available": False,
                "generated_at": generated_at,
                "items": [],
                "last_error": "Meme scanner database is not available",
            }

        try:
            with self._connect() as connection:
                jobs = connection.execute(
                    """
                    SELECT id,address,trigger_key,trigger_level,payload_json,trigger_kind,
                           queued_at,status,reason,title,content,updated_at
                    FROM jobs
                    ORDER BY updated_at DESC,id DESC
                    LIMIT ?
                    """,
                    (max(1, min(int(limit), 200)),),
                ).fetchall()
                candidate_rows = connection.execute(
                    """
                    SELECT id,mention_count,chat_count,sender_count
                    FROM tg_candidates
                    WHERE id IN (
                      SELECT CAST(SUBSTR(trigger_key, 10) AS INTEGER)
                      FROM jobs WHERE trigger_kind='tg_burst' AND trigger_key LIKE 'tg_burst:%'
                    )
                    """
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            return {
                "available": False,
                "generated_at": generated_at,
                "items": [],
                "last_error": str(exc),
            }

        candidates = {int(row["id"]): dict(row) for row in candidate_rows}
        items: list[dict[str, Any]] = []
        for row in jobs:
            payload = _json_object(row["payload_json"])
            candidate: dict[str, Any] = {}
            if row["trigger_kind"] == "tg_burst":
                try:
                    candidate_id = int(str(row["trigger_key"]).split(":", 1)[1])
                except (IndexError, TypeError, ValueError):
                    candidate_id = 0
                candidate = candidates.get(candidate_id, {})
            items.append(
                {
                    "id": int(row["id"]),
                    "address": str(row["address"]),
                    "chain": "bsc",
                    "platform": str(payload.get("launchpad_platform") or payload.get("launchpad") or "telegram"),
                    "name": str(payload.get("name") or ""),
                    "symbol": str(payload.get("symbol") or payload.get("name") or "?"),
                    "market_cap": _number(payload.get("usd_market_cap") or payload.get("market_cap")),
                    "volume_24h": _number(payload.get("volume_24h")),
                    "trigger_kind": str(row["trigger_kind"]),
                    "trigger_level": _number(row["trigger_level"]),
                    "mention_count": candidate.get("mention_count"),
                    "chat_count": candidate.get("chat_count"),
                    "sender_count": candidate.get("sender_count"),
                    "status": str(row["status"]),
                    "reason": str(row["reason"] or ""),
                    "title": str(row["title"] or ""),
                    "content": str(row["content"] or ""),
                    "queued_at": str(row["queued_at"]),
                    "updated_at": str(row["updated_at"]),
                }
            )
        return {
            "available": True,
            "generated_at": generated_at,
            "items": items,
            "last_error": None,
        }
