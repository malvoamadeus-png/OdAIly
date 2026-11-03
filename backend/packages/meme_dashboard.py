from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.paths import get_paths


def load_meme_scanner_database_path() -> Path:
    default_path = get_paths().processed_dir / "meme_scanner.sqlite3"
    return Path(os.getenv("MEME_SCANNER_DB_PATH") or default_path).expanduser().resolve()


def load_meme_quote_audit_database_path() -> Path:
    default_path = get_paths().processed_dir / "meme_quote_audit.sqlite3"
    return Path(os.getenv("MEME_QUOTE_AUDIT_DB_PATH") or default_path).expanduser().resolve()


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
    if normalized in {"eth", "ethereum"}:
        return "eth"
    if normalized == "base":
        return "base"
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


def _current_market_observations(connection: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """Return the latest tracked market values without changing job snapshots.

    Only active observations belong to the scanner's current three-day
    tracking window.  Older/legacy rows remain useful audit data but must not
    make a historical job card look like it has a live price.
    """
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'"
    ).fetchone()
    if table is None:
        return {}
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(observations)")}
    required = {"address", "chain", "last_market_cap", "last_volume_24h", "last_seen_at", "tracking_status"}
    if not required.issubset(columns):
        return {}
    rows = connection.execute(
        """SELECT address, chain, last_market_cap, last_volume_24h, last_seen_at
           FROM observations WHERE tracking_status='active'"""
    ).fetchall()
    return {
        (_chain(row["chain"]), str(row["address"]).strip().lower()): {
            "market_cap": _number(row["last_market_cap"]),
            "volume_24h": _number(row["last_volume_24h"]),
            "observed_at": str(row["last_seen_at"] or ""),
        }
        for row in rows
    }


def _quote_audit_data(addresses: set[str]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    path = load_meme_quote_audit_database_path()
    empty_summary = {
        "available": False,
        "samples_24h": 0,
        "divergent_24h": 0,
        "pool_mismatches_24h": 0,
        "missing_values_24h": 0,
        "provider_calls_24h": {},
        "provider_failures_24h": {},
        "backlog": None,
    }
    if not path.exists():
        return {}, empty_summary
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=2000")
        try:
            samples = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='quote_samples'"
            ).fetchone()
            if samples is None:
                return {}, empty_summary
            latest: dict[str, dict[str, Any]] = {}
            if addresses:
                normalized = {address.lower() for address in addresses}
                placeholders = ",".join("?" for _ in normalized)
                rows = connection.execute(
                    f"""SELECT * FROM quote_samples WHERE lower(address) IN ({placeholders})
                        ORDER BY sampled_at DESC,id DESC""",
                    tuple(normalized),
                ).fetchall()
                for row in rows:
                    record = dict(row)
                    latest.setdefault(str(row["address"]).lower(), {
                        "sampled_at": record["sampled_at"],
                        "priority_reason": record["priority_reason"],
                        "comparison_status": record["comparison_status"],
                        "divergence_ratio": _number(record["divergence_ratio"]),
                        "supply_status": record["supply_status"],
                        "dexscreener": {
                            "sampled_at": record.get("dex_sampled_at"),
                            "status": record["dex_status"],
                            "price": _number(record["dex_price"]),
                            "market_cap": _number(record["dex_market_cap"]),
                            "pair_address": record["dex_pair_address"],
                            "liquidity": _number(record["dex_liquidity"]),
                        },
                        "geckoterminal": {
                            "sampled_at": record.get("gecko_sampled_at"),
                            "status": record["gecko_status"],
                            "price": _number(record["gecko_price"]),
                            "market_cap": _number(record["gecko_market_cap"]),
                            "pool_address": record["gecko_pool_address"],
                            "pool_reserve": _number(record["gecko_pool_reserve"]),
                            "pool_matched": bool(record["gecko_pool_matched"]),
                            "volume_m5": _number(record["gecko_volume_m5"]),
                        },
                        "jupiter": {
                            "sampled_at": record.get("jupiter_sampled_at"),
                            "status": record["jupiter_status"],
                            "price": _number(record["jupiter_price"]),
                            "market_cap": _number(record["jupiter_market_cap"]),
                            "liquidity": _number(record["jupiter_liquidity"]),
                            "block_id": record["jupiter_block_id"],
                        },
                    })

            since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
            counts = connection.execute(
                """SELECT COUNT(*) total,
                   SUM(CASE WHEN comparison_status='divergent' THEN 1 ELSE 0 END) divergent,
                   SUM(CASE WHEN gecko_pool_matched=0 AND gecko_status='success' THEN 1 ELSE 0 END) pool_mismatch,
                   SUM(CASE WHEN dex_status IN ('missing','incomplete') THEN 1 ELSE 0 END)
                   + SUM(CASE WHEN gecko_status IN ('missing','incomplete') THEN 1 ELSE 0 END)
                   + SUM(CASE WHEN jupiter_status IN ('missing','incomplete') THEN 1 ELSE 0 END) missing_values
                   FROM quote_samples WHERE sampled_at>=?""",
                (since,),
            ).fetchone()
            calls: dict[str, int] = {}
            failures: dict[str, int] = {}
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_calls'"
            ).fetchone():
                for row in connection.execute(
                    """SELECT provider,COUNT(*) count,
                       SUM(CASE WHEN outcome!='success' THEN 1 ELSE 0 END) failures
                       FROM provider_calls WHERE called_at>=? GROUP BY provider""",
                    (since,),
                ):
                    calls[str(row["provider"])] = int(row["count"])
                    failures[str(row["provider"])] = int(row["failures"] or 0)
            backlog_row = connection.execute(
                "SELECT value FROM audit_meta WHERE key='backlog'"
            ).fetchone() if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_meta'"
            ).fetchone() else None
            return latest, {
                "available": True,
                "samples_24h": int(counts["total"] or 0),
                "divergent_24h": int(counts["divergent"] or 0),
                "pool_mismatches_24h": int(counts["pool_mismatch"] or 0),
                "missing_values_24h": int(counts["missing_values"] or 0),
                "provider_calls_24h": calls,
                "provider_failures_24h": failures,
                "backlog": int(backlog_row["value"]) if backlog_row else 0,
            }
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return {}, empty_summary


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


