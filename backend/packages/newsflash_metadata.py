from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite


ORIGIN_LABELS = {
    "x": "X",
    "binance_square": "币安广场",
    "competitor": "竞品",
    "external_media": "外媒",
    "hot_topic_auto": "热点自动快讯",
    "hyperliquid_monitor": "Hyperliquid 监控",
    "gate_market": "Gate 行情",
    "stock_market": "股票行情",
    "meme": "Meme 速递",
    "scheduled_brief": "定时简报",
    "manual": "人工发布",
}

PRIORITY_LEVELS = {"high", "medium", "low"}
PRIORITY_STATUSES = {"evaluated", "not_applicable", "pending", "failed", "historical"}
DUPLICATE_STATUSES = {"clear", "duplicate", "not_checked", "pending", "historical"}
NEWSFLASH_METADATA_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS newsflash_production_metadata (
    delivery_key text PRIMARY KEY,
    origin_key text NOT NULL,
    odaily_id text,
    id_match_status text NOT NULL DEFAULT 'missing',
    title text NOT NULL,
    pushed_at text NOT NULL,
    priority_status text NOT NULL DEFAULT 'pending',
    priority_level text,
    priority_reason text,
    duplicate_status text NOT NULL DEFAULT 'historical',
    duplicate_target_type text,
    duplicate_target_id text,
    duplicate_target_title text,
    duplicate_reason text,
    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (priority_status IN ('evaluated','not_applicable','pending','failed','historical')),
    CHECK (id_match_status IN ('matched','missing','ambiguous')),
    CHECK (priority_level IS NULL OR priority_level IN ('high','medium','low')),
    CHECK (duplicate_status IN ('clear','duplicate','not_checked','pending','historical'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_newsflash_production_metadata_odaily_id
ON newsflash_production_metadata(odaily_id) WHERE odaily_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_newsflash_production_metadata_title_time
ON newsflash_production_metadata(title,pushed_at DESC);
CREATE TABLE IF NOT EXISTS newsflash_metadata_state (
    singleton_key text PRIMARY KEY CHECK (singleton_key='global'),
    enabled_at text NOT NULL
);
"""


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _response_ids(response_text: str | None) -> set[str]:
    if not response_text:
        return set()
    try:
        payload = json.loads(response_text)
    except (TypeError, json.JSONDecodeError):
        return set()
    found: set[str] = set()

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif key.casefold() in {"id", "newsflashid", "newsflash_id", "source_item_id"} and value is not None:
            text = str(value).strip()
            if text:
                found.add(text[:-2] if text.endswith(".0") and text[:-2].isdigit() else text)

    visit(payload)
    return found


def ensure_newsflash_metadata_schema(path: Path) -> None:
    with connect_sqlite(path) as conn:
        ensure_newsflash_metadata_tables(conn)
        conn.execute(
            "INSERT OR IGNORE INTO newsflash_metadata_state(singleton_key,enabled_at) VALUES ('global',?)",
            (datetime.now(UTC).isoformat(),),
        )
        conn.commit()


def ensure_newsflash_metadata_tables(conn) -> None:
    conn.executescript(NEWSFLASH_METADATA_SCHEMA_SQL)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(newsflash_production_metadata)")}
    if "id_match_status" not in columns:
        conn.execute(
            "ALTER TABLE newsflash_production_metadata "
            "ADD COLUMN id_match_status text NOT NULL DEFAULT 'missing' "
            "CHECK (id_match_status IN ('matched','missing','ambiguous'))"
        )
    if "duplicate_status" not in columns:
        conn.execute(
            "ALTER TABLE newsflash_production_metadata ADD COLUMN duplicate_status text NOT NULL "
            "DEFAULT 'historical' CHECK (duplicate_status IN ('clear','duplicate','not_checked','pending','historical'))"
        )
    for column in ("duplicate_target_type", "duplicate_target_id", "duplicate_target_title", "duplicate_reason"):
        if column not in columns:
            conn.execute(f"ALTER TABLE newsflash_production_metadata ADD COLUMN {column} text")


def _matching_metadata_rows(conn, *, title: str | None, published_at: Any) -> list[Any]:
    if not title:
        return []
    published = _parse_datetime(published_at)
    if published is None:
        return []
    candidates = conn.execute(
        "SELECT * FROM newsflash_production_metadata "
        "WHERE title=? AND odaily_id IS NULL AND id_match_status='missing'",
        (title,),
    ).fetchall()
    return [
        row for row in candidates
        if (pushed := _parse_datetime(row["pushed_at"])) is not None
        and abs((pushed - published).total_seconds()) <= 300
    ]


def _has_unresolved_metadata_for_title(conn, title: str | None) -> bool:
    if not title:
        return False
    return conn.execute(
        "SELECT 1 FROM newsflash_production_metadata WHERE title=? AND odaily_id IS NULL LIMIT 1",
        (title,),
    ).fetchone() is not None


def _matching_reference_rows(conn, *, title: str | None, published_at: Any) -> list[Any]:
    if not title:
        return []
    published = _parse_datetime(published_at)
    if published is None:
        return []
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='odaily_reference_items'"
    ).fetchone()
    if not exists:
        return []
    references = conn.execute(
        "SELECT source_item_id,published_at FROM odaily_reference_items WHERE title=?",
        (title,),
    ).fetchall()
    return [
        row for row in references
        if (reference_time := _parse_datetime(row["published_at"])) is not None
        and abs((reference_time - published).total_seconds()) <= 300
    ]


def _try_link_metadata(conn, row: Any, source_item_id: str) -> bool:
    try:
        cursor = conn.execute(
            "UPDATE newsflash_production_metadata SET odaily_id=?,id_match_status='matched',updated_at=CURRENT_TIMESTAMP "
            "WHERE delivery_key=? AND odaily_id IS NULL",
            (source_item_id, row["delivery_key"]),
        )
        return cursor.rowcount == 1
    except sqlite3.IntegrityError:
        # A conflicting Odaily ID means this candidate cannot be linked safely.
        return False


def record_newsflash_delivery(
    path: Path,
    *,
    delivery_key: str | None,
    origin_key: str,
    title: str,
    response_text: str | None,
    pushed_at: str | None = None,
    priority_status: str = "pending",
    priority_level: str | None = None,
    priority_reason: str | None = None,
    duplicate_result: dict[str, Any] | None = None,
) -> str:
    """Persist origin, priority and duplicate-check metadata after a successful push."""
    ensure_newsflash_metadata_schema(path)
    key = str(delivery_key or uuid.uuid4().hex)
    stamp = pushed_at or datetime.now(UTC).isoformat()
    ids = _response_ids(response_text)
    odaily_id = next(iter(ids)) if len(ids) == 1 else None
    id_match_status = "matched" if odaily_id is not None else "ambiguous" if ids else "missing"
    if priority_status not in PRIORITY_STATUSES:
        priority_status = "failed"
    if priority_level not in PRIORITY_LEVELS:
        priority_level = None
    if priority_status != "evaluated":
        priority_level = None
        priority_reason = None
    duplicate_status = "not_checked"
    duplicate_target_type = duplicate_target_id = duplicate_target_title = duplicate_reason = None
    if isinstance(duplicate_result, dict) and isinstance(duplicate_result.get("is_duplicate"), bool):
        duplicate_status = "duplicate" if duplicate_result["is_duplicate"] else "clear"
        target = duplicate_result.get("duplicate_target")
        target = target if isinstance(target, dict) else {}
        duplicate_target_type = str(duplicate_result.get("duplicate_target_type") or target.get("target_type") or "") or None
        duplicate_target_id = str(duplicate_result.get("duplicate_target_id") or target.get("target_id") or "") or None
        duplicate_target_title = str(target.get("title") or "") or None
        duplicate_reason = str(duplicate_result.get("reason") or "") or None
        duplicate_target_title = duplicate_target_title[:500] if duplicate_target_title else None
        duplicate_reason = duplicate_reason[:500] if duplicate_reason else None

    with connect_sqlite(path) as conn:
        conn.execute(
            """INSERT INTO newsflash_production_metadata(
                delivery_key,origin_key,odaily_id,id_match_status,title,pushed_at,
                priority_status,priority_level,priority_reason,duplicate_status,
                duplicate_target_type,duplicate_target_id,duplicate_target_title,duplicate_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(delivery_key) DO UPDATE SET
                origin_key=excluded.origin_key,
                odaily_id=COALESCE(excluded.odaily_id,newsflash_production_metadata.odaily_id),
                id_match_status=CASE
                    WHEN COALESCE(excluded.odaily_id,newsflash_production_metadata.odaily_id) IS NOT NULL THEN 'matched'
                    WHEN excluded.id_match_status='ambiguous' THEN 'ambiguous'
                    ELSE newsflash_production_metadata.id_match_status END,
                title=excluded.title,
                pushed_at=excluded.pushed_at,
                priority_status=excluded.priority_status,
                priority_level=excluded.priority_level,
                priority_reason=excluded.priority_reason,
                duplicate_status=excluded.duplicate_status,
                duplicate_target_type=excluded.duplicate_target_type,
                duplicate_target_id=excluded.duplicate_target_id,
                duplicate_target_title=excluded.duplicate_target_title,
                duplicate_reason=excluded.duplicate_reason,
                updated_at=CURRENT_TIMESTAMP""",
            (key, origin_key, odaily_id, id_match_status, title, stamp, priority_status, priority_level,
             priority_reason, duplicate_status, duplicate_target_type, duplicate_target_id,
             duplicate_target_title, duplicate_reason),
        )
        if id_match_status == "missing":
            matches = _matching_metadata_rows(conn, title=title, published_at=stamp)
            matching_refs = _matching_reference_rows(conn, title=title, published_at=stamp)
            if len(matches) == 1 and len(matching_refs) == 1:
                _try_link_metadata(conn, matches[0], str(matching_refs[0]["source_item_id"]))
        conn.commit()
    return key


def update_newsflash_priority(
    path: Path,
    *,
    delivery_key: str,
    status: str,
    level: str | None = None,
    reason: str | None = None,
) -> bool:
    ensure_newsflash_metadata_schema(path)
    if status not in PRIORITY_STATUSES:
        status = "failed"
    if level not in PRIORITY_LEVELS:
        level = None
    if status != "evaluated":
        level = None
        reason = None
    with connect_sqlite(path) as conn:
        cursor = conn.execute(
            "UPDATE newsflash_production_metadata SET priority_status=?,priority_level=?,priority_reason=?,updated_at=CURRENT_TIMESTAMP WHERE delivery_key=?",
            (status, level, reason, delivery_key),
        )
        conn.commit()
        return cursor.rowcount > 0


def metadata_for_reference(
    conn,
    *,
    source_item_id: str,
    title: str | None,
    published_at: Any,
    publisher_kind: str | None,
    operation_created_at: Any,
) -> dict[str, Any]:
    state = conn.execute("SELECT enabled_at FROM newsflash_metadata_state WHERE singleton_key='global'").fetchone()
    enabled_at = _parse_datetime(state["enabled_at"]) if state else None
    direct = conn.execute(
        "SELECT * FROM newsflash_production_metadata WHERE odaily_id=?",
        (source_item_id,),
    ).fetchone()
    if direct:
        row = direct
    else:
        candidates = _matching_metadata_rows(conn, title=title, published_at=published_at)
        if len(candidates) == 1:
            candidate = candidates[0]
            references = _matching_reference_rows(conn, title=title, published_at=published_at)
            if (
                len(references) == 1
                and str(references[0]["source_item_id"]) == source_item_id
                and _try_link_metadata(conn, candidate, source_item_id)
            ):
                row = dict(candidate)
                row["odaily_id"] = source_item_id
            else:
                return {
                    "origin_key": None,
                    "origin_label": "来源待匹配",
                    "origin_status": "pending",
                    "priority_status": "pending",
                    "priority_level": None,
                    "priority_reason": None,
                    "duplicate_status": "pending",
                    "duplicate_target_type": None,
                    "duplicate_target_id": None,
                    "duplicate_target_title": None,
                    "duplicate_reason": None,
                }
        elif candidates:
            return {
                "origin_key": None,
                "origin_label": "来源待匹配",
                "origin_status": "pending",
                "priority_status": "pending",
                "priority_level": None,
                "priority_reason": None,
                "duplicate_status": "pending",
                "duplicate_target_type": None,
                "duplicate_target_id": None,
                "duplicate_target_title": None,
                "duplicate_reason": None,
            }
        else:
            if _has_unresolved_metadata_for_title(conn, title):
                return {
                    "origin_key": None,
                    "origin_label": "来源待匹配",
                    "origin_status": "pending",
                    "priority_status": "pending",
                    "priority_level": None,
                    "priority_reason": None,
                    "duplicate_status": "pending",
                    "duplicate_target_type": None,
                    "duplicate_target_id": None,
                    "duplicate_target_title": None,
                    "duplicate_reason": None,
                }
            created = _parse_datetime(operation_created_at)
            published = _parse_datetime(published_at)
            if publisher_kind == "human" and enabled_at and created and published and created >= enabled_at and published >= enabled_at:
                return {
                    "origin_key": "manual",
                    "origin_label": ORIGIN_LABELS["manual"],
                    "origin_status": "matched",
                    "priority_status": "not_applicable",
                    "priority_level": None,
                    "priority_reason": None,
                    "duplicate_status": "not_checked",
                    "duplicate_target_type": None,
                    "duplicate_target_id": None,
                    "duplicate_target_title": None,
                    "duplicate_reason": None,
                }
            return {
                "origin_key": None,
                "origin_label": "历史未记录",
                "origin_status": "historical",
                "priority_status": "historical",
                "priority_level": None,
                "priority_reason": None,
                "duplicate_status": "historical",
                "duplicate_target_type": None,
                "duplicate_target_id": None,
                "duplicate_target_title": None,
                "duplicate_reason": None,
            }
    status = str(row["priority_status"] or "pending")
    return {
        "origin_key": str(row["origin_key"]),
        "origin_label": ORIGIN_LABELS.get(str(row["origin_key"]), str(row["origin_key"])),
        "origin_status": "matched" if row["odaily_id"] else "pending",
        "priority_status": status,
        "priority_level": row["priority_level"] if status == "evaluated" else None,
        "priority_reason": row["priority_reason"] if status == "evaluated" else None,
        "duplicate_status": row["duplicate_status"] if row["duplicate_status"] in DUPLICATE_STATUSES else "historical",
        "duplicate_target_type": row["duplicate_target_type"],
        "duplicate_target_id": row["duplicate_target_id"],
        "duplicate_target_title": row["duplicate_target_title"],
        "duplicate_reason": row["duplicate_reason"],
    }
