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


def _chain(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    if normalized in {"sol", "solana"}:
        return "solana"
    if normalized in {"robinhood", "robinhood-chain", "robinhoodchain"}:
        return "robinhood"
    return "bsc"


def _narrative_summary(value: Any) -> dict[str, Any]:
    payload = _json_object(value)
    if not payload:
        return {
            "narrative_available": False,
            "narrative_status": None,
            "failure_stage": None,
            "failure_code": None,
            "primary_type": None,
            "type_hypothesis": None,
        }
    grok_research = payload.get("grok_research") if isinstance(payload.get("grok_research"), dict) else {}
    status = str(payload.get("status") or ("success" if str(payload.get("reader_text") or "").strip() else "empty"))
    return {
        "narrative_available": True,
        "narrative_status": status,
        "failure_stage": str(payload.get("failure_stage") or "") or None,
        "failure_code": str(payload.get("failure_code") or payload.get("decision_code") or "") or None,
        "primary_type": str(payload.get("primary_type") or "") or None,
        "type_hypothesis": str(payload.get("type_hypothesis") or grok_research.get("type_hypothesis") or "") or None,
    }


def _job_columns(connection: sqlite3.Connection) -> str:
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(jobs)")}
    selected = []
    for column in ("narrative_json", "processing_started_at", "publishing_started_at", "completed_at"):
        selected.append(column if column in columns else f"NULL AS {column}")
    return ",".join(selected)


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _duration_ms(start: Any, end: Any) -> int | None:
    started = _parse_timestamp(start)
    finished = _parse_timestamp(end)
    if started is None or finished is None:
        return None
    return max(0, round((finished - started).total_seconds() * 1000))


def _timing_summary(row: sqlite3.Row, narrative: dict[str, Any]) -> dict[str, Any]:
    performance = narrative.get("performance") if isinstance(narrative.get("performance"), dict) else {}
    return {
        "queued_at": str(row["queued_at"] or ""),
        "processing_started_at": row["processing_started_at"],
        "publishing_started_at": row["publishing_started_at"],
        "completed_at": row["completed_at"],
        "queue_duration_ms": _duration_ms(row["queued_at"], row["processing_started_at"]),
        "narrative_duration_ms": _number(performance.get("total_duration_ms")),
        "publishing_duration_ms": _duration_ms(row["publishing_started_at"], row["completed_at"]),
        "total_duration_ms": _duration_ms(row["queued_at"], row["completed_at"]),
    }


def _row_item(row: sqlite3.Row, candidates: dict[int, dict[str, Any]]) -> dict[str, Any]:
    payload = _json_object(row["payload_json"])
    narrative = _json_object(row["narrative_json"])
    candidate: dict[str, Any] = {}
    if row["trigger_kind"] == "tg_burst":
        try:
            candidate_id = int(str(row["trigger_key"]).split(":", 1)[1])
        except (IndexError, TypeError, ValueError):
            candidate_id = 0
        candidate = candidates.get(candidate_id, {})
    return {
        "id": int(row["id"]),
        "address": str(row["address"]),
        "chain": _chain(payload.get("chain")),
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
        "timing": _timing_summary(row, narrative),
        **_narrative_summary(row["narrative_json"]),
    }


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
                    f"""
                    SELECT id,address,trigger_key,trigger_level,payload_json,trigger_kind,
                           queued_at,status,reason,title,content,updated_at,{_job_columns(connection)}
                    FROM jobs
                    WHERE COALESCE(reason, '') NOT IN ('volume_gate_failed', 'tg_market_cap_gate_failed', 'unsupported_chain', 'token_not_found')
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
            items.append(_row_item(row, candidates))
        return {
            "available": True,
            "generated_at": generated_at,
            "items": items,
            "last_error": None,
        }

    def narrative_detail(self, job_id: int) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            with self._connect() as connection:
                narrative_column = _job_columns(connection)
                row = connection.execute(
                    f"""
                    SELECT id,address,trigger_key,trigger_level,payload_json,trigger_kind,
                           queued_at,status,reason,title,content,updated_at,{narrative_column}
                    FROM jobs WHERE id=?
                    """,
                    (int(job_id),),
                ).fetchone()
                if row is None:
                    return None
                candidate_rows = connection.execute(
                    """
                    SELECT id,mention_count,chat_count,sender_count
                    FROM tg_candidates
                    WHERE id = CAST(SUBSTR(?, 10) AS INTEGER)
                    """,
                    (str(row["trigger_key"] or ""),),
                ).fetchall()
        except (OSError, sqlite3.Error):
            return None

        candidates = {int(item["id"]): dict(item) for item in candidate_rows}
        payload = _json_object(row["narrative_json"])
        return {
            "available": bool(payload),
            "job": _row_item(row, candidates),
            "narrative": payload if payload else None,
        }