def _row_item(
    row: sqlite3.Row,
    candidates: dict[int, dict[str, Any]],
    market_observations: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = _json_object(row["payload_json"])
    narrative = _json_object(row["narrative_json"])
    chain = _chain(payload.get("chain"))
    current_market = None if row["trigger_kind"] == "daily_hot" else (market_observations or {}).get((chain, str(row["address"]).strip().lower()))
    candidate: dict[str, Any] = {}
    if row["trigger_kind"] == "tg_burst":
        try:
            candidate_id = int(str(row["trigger_key"]).split(":", 1)[1])
        except (IndexError, TypeError, ValueError):
            candidate_id = 0
        candidate = candidates.get(candidate_id, {})
    quote_audit = (market_observations or {}).get(("quote_audit", str(row["address"]).strip().lower()))
    return {
        "id": int(row["id"]),
        "address": str(row["address"]),
        "chain": chain,
        "platform": str(payload.get("launchpad_platform") or payload.get("launchpad") or "telegram"),
        "name": str(payload.get("name") or ""),
        "symbol": str(payload.get("symbol") or payload.get("name") or "?"),
        "market_cap": current_market["market_cap"] if current_market else _number(payload.get("usd_market_cap") or payload.get("market_cap")),
        "volume_24h": current_market["volume_24h"] if current_market else _number(payload.get("volume_24h")),
        "market_observed_at": current_market["observed_at"] if current_market else None,
        "quote_audit": quote_audit,
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

    def dashboard(self, *, limit: int = 100, column: str = "all") -> dict[str, Any]:
        if column not in {"all", "digest", "daily_hot"}:
            raise ValueError("Invalid Meme column")
        column_clause = {"all": "1=1", "digest": "trigger_kind != 'daily_hot'",
                         "daily_hot": "trigger_kind = 'daily_hot'"}[column]
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
                    WHERE ({column_clause}) AND COALESCE(reason, '') NOT IN ('volume_gate_failed', 'tg_market_cap_gate_failed', 'unsupported_chain', 'token_not_found')
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
                market_observations = _current_market_observations(connection)
                quote_audit, quote_audit_summary = _quote_audit_data(
                    {str(row["address"]) for row in jobs if _chain(_json_object(row["payload_json"]).get("chain")) == "solana"}
                )
                market_observations.update({("quote_audit", address): data for address, data in quote_audit.items()})
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
            items.append(_row_item(row, candidates, market_observations))
        return {
            "available": True,
            "generated_at": generated_at,
            "items": items,
            "quote_audit_summary": quote_audit_summary,
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
                market_observations = _current_market_observations(connection)
        except (OSError, sqlite3.Error):
            return None

        candidates = {int(item["id"]): dict(item) for item in candidate_rows}
        payload = _json_object(row["narrative_json"])
        return {
            "available": bool(payload),
            "job": _row_item(row, candidates, market_observations),
            "narrative": payload if payload else None,
        }
