from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.storage import ClosingSQLiteConnection
from .error_policy import day_start_ms, external_error, expire_external_errors, failure_category, error_messages, scrub_external_errors

SQLITE_BUSY_TIMEOUT_MS = 60_000


def timestamp_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number * 1000 if abs(number) < 10_000_000_000 else number
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe_json(item)
            for key, item in value.items()
            if not re.search(r"token|secret|password|cookie|authorization|api.?key|credential", str(key), re.I)
        }
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    return value


def _search_terms(text: str) -> set[str]:
    normalized = text.casefold()
    return {
        normalized[index:index + 2]
        for index in range(len(normalized) - 1)
        if not normalized[index:index + 2].isspace()
    }


def _searchable_text(fields: tuple[Any, ...], evidence: Any) -> str:
    parts = [str(value) for value in fields if value not in (None, "")]
    if evidence is not None:
        parts.append(json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), default=str))
    return " ".join(parts).casefold()


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SystemInformationStore:
    def __init__(self, database_path: Path, evidence_dir: Path | None = None) -> None:
        self.database_path = database_path
        self.evidence_dir = evidence_dir or database_path.parent / "system_information" / "evidence"

    def connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.database_path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000, factory=ClosingSQLiteConnection,
        )
        self.database_path.chmod(0o600)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS system_information_items (
                    item_id TEXT PRIMARY KEY,
                    business_key TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_id TEXT,
                    task_id INTEGER,
                    title TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    source_url TEXT,
                    stage TEXT NOT NULL DEFAULT 'unknown',
                    status TEXT NOT NULL DEFAULT 'unknown',
                    result TEXT NOT NULL DEFAULT 'unknown',
                    failure_category TEXT,
                    created_at_ms INTEGER,
                    updated_at_ms INTEGER,
                    observed_at_ms INTEGER NOT NULL,
                    source_database TEXT NOT NULL,
                    source_table TEXT NOT NULL,
                    source_row_id TEXT NOT NULL,
                    evidence_ref TEXT,
                    fingerprint TEXT NOT NULL,
                    legacy_history INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_system_information_created
                    ON system_information_items(created_at_ms DESC, item_id DESC);
                CREATE INDEX IF NOT EXISTS idx_system_information_activity
                    ON system_information_items(updated_at_ms DESC, item_id DESC);
                CREATE INDEX IF NOT EXISTS idx_system_information_business
                    ON system_information_items(business_key, created_at_ms DESC, item_id DESC);
                CREATE INDEX IF NOT EXISTS idx_system_information_business_source
                    ON system_information_items(business_key, source);
                CREATE INDEX IF NOT EXISTS idx_system_information_business_activity
                    ON system_information_items(business_key, updated_at_ms DESC, item_id DESC);
                CREATE INDEX IF NOT EXISTS idx_system_information_status
                    ON system_information_items(status, created_at_ms DESC, item_id DESC);
                CREATE INDEX IF NOT EXISTS idx_system_information_source
                    ON system_information_items(source, created_at_ms DESC, item_id DESC);
                CREATE INDEX IF NOT EXISTS idx_system_information_task
                    ON system_information_items(task_id);

                CREATE TABLE IF NOT EXISTS system_information_events (
                    event_id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL REFERENCES system_information_items(item_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT NOT NULL,
                    happened_at_ms INTEGER,
                    observed_at_ms INTEGER NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    reason TEXT,
                    error_code TEXT,
                    evidence_ref TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_system_information_events_item
                    ON system_information_events(item_id, happened_at_ms, event_id);

                CREATE TABLE IF NOT EXISTS system_information_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS system_information_search (
                    term TEXT NOT NULL,
                    item_id TEXT NOT NULL REFERENCES system_information_items(item_id) ON DELETE CASCADE,
                    PRIMARY KEY(term, item_id)
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS idx_system_information_search_item
                    ON system_information_search(item_id);
                CREATE TABLE IF NOT EXISTS system_information_evidence (
                    evidence_ref TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    created_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS system_information_evidence_pins (
                    pin_key TEXT PRIMARY KEY,
                    evidence_ref TEXT NOT NULL REFERENCES system_information_evidence(evidence_ref),
                    pinned_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS system_information_storage_samples (
                    sample_date TEXT PRIMARY KEY,
                    database_bytes INTEGER NOT NULL,
                    evidence_bytes INTEGER NOT NULL,
                    sampled_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS system_information_decision_samples (
                    sample_key TEXT PRIMARY KEY,
                    sample_date TEXT NOT NULL,
                    sampled_at_ms INTEGER NOT NULL,
                    raw_expires_at_ms INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_id TEXT,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT NOT NULL,
                    reason_summary TEXT NOT NULL DEFAULT '',
                    rule_version TEXT,
                    evidence_ref TEXT,
                    evidence_bytes INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_system_information_decision_samples_date
                    ON system_information_decision_samples(sampled_at_ms DESC, business_key, source, result);
                """
            )
            legacy_position_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_information_search_positions'"
            ).fetchone()
            started = conn.execute(
                "SELECT state_value FROM system_information_state WHERE state_key='search_bigrams_rebuild_started'"
            ).fetchone()
            done = conn.execute(
                "SELECT state_value FROM system_information_state WHERE state_key='search_bigrams_rebuild_done'"
            ).fetchone()
            if started is None:
                ready = not legacy_position_table
                conn.execute(
                    "INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES(?,?,?)",
                    ("search_bigrams_rebuild_started", "1" if ready else "0", int(datetime.now(UTC).timestamp() * 1000)),
                )
                conn.execute(
                    "INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES(?,?,?)",
                    ("search_bigrams_rebuild_done", "1" if ready else "0", int(datetime.now(UTC).timestamp() * 1000)),
                )
            elif done is None:
                conn.execute(
                    "INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES(?,?,?)",
                    ("search_bigrams_rebuild_done", "0", int(datetime.now(UTC).timestamp() * 1000)),
                )
        if not self.get_state("observability_started_at"):
            self.set_state("observability_started_at", int(datetime.now(UTC).timestamp() * 1000))
        with self.connect() as conn:
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(system_information_items)')}
            for name, definition in (
                ('news_visible', 'INTEGER NOT NULL DEFAULT 0'), ('admission', "TEXT NOT NULL DEFAULT 'unassessed'"),
                ('parent_item_id', 'TEXT'), ('duplicate_json', 'TEXT'),
                ('source_name', "TEXT NOT NULL DEFAULT ''"), ('source_updated_at_ms', 'INTEGER'),
            ):
                if name not in columns:
                    conn.execute(f'ALTER TABLE system_information_items ADD COLUMN {name} {definition}')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_system_information_news_activity ON system_information_items(updated_at_ms DESC,item_id DESC) WHERE news_visible=1 AND parent_item_id IS NULL')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_system_information_parent ON system_information_items(parent_item_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_system_information_origin ON system_information_items(source_database,source_table,source_row_id)')
            for table in ('system_information_items', 'system_information_events',
                          'system_information_decision_samples', 'system_information_evidence_pins'):
                conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_evidence ON {table}(evidence_ref)')
            conn.execute('''CREATE TABLE IF NOT EXISTS system_information_external_error_daily (
                database_key TEXT NOT NULL,error_date TEXT NOT NULL,source TEXT NOT NULL,stage TEXT NOT NULL,
                category TEXT NOT NULL,occurrences INTEGER NOT NULL,
                PRIMARY KEY(database_key,error_date,source,stage,category))''')
            version = conn.execute("SELECT 1 FROM system_information_state WHERE state_key='news_projection_v1'").fetchone()
            if version is None:
                conn.execute("UPDATE system_information_state SET state_value='0' WHERE state_key='main_tasks_watermark' OR (state_key LIKE 'adapter_%_watermark')")
                conn.execute("INSERT INTO system_information_state VALUES('news_projection_v1','1',?)", (int(datetime.now(UTC).timestamp()*1000),))

    def save_evidence(self, evidence: Any) -> str:
        safe = _safe_json(evidence)
        payload = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        directory = self.evidence_dir / digest[:2]
        path = directory / f"{digest}.json.gz"
        if not path.exists():
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(gzip.compress(payload, compresslevel=6, mtime=0))
            path.chmod(0o600)
        with self.connect() as conn:
            self._register_evidence(conn, digest, path, len(payload))
        return digest

    def pin_evidence(self, evidence_ref: str, pin_key: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO system_information_evidence_pins(pin_key,evidence_ref,pinned_at_ms) VALUES(?,?,?) "
                "ON CONFLICT(pin_key) DO UPDATE SET evidence_ref=excluded.evidence_ref,pinned_at_ms=excluded.pinned_at_ms",
                (pin_key, evidence_ref, int(datetime.now(UTC).timestamp() * 1000)),
            )

    def unpin_evidence(self, pin_key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM system_information_evidence_pins WHERE pin_key=?", (pin_key,))

    @staticmethod
    def _register_evidence(conn: sqlite3.Connection, digest: str, path: Path, size: int) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO system_information_evidence(evidence_ref,path,byte_size,created_at_ms) VALUES (?,?,?,?)",
            (digest, str(path), size, int(datetime.now(UTC).timestamp() * 1000)),
        )

    def _write_evidence_file(self, evidence: Any) -> tuple[str, Path, int]:
        safe = _safe_json(evidence)
        payload = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        directory = self.evidence_dir / digest[:2]
        path = directory / f"{digest}.json.gz"
        if not path.exists():
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(gzip.compress(payload, compresslevel=6, mtime=0))
            path.chmod(0o600)
        return digest, path, len(payload)

    def load_evidence(self, evidence_ref: str | None) -> Any:
        if not evidence_ref:
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT path FROM system_information_evidence WHERE evidence_ref=?", (evidence_ref,)).fetchone()
        if not row:
            return None
        return self._read_evidence_file(row["path"], evidence_ref)

    @staticmethod
    def _read_evidence_file(path: str, evidence_ref: str) -> Any:
        try:
            return json.loads(gzip.decompress(Path(path).read_bytes()))
        except (OSError, EOFError, json.JSONDecodeError):
            return {"unavailable": True, "evidence_ref": evidence_ref}

    @staticmethod
    def _compact_evidence(value: Any) -> Any:
        if isinstance(value, list):
            return [SystemInformationStore._compact_evidence(item) for item in value]
        if not isinstance(value, dict):
            return value
        compacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized == "content" and isinstance(item, str) and len(item) > 2000:
                compacted["content_excerpt"] = item[:2000]
                compacted["content_sha256"] = hashlib.sha256(item.encode("utf-8")).hexdigest()
            elif normalized == "raw_model_response" and isinstance(item, str):
                compacted["raw_model_response_excerpt"] = item[:1000]
                compacted["raw_model_response_sha256"] = hashlib.sha256(item.encode("utf-8")).hexdigest()
                compacted["raw_model_response_bytes"] = len(item.encode("utf-8"))
            elif normalized in {"writer_output", "model_response", "response_text", "raw_output"} and isinstance(item, str):
                compacted[f"{key}_excerpt"] = item[:1000]
                compacted[f"{key}_sha256"] = hashlib.sha256(item.encode("utf-8")).hexdigest()
            else:
                compacted[str(key)] = SystemInformationStore._compact_evidence(item)
        return compacted

    def cleanup_expired_evidence(self, *, now_ms: int | None = None, retain_days: int = 90, limit: int = 200, dry_run: bool = True) -> dict[str, int]:
        now = now_ms or int(datetime.now(UTC).timestamp() * 1000)
        self.expire_decision_samples(now_ms=now)
        cutoff = now - max(1, retain_days) * 24 * 60 * 60 * 1000
        compacted = compacted_bytes = protected = orphaned = orphaned_bytes = 0
        delete_after_commit: list[Path] = []
        with self.connect() as conn:
            evidence_rows = conn.execute(
                "SELECT evidence_ref,path,byte_size,created_at_ms FROM system_information_evidence "
                "WHERE created_at_ms<? ORDER BY created_at_ms,evidence_ref LIMIT ?",
                (cutoff, max(1, min(2000, limit))),
            ).fetchall()
            for row in evidence_rows:
                ref = str(row["evidence_ref"])
                item_refs = conn.execute(
                    "SELECT i.status,i.result FROM system_information_items i WHERE i.evidence_ref=?",
                    (ref,),
                ).fetchall()
                event_refs = conn.execute(
                    "SELECT e.status,e.result FROM system_information_events e WHERE e.evidence_ref=?",
                    (ref,),
                ).fetchall()
                attempt_refs = []
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_review_analysis_attempts'").fetchone():
                    attempt_refs = conn.execute(
                        "SELECT status,'review_evidence' AS result FROM system_review_analysis_attempts WHERE raw_response_ref=?",
                        (ref,),
                    ).fetchall()
                pin_refs = conn.execute(
                    "SELECT 'pinned' AS status,'source_evidence' AS result FROM system_information_evidence_pins WHERE evidence_ref=?",
                    (ref,),
                ).fetchall()
                references = [dict(item) for item in (*item_refs, *event_refs, *attempt_refs, *pin_refs)]
                if any(
                    reference["status"] in {"pending", "processing", "running", "queued", "waiting", "retry"}
                    or reference["result"] in {"processing", "unknown", "backoffice"}
                    for reference in references
                ):
                    protected += 1
                    continue
                path = Path(row["path"])
                try:
                    payload = json.loads(gzip.decompress(path.read_bytes()))
                except (OSError, EOFError, json.JSONDecodeError):
                    payload = {"unavailable": True, "evidence_ref": ref}
                compact_payload = self._compact_evidence(payload)
                if compact_payload == payload:
                    continue
                if dry_run:
                    serialized = json.dumps(compact_payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
                    compact_size = len(serialized)
                    compacted += 1
                    compacted_bytes += max(0, int(row["byte_size"]) - compact_size)
                    continue
                compact_ref, compact_path, compact_size = self._write_evidence_file(compact_payload)
                self._register_evidence(conn, compact_ref, compact_path, compact_size)
                conn.execute("UPDATE system_information_items SET evidence_ref=? WHERE evidence_ref=?", (compact_ref, ref))
                conn.execute("UPDATE system_information_events SET evidence_ref=? WHERE evidence_ref=?", (compact_ref, ref))
                conn.execute("UPDATE system_information_decision_samples SET evidence_ref=? WHERE evidence_ref=?", (compact_ref, ref))
                if attempt_refs:
                    conn.execute("UPDATE system_review_analysis_attempts SET raw_response_ref=? WHERE raw_response_ref=?", (compact_ref, ref))
                conn.execute("UPDATE system_information_evidence_pins SET evidence_ref=? WHERE evidence_ref=?", (compact_ref, ref))
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_review_analysis_attempts'").fetchone():
                    still_referenced = conn.execute(
                        "SELECT 1 FROM system_information_items WHERE evidence_ref=? UNION ALL "
                        "SELECT 1 FROM system_information_events WHERE evidence_ref=? UNION ALL "
                        "SELECT 1 FROM system_information_decision_samples WHERE evidence_ref=? UNION ALL "
                        "SELECT 1 FROM system_review_analysis_attempts WHERE raw_response_ref=? UNION ALL "
                        "SELECT 1 FROM system_information_evidence_pins WHERE evidence_ref=? LIMIT 1",
                        (ref, ref, ref, ref, ref),
                    ).fetchone()
                else:
                    still_referenced = conn.execute(
                        "SELECT 1 FROM system_information_items WHERE evidence_ref=? UNION ALL "
                        "SELECT 1 FROM system_information_events WHERE evidence_ref=? UNION ALL "
                        "SELECT 1 FROM system_information_decision_samples WHERE evidence_ref=? UNION ALL "
                        "SELECT 1 FROM system_information_evidence_pins WHERE evidence_ref=? LIMIT 1", (ref, ref, ref, ref),
                    ).fetchone()
                if not still_referenced:
                    conn.execute("DELETE FROM system_information_evidence WHERE evidence_ref=?", (ref,))
                    delete_after_commit.append(path)
                compacted += 1
                compacted_bytes += max(0, int(row["byte_size"]) - compact_size)

            if not dry_run:
                referenced = {
                    str(row[0]) for row in conn.execute(
                        "SELECT evidence_ref FROM system_information_items WHERE evidence_ref IS NOT NULL "
                        "UNION SELECT evidence_ref FROM system_information_events WHERE evidence_ref IS NOT NULL "
                        "UNION SELECT evidence_ref FROM system_information_decision_samples WHERE evidence_ref IS NOT NULL"
                    )
                }
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_review_analysis_attempts'").fetchone():
                    referenced.update(str(row[0]) for row in conn.execute(
                        "SELECT raw_response_ref FROM system_review_analysis_attempts WHERE raw_response_ref IS NOT NULL"
                    ))
                referenced.update(str(row[0]) for row in conn.execute(
                    "SELECT evidence_ref FROM system_information_evidence_pins"
                ))
                for row in conn.execute("SELECT evidence_ref,path,byte_size FROM system_information_evidence").fetchall():
                    ref = str(row["evidence_ref"])
                    if ref not in referenced:
                        path = Path(row["path"])
                        conn.execute("DELETE FROM system_information_evidence WHERE evidence_ref=?", (ref,))
                        delete_after_commit.append(path)
                        orphaned += 1
                        orphaned_bytes += int(row["byte_size"])
            conn.commit()
        for path in delete_after_commit:
            path.unlink(missing_ok=True)
        return {"compacted": compacted, "compacted_bytes": compacted_bytes, "protected": protected,
                "orphaned": orphaned, "orphaned_bytes": orphaned_bytes, "dry_run": int(dry_run)}

    def record_decision_samples(
        self,
        *,
        now_ms: int | None = None,
        limit: int = 50,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> dict[str, int]:
        now = now_ms or int(datetime.now(UTC).timestamp() * 1000)
        sample_date = datetime.fromtimestamp(now / 1000, UTC).date().isoformat()
        sample_cutoff = now - 24 * 60 * 60 * 1000
        with self.connect() as conn:
            state = conn.execute(
                "SELECT state_value FROM system_information_state WHERE state_key='decision_sample_date'",
            ).fetchone()
            existing = int(conn.execute(
                "SELECT COUNT(*) FROM system_information_decision_samples WHERE sample_date=?", (sample_date,),
            ).fetchone()[0])
            if state and state["state_value"] == sample_date:
                return {"sampled": existing, "evidence_bytes": 0, "already_sampled": 1}
            conn.execute("DELETE FROM system_information_decision_samples WHERE sampled_at_ms<?", (now - 30 * 86_400_000,))
            rows = conn.execute(
                "SELECT i.item_id,i.business_key,i.source,i.source_id,i.stage,i.status,i.result,i.summary,i.evidence_ref,e.byte_size "
                "FROM system_information_items i JOIN system_information_evidence e ON e.evidence_ref=i.evidence_ref "
                "WHERE i.updated_at_ms>=? AND (i.result='stopped' OR i.status IN ('no_candidate','ignored','discarded','deduped','duplicate')) "
                "ORDER BY i.item_id",
                (sample_cutoff,),
            ).fetchall()
            strata: dict[tuple[str, str, str, str], list[sqlite3.Row]] = {}
            for row in rows:
                key = (str(row["business_key"]), str(row["source"]), str(row["status"]), str(row["result"]))
                strata.setdefault(key, []).append(row)
            for key, values in strata.items():
                values.sort(key=lambda row: hashlib.sha256(f"{sample_date}:{key}:{row['item_id']}".encode()).digest())

            sample_limit = max(1, min(50, int(limit)))
            byte_limit = max(1, min(20 * 1024 * 1024, int(max_bytes)))
            sampled: list[sqlite3.Row] = []
            total_bytes = 0
            remaining = sorted(strata)
            while remaining and len(sampled) < sample_limit:
                next_round = []
                for key in remaining:
                    candidates = strata[key]
                    while candidates:
                        candidate = candidates.pop(0)
                        size = max(0, int(candidate["byte_size"] or 0))
                        if total_bytes + size <= byte_limit:
                            sampled.append(candidate)
                            total_bytes += size
                            break
                    if candidates:
                        next_round.append(key)
                remaining = next_round

            for row in sampled:
                item_id = str(row["item_id"])
                sample_key = hashlib.sha256(f"{sample_date}:{item_id}".encode()).hexdigest()
                evidence_ref = str(row["evidence_ref"])
                conn.execute(
                    "INSERT OR IGNORE INTO system_information_decision_samples("
                    "sample_key,sample_date,sampled_at_ms,raw_expires_at_ms,item_id,business_key,source,source_id,stage,status,result,reason_summary,rule_version,evidence_ref,evidence_bytes) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sample_key, sample_date, now, now + 14 * 86_400_000, item_id, row["business_key"], row["source"],
                     row["source_id"], row["stage"], row["status"], row["result"], str(row["summary"] or "")[:1200],
                     self._sample_rule_version(evidence_ref), evidence_ref, int(row["byte_size"] or 0)),
                )
                conn.execute(
                    "INSERT INTO system_information_evidence_pins(pin_key,evidence_ref,pinned_at_ms) VALUES(?,?,?) "
                    "ON CONFLICT(pin_key) DO UPDATE SET evidence_ref=excluded.evidence_ref,pinned_at_ms=excluded.pinned_at_ms",
                    (f"decision-sample:{sample_key}", evidence_ref, now),
                )
            conn.execute(
                "INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('decision_sample_date',?,?) "
                "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms",
                (sample_date, now),
            )
        return {"sampled": len(sampled), "evidence_bytes": total_bytes, "already_sampled": 0}

    def expire_decision_samples(self, *, now_ms: int | None = None) -> dict[str, int]:
        now = now_ms or int(datetime.now(UTC).timestamp() * 1000)
        with self.connect() as conn:
            expired = conn.execute(
                "SELECT sample_key FROM system_information_decision_samples WHERE evidence_ref IS NOT NULL AND raw_expires_at_ms<=?",
                (now,),
            ).fetchall()
            for row in expired:
                sample_key = str(row["sample_key"])
                conn.execute("DELETE FROM system_information_evidence_pins WHERE pin_key=?", (f"decision-sample:{sample_key}",))
                conn.execute(
                    "UPDATE system_information_decision_samples SET evidence_ref=NULL,evidence_bytes=0 WHERE sample_key=?",
                    (sample_key,),
                )
            removed = conn.execute(
                "DELETE FROM system_information_decision_samples WHERE sampled_at_ms<?", (now - 30 * 86_400_000,),
            ).rowcount
        return {"raw_expired": len(expired), "index_removed": int(removed)}

    def decision_samples(self, *, sample_date: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        where = " WHERE sample_date=?" if sample_date else ""
        params: tuple[Any, ...] = (sample_date, max(1, min(100, int(limit)))) if sample_date else (max(1, min(100, int(limit))),)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM system_information_decision_samples{where} ORDER BY sampled_at_ms DESC,business_key,source,item_id LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def decision_samples_for_sources(
        self, source_items: list[tuple[str, str]], *, through_ms: int, limit: int = 5,
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        pairs = list(dict.fromkeys((str(source), str(source_id)) for source, source_id in source_items))[:12]
        if not pairs:
            return {}
        per_source_limit = max(1, min(10, limit))
        pair_sql = ",".join("(?,?)" for _ in pairs)
        params: list[Any] = [value for pair in pairs for value in pair]
        params.append(through_ms)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT sample_key,item_id,source,source_id,sampled_at_ms,sample_date,stage,status,result,"
                "reason_summary,rule_version,evidence_ref FROM system_information_decision_samples "
                f"WHERE (source,source_id) IN ({pair_sql}) AND sampled_at_ms<=? "
                "ORDER BY sampled_at_ms DESC,business_key,source,item_id",
                params,
            ).fetchall()
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {pair: [] for pair in pairs}
        for row in rows:
            pair = (str(row["source"]), str(row["source_id"]))
            values = grouped.get(pair)
            if values is None or len(values) >= per_source_limit:
                continue
            item = dict(row)
            item["raw_evidence_available"] = bool(item.pop("evidence_ref"))
            values.append(item)
        return {pair: values for pair, values in grouped.items() if values}

    def configuration_changes_for_sources(
        self, source_keys: list[str], *, through_ms: int, limit_per_source: int = 20,
    ) -> dict[str, list[dict[str, Any]]]:
        keys = list(dict.fromkeys(str(key) for key in source_keys if key))[:12]
        if not keys:
            return {}
        item_ids = [f"main:non_mainstream_media_sources:{key}" for key in keys]
        placeholders = ",".join("?" for _ in item_ids)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT i.source_id,e.event_id,e.event_type,e.status,e.happened_at_ms,e.summary,e.reason,"
                "e.evidence_ref,e.details_json FROM system_information_items i "
                "JOIN system_information_events e ON e.item_id=i.item_id "
                f"WHERE i.item_id IN ({placeholders}) AND e.event_type='source_state_change' "
                "AND e.happened_at_ms<=? ORDER BY e.happened_at_ms DESC,e.event_id",
                (*item_ids, through_ms),
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in keys}
        for row in rows:
            source_key = str(row["source_id"] or "")
            values = grouped.get(source_key)
            if values is None or len(values) >= max(1, min(20, limit_per_source)):
                continue
            item = dict(row)
            evidence_ref = item.pop("evidence_ref")
            item["details"] = json.loads(item.pop("details_json") or "{}")
            item["evidence"] = self.load_evidence(evidence_ref) if evidence_ref else None
            values.append(item)
        return {key: values for key, values in grouped.items() if values}

    def output_review_samples(self, *, now_ms: int | None = None, limit: int = 30) -> list[dict[str, Any]]:
        now = now_ms or int(datetime.now(UTC).timestamp() * 1000)
        sample_date = datetime.fromtimestamp(now / 1000, UTC).date().isoformat()
        cutoff = now - 24 * 60 * 60 * 1000
        sample_limit = max(1, min(30, int(limit)))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT item_id,business_key,source,source_id,title,summary,source_url,stage,status,result,"
                "updated_at_ms,evidence_ref FROM system_information_items "
                "WHERE business_key IN ('x','crypto_media','meme','x_agent') "
                "AND result IN ('internal_complete','direct_publish','backoffice') "
                "AND evidence_ref IS NOT NULL AND updated_at_ms>=? ORDER BY item_id",
                (cutoff,),
            ).fetchall()
        strata: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            key = (str(row["business_key"]), str(row["source"]))
            strata.setdefault(key, []).append(row)
        for key, values in strata.items():
            values.sort(key=lambda row: hashlib.sha256(f"{sample_date}:{key}:{row['item_id']}".encode()).digest())
        selected: list[sqlite3.Row] = []
        remaining = sorted(strata)
        while remaining and len(selected) < sample_limit:
            next_round = []
            for key in remaining:
                if strata[key]:
                    selected.append(strata[key].pop(0))
                if strata[key]:
                    next_round.append(key)
                if len(selected) >= sample_limit:
                    break
            remaining = next_round
        return [dict(row) for row in selected]

    def stream_item_evidence_details(self, item_ids: list[str]) -> list[dict[str, Any]]:
        if not item_ids:
            return []
        keys = sorted({str(item_id) for item_id in item_ids})[:30]
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT item_id,business_key,source,source_id,stage,status,result,evidence_ref "
                "FROM system_information_items WHERE item_id IN (" + ",".join("?" for _ in keys) + ")",
                keys,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["stream_item_id"] = item["item_id"]
            reference = item.pop("evidence_ref")
            evidence = self.load_evidence(reference) if reference else None
            if evidence is not None:
                encoded = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), default=str)
                item["evidence_excerpt"] = encoded[:8000]
                item["evidence_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                item["evidence_truncated"] = len(encoded) > 8000
            else:
                item["evidence_excerpt"] = None
                item["evidence_sha256"] = None
                item["evidence_truncated"] = False
            item["evidence_available"] = evidence is not None
            result.append(item)
        return result

    def decision_sample_details(self, sample_keys: list[str]) -> list[dict[str, Any]]:
        if not sample_keys:
            return []
        keys = sorted({str(key) for key in sample_keys})[:50]
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT sample_key,item_id,business_key,source,source_id,stage,status,result,reason_summary,rule_version,evidence_ref "
                "FROM system_information_decision_samples WHERE sample_key IN (" + ",".join("?" for _ in keys) + ") "
                "ORDER BY sample_date DESC,business_key,source,item_id",
                keys,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            reference = item.pop("evidence_ref")
            evidence = self.load_evidence(reference) if reference else None
            if evidence is not None:
                encoded = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), default=str)
                item["evidence_excerpt"] = encoded[:8000]
                item["evidence_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                item["evidence_truncated"] = len(encoded) > 8000
            else:
                item["evidence_excerpt"] = None
                item["evidence_sha256"] = None
                item["evidence_truncated"] = False
            item["evidence_available"] = evidence is not None
            result.append(item)
        return result

    def _sample_rule_version(self, evidence_ref: str) -> str | None:
        evidence = self.load_evidence(evidence_ref)
        if not isinstance(evidence, dict):
            return None
        pending: list[Any] = [evidence]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                for key in ("prompt_version", "prompt_version_id", "policy_version", "template_version"):
                    if value.get(key) not in (None, ""):
                        return str(value[key])[:160]
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        return None

    def prune_orphan_evidence(self, *, limit: int = 500, dry_run: bool = True) -> dict[str, int]:
        with self.connect() as conn:
            referenced = {
                str(row[0]) for row in conn.execute(
                    "SELECT evidence_ref FROM system_information_items WHERE evidence_ref IS NOT NULL "
                    "UNION SELECT evidence_ref FROM system_information_events WHERE evidence_ref IS NOT NULL "
                    "UNION SELECT evidence_ref FROM system_information_decision_samples WHERE evidence_ref IS NOT NULL"
                )
            }
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_review_analysis_attempts'").fetchone():
                referenced.update(str(row[0]) for row in conn.execute(
                    "SELECT raw_response_ref FROM system_review_analysis_attempts WHERE raw_response_ref IS NOT NULL"
                ))
            referenced.update(str(row[0]) for row in conn.execute(
                "SELECT evidence_ref FROM system_information_evidence_pins"
            ))
            registered = conn.execute("SELECT evidence_ref,path,byte_size FROM system_information_evidence").fetchall()
            candidates: dict[str, tuple[Path, int]] = {}
            for row in registered:
                ref = str(row["evidence_ref"])
                if ref not in referenced:
                    candidates[ref] = (Path(row["path"]), int(row["byte_size"]))
            for path in self.evidence_dir.glob("[0-9a-f][0-9a-f]/*.json.gz"):
                ref = path.name.removesuffix(".json.gz")
                if ref not in referenced and ref not in candidates:
                    try:
                        size = len(gzip.decompress(path.read_bytes()))
                    except (OSError, EOFError):
                        size = path.stat().st_size
                    candidates[ref] = (path, size)
            candidates = dict(list(candidates.items())[:max(1, min(5000, limit))])
            if not dry_run:
                conn.executemany("DELETE FROM system_information_evidence WHERE evidence_ref=?", ((ref,) for ref in candidates))
                conn.commit()
                for path, _size in candidates.values():
                    path.unlink(missing_ok=True)
        return {"files": len(candidates), "bytes": sum(size for _path, size in candidates.values()), "dry_run": int(dry_run)}

    def storage_health(self, *, sample: bool = False, now_ms: int | None = None) -> dict[str, Any]:
        now = now_ms or int(datetime.now(UTC).timestamp() * 1000)
        database_bytes = sum(
            path.stat().st_size for path in (self.database_path, Path(f"{self.database_path}-wal"), Path(f"{self.database_path}-shm"))
            if path.exists()
        )
        evidence_bytes = sum(path.stat().st_size for path in self.evidence_dir.glob("[0-9a-f][0-9a-f]/*.json.gz")) if self.evidence_dir.exists() else 0
        usage = shutil.disk_usage(self.database_path.parent)
        sample_date = datetime.fromtimestamp(now / 1000, UTC).date().isoformat()
        with self.connect() as conn:
            if sample:
                conn.execute(
                    "INSERT INTO system_information_storage_samples(sample_date,database_bytes,evidence_bytes,sampled_at_ms) VALUES(?,?,?,?) "
                    "ON CONFLICT(sample_date) DO UPDATE SET database_bytes=excluded.database_bytes,evidence_bytes=excluded.evidence_bytes,sampled_at_ms=excluded.sampled_at_ms",
                    (sample_date, database_bytes, evidence_bytes, now),
                )
            samples = conn.execute(
                "SELECT database_bytes,evidence_bytes,sampled_at_ms FROM system_information_storage_samples "
                "WHERE sampled_at_ms>=? ORDER BY sampled_at_ms", (now - 30 * 24 * 60 * 60 * 1000,),
            ).fetchall()
        daily_growth: int | None = None
        if len(samples) >= 2:
            first = samples[0]
            last = samples[-1]
            elapsed_days = max((int(last["sampled_at_ms"]) - int(first["sampled_at_ms"])) / 86_400_000, 1 / 24)
            growth = (int(last["database_bytes"]) + int(last["evidence_bytes"])) - (int(first["database_bytes"]) + int(first["evidence_bytes"]))
            daily_growth = max(0, int(growth / elapsed_days))
        threshold_bytes = max(10 * 1024**3, int(usage.total * 0.15))
        return {
            "filesystem_total_bytes": usage.total, "filesystem_free_bytes": usage.free,
            "required_free_bytes": threshold_bytes, "space_ok": usage.free >= threshold_bytes,
            "database_bytes": database_bytes, "evidence_bytes": evidence_bytes,
            "estimated_growth_bytes_per_day": daily_growth,
            "estimated_growth_next_30d_bytes": daily_growth * 30 if daily_growth is not None else None,
            "samples": len(samples), "sampled_at_ms": now,
        }

    def upsert_snapshot(self, snapshot: dict[str, Any], evidence: Any) -> bool:
        observed = int(datetime.now(UTC).timestamp() * 1000)
        with self.connect() as conn:
            return self._upsert_snapshot_in_connection(conn, snapshot, evidence, observed)

    def _upsert_snapshot_in_connection(
        self, conn: sqlite3.Connection, snapshot: dict[str, Any], evidence: Any, observed: int,
    ) -> bool:
        operation_time = snapshot.get('updated_at_ms') or snapshot.get('created_at_ms')
        material = expire_external_errors({'snapshot': snapshot, 'evidence': evidence}, operation_time)
        snapshot, evidence = material['snapshot'], material['evidence']
        messages = error_messages(evidence)
        if messages and operation_time is not None and operation_time >= day_start_ms():
            snapshot = {**snapshot, 'failure_category': failure_category(messages[0])}
        fingerprint = _fingerprint({key: value for key, value in snapshot.items() if key != 'updated_at_ms'})
        item_id = str(snapshot["item_id"])
        previous = conn.execute(
            "SELECT fingerprint,title,summary,source_url,stage,status,result FROM system_information_items WHERE item_id=?", (item_id,),
        ).fetchone()
        if previous is not None and previous["fingerprint"] == fingerprint:
            conn.execute('UPDATE system_information_items SET updated_at_ms=MAX(COALESCE(updated_at_ms,0),?),observed_at_ms=? WHERE item_id=? AND COALESCE(updated_at_ms,0)<?',
                         (snapshot.get('updated_at_ms') or 0, observed, item_id, snapshot.get('updated_at_ms') or 0))
            if snapshot.get('parent_item_id'):
                conn.execute('UPDATE system_information_items SET updated_at_ms=MAX(COALESCE(updated_at_ms,0),?) WHERE item_id=?',
                             (snapshot.get('updated_at_ms') or 0, snapshot['parent_item_id']))
            return False
        evidence_ref = None
        if evidence is not None:
            evidence_ref, evidence_path, evidence_size = self._write_evidence_file(evidence)
            self._register_evidence(conn, evidence_ref, evidence_path, evidence_size)
        conn.execute(
            """INSERT INTO system_information_items(
                item_id,business_key,source,source_id,task_id,title,summary,source_url,stage,status,result,
                failure_category,created_at_ms,updated_at_ms,observed_at_ms,source_database,source_table,
                source_row_id,evidence_ref,fingerprint,legacy_history
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET
                business_key=excluded.business_key,source=excluded.source,source_id=excluded.source_id,
                task_id=excluded.task_id,title=excluded.title,summary=excluded.summary,source_url=excluded.source_url,
                stage=excluded.stage,status=excluded.status,result=excluded.result,
                failure_category=excluded.failure_category,created_at_ms=excluded.created_at_ms,
                updated_at_ms=CASE WHEN updated_at_ms IS NULL THEN excluded.updated_at_ms WHEN excluded.updated_at_ms IS NULL THEN updated_at_ms ELSE MAX(updated_at_ms,excluded.updated_at_ms) END,observed_at_ms=excluded.observed_at_ms,
                source_database=excluded.source_database,source_table=excluded.source_table,
                source_row_id=excluded.source_row_id,evidence_ref=excluded.evidence_ref,
                fingerprint=excluded.fingerprint""",
            (
                item_id, snapshot["business_key"], snapshot["source"], snapshot.get("source_id"),
                snapshot.get("task_id"), snapshot.get("title") or "", snapshot.get("summary") or "",
                snapshot.get("source_url"), snapshot.get("stage") or "unknown", snapshot.get("status") or "unknown",
                snapshot.get("result") or "unknown", snapshot.get("failure_category"), snapshot.get("created_at_ms"),
                snapshot.get("updated_at_ms"), observed, snapshot["source_database"], snapshot["source_table"],
                str(snapshot["source_row_id"]), evidence_ref, fingerprint, int(snapshot.get("legacy_history", True)),
            ),
        )
        conn.execute("DELETE FROM system_information_search WHERE item_id=?", (item_id,))
        conn.execute('UPDATE system_information_items SET news_visible=?,admission=?,parent_item_id=?,duplicate_json=?,source_name=?,source_updated_at_ms=? WHERE item_id=?',
                     (int(snapshot.get('news_visible', True)), snapshot.get('admission', 'unassessed'),
                      snapshot.get('parent_item_id'), json.dumps(snapshot.get('duplicate_target'), ensure_ascii=False), snapshot.get('source_name') or '', snapshot.get('source_updated_at_ms'), item_id))
        if snapshot.get('parent_item_id'):
            conn.execute('UPDATE system_information_items SET updated_at_ms=MAX(COALESCE(updated_at_ms,0),?) WHERE item_id=?',
                         (snapshot.get('updated_at_ms') or 0, snapshot['parent_item_id']))
        searchable = _searchable_text(
            tuple(snapshot.get(key) for key in ("title", "summary", "source", "source_id", "source_url")),
            _safe_json(evidence) if evidence is not None else None,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO system_information_search(term,item_id) VALUES (?,?)",
            ((term, item_id) for term in _search_terms(searchable)),
        )
        event_id = hashlib.sha256(f"{item_id}:{fingerprint}".encode()).hexdigest()
        state_changed = previous is not None and any(
            previous[key] != (snapshot.get(key) or ("" if key in {"title", "summary", "source_url"} else None))
            for key in ("stage", "status", "result", "title", "summary", "source_url")
        )
        conn.execute(
            """INSERT OR IGNORE INTO system_information_events(
                event_id,item_id,event_type,stage,status,result,happened_at_ms,observed_at_ms,
                summary,reason,error_code,evidence_ref,details_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id, item_id, "legacy_snapshot" if previous is None else "observed_change" if state_changed else "artifact_update",
                snapshot.get("stage") or "unknown", snapshot.get("status") or "unknown",
                snapshot.get("result") or "unknown", snapshot.get("updated_at_ms") or snapshot.get("created_at_ms"),
                observed, snapshot.get("summary") or "", snapshot.get("reason"), snapshot.get("error_code"),
                evidence_ref, json.dumps({
                    "history_complete": previous is not None and not bool(snapshot.get("legacy_history")),
                    "source_row_id": str(snapshot["source_row_id"]),
                    "history_note": "历史初始状态不包含早期尝试；状态变化由常驻投影器轮询观察，轮询间隔内的中间尝试可能缺失。" if snapshot.get("legacy_history") else "状态变化由常驻投影器轮询观察；轮询间隔内的中间尝试可能缺失。",
                }, ensure_ascii=False),
            )
        )
        return True

    def append_event(
        self,
        *,
        item_id: str,
        event_id: str,
        event_type: str,
        stage: str,
        status: str,
        result: str,
        happened_at_ms: int | None,
        summary: str,
        reason: str | None = None,
        error_code: str | None = None,
        evidence: Any = None,
        evidence_ref: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        observed = int(datetime.now(UTC).timestamp() * 1000)
        with self.connect() as conn:
            return self._append_event_in_connection(
                conn, observed=observed, item_id=item_id, event_id=event_id, event_type=event_type,
                stage=stage, status=status, result=result, happened_at_ms=happened_at_ms,
                summary=summary, reason=reason, error_code=error_code, evidence=evidence,
                evidence_ref=evidence_ref, details=details,
            )

    def _append_event_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        observed: int,
        item_id: str,
        event_id: str,
        event_type: str,
        stage: str,
        status: str,
        result: str,
        happened_at_ms: int | None,
        summary: str,
        reason: str | None = None,
        error_code: str | None = None,
        evidence: Any = None,
        evidence_ref: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        expired_external = happened_at_ms is not None and happened_at_ms < day_start_ms() and external_error(
            {'reason': reason, 'error_code': error_code, 'status': status, 'summary': summary, 'evidence': evidence})
        material = expire_external_errors({'reason': reason, 'error_code': error_code, 'summary': summary,
                                          'status': status, 'evidence': evidence, 'details': details}, happened_at_ms)
        reason, error_code, summary = material['reason'], material['error_code'], material['summary']
        evidence, details = material['evidence'], material['details']
        if expired_external:
            evidence_ref = None
        if conn.execute("SELECT 1 FROM system_information_items WHERE item_id=?", (item_id,)).fetchone() is None:
            return False
        if evidence is not None:
            evidence_ref, path, size = self._write_evidence_file(evidence)
            self._register_evidence(conn, evidence_ref, path, size)
        elif evidence_ref and conn.execute(
            "SELECT 1 FROM system_information_evidence WHERE evidence_ref=?", (evidence_ref,),
        ).fetchone() is None:
            evidence_ref = None
        cursor = conn.execute(
            """INSERT OR IGNORE INTO system_information_events(
                event_id,item_id,event_type,stage,status,result,happened_at_ms,observed_at_ms,
                summary,reason,error_code,evidence_ref,details_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, item_id, event_type, stage, status, result, happened_at_ms, observed,
             summary[:1200], reason[:1200] if reason else None, error_code, evidence_ref,
             json.dumps(details or {}, ensure_ascii=False, separators=(",", ":"))),
        )
        if cursor.rowcount and event_type == 'stage_attempt' and happened_at_ms is not None:
            conn.execute('UPDATE system_information_items SET updated_at_ms=MAX(COALESCE(updated_at_ms,0),?) WHERE item_id=?', (happened_at_ms, item_id))
        return cursor.rowcount > 0

    def apply_source_audit_batch(
        self, entries: list[dict[str, Any]], *, watermark_key: str, watermark: int,
    ) -> tuple[int, int]:
        """Commit a source-event batch and its resume cursor as one destination transaction."""
        observed = int(datetime.now(UTC).timestamp() * 1000)
        changed = 0
        with self.connect() as conn:
            snapshots: dict[str, dict[str, Any]] = {}
            for entry in entries:
                snapshot = entry["snapshot"]
                item_id = str(snapshot["item_id"])
                if entry.get("only_if_newer"):
                    previous = conn.execute(
                        "SELECT updated_at_ms FROM system_information_items WHERE item_id=?", (item_id,),
                    ).fetchone()
                    incoming_time = snapshot.get("updated_at_ms") or 0
                    if previous is not None and incoming_time < (previous["updated_at_ms"] or 0):
                        continue
                snapshots[item_id] = entry
            for entry in snapshots.values():
                snapshot = entry["snapshot"]
                item_id = str(snapshot["item_id"])
                if entry.get("only_if_newer"):
                    previous = conn.execute(
                        "SELECT 1 FROM system_information_items WHERE item_id=?", (item_id,),
                    ).fetchone()
                    if previous is not None:
                        admission = str(snapshot.get("admission") or "unassessed")
                        visible_sql = (
                            "CASE WHEN ? IN ('accepted','duplicate') THEN 1 "
                            "WHEN ?='rejected' THEN 0 ELSE news_visible END"
                        )
                        admission_sql = (
                            "CASE WHEN ? IN ('accepted','duplicate','rejected') THEN ? ELSE admission END"
                        )
                        cursor = conn.execute(
                            "UPDATE system_information_items SET "
                            "business_key=CASE WHEN business_key='unmapped_source' THEN ? ELSE business_key END, "
                            "task_id=COALESCE(task_id,?), "
                            "title=CASE WHEN title='' THEN ? ELSE title END, "
                            "source_url=COALESCE(source_url,?), stage=?,status=?,result=?,summary=?,failure_category=?, "
                            "updated_at_ms=?,observed_at_ms=?, "
                            f"news_visible={visible_sql},admission={admission_sql} WHERE item_id=?",
                            (
                                snapshot.get("business_key") or "unmapped_source", snapshot.get("task_id"),
                                snapshot.get("title") or "", snapshot.get("source_url"),
                                snapshot.get("stage") or "unknown", snapshot.get("status") or "unknown",
                                snapshot.get("result") or "unknown", snapshot.get("summary") or "",
                                snapshot.get("failure_category"), snapshot.get("updated_at_ms"), observed,
                                admission, admission, admission, admission, item_id,
                            ),
                        )
                        changed += int(cursor.rowcount > 0)
                        continue
                changed += int(self._upsert_snapshot_in_connection(
                    conn, snapshot, entry.get("evidence"), observed,
                ))
            for entry in entries:
                snapshot = entry["snapshot"]
                item_id = str(snapshot["item_id"])
                self._append_event_in_connection(
                    conn,
                    observed=observed,
                    item_id=item_id,
                    event_id=str(entry["event_id"]),
                    event_type="source_state_change",
                    stage=str(entry["stage"]),
                    status=str(entry["status"]),
                    result=str(entry["result"]),
                    happened_at_ms=int(entry["happened_at_ms"]),
                    summary=str(entry["summary"]),
                    reason=entry.get("reason"),
                    error_code=entry.get("error_code"),
                    evidence=entry.get("evidence"),
                    details=entry.get("details"),
                )
            state_row = conn.execute(
                "SELECT state_value FROM system_information_state WHERE state_key=?", (watermark_key,),
            ).fetchone()
            encoded_watermark = str(watermark)
            if state_row is None or str(state_row["state_value"]) != encoded_watermark:
                conn.execute(
                    "INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES(?,?,?) "
                    "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms",
                    (watermark_key, encoded_watermark, observed),
                )
        return len(entries), changed

    def set_state(self, key: str, value: Any) -> None:
        encoded = str(value)
        if self.get_state(key) == encoded:
            return
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES (?,?,?) "
                "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms",
                (key, encoded, int(datetime.now(UTC).timestamp() * 1000)),
            )

    def record_external_error_totals(self, database_key: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with self.connect() as conn:
            conn.executemany('INSERT INTO system_information_external_error_daily VALUES(?,?,?,?,?,?) ON CONFLICT(database_key,error_date,source,stage,category) DO UPDATE SET occurrences=excluded.occurrences',
                             ((database_key, row['error_date'], row['source'], row['stage'], row['category'], row['occurrences']) for row in rows))

    def external_error_totals(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute('SELECT error_date,source,stage,category,SUM(occurrences) AS occurrences FROM system_information_external_error_daily GROUP BY error_date,source,stage,category ORDER BY error_date DESC,source,stage,category LIMIT 500')]

    def get_state(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT state_value FROM system_information_state WHERE state_key=?", (key,)).fetchone()
        return str(row["state_value"]) if row else default

    def has_item(self, item_id: str) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM system_information_items WHERE item_id=?", (item_id,),
            ).fetchone() is not None

    def rebuild_search_index(self, limit: int = 100) -> int:
        if self.get_state("search_bigrams_rebuild_done") == "1":
            return 0

        now = int(datetime.now(UTC).timestamp() * 1000)
        with self.connect() as conn:
            started = self.get_state("search_bigrams_rebuild_started") == "1"
            if not started:
                conn.execute("DELETE FROM system_information_search")
                conn.execute("DROP TABLE IF EXISTS system_information_search_positions")
                for key, value in (
                    ("search_bigrams_rebuild_started", "1"),
                    ("search_bigrams_rebuild_cursor", ""),
                    ("search_bigrams_rebuild_done", "0"),
                    ("search_positions_rebuild_done", "0"),
                    ("search_positions_rebuild_cursor", ""),
                ):
                    conn.execute(
                        "INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES(?,?,?) "
                        "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms",
                        (key, value, now),
                    )
                conn.commit()
            cursor = self.get_state("search_bigrams_rebuild_cursor")
            batch_size = max(1, min(limit, 100))
            rows = conn.execute(
                "SELECT i.item_id,i.title,i.summary,i.source,i.source_id,i.source_url,i.evidence_ref,e.path AS evidence_path "
                "FROM system_information_items i LEFT JOIN system_information_evidence e ON e.evidence_ref=i.evidence_ref "
                "WHERE i.item_id>? ORDER BY i.item_id LIMIT ?",
                (cursor, batch_size),
            ).fetchall()
            if not rows:
                conn.execute("DROP TABLE IF EXISTS system_information_search_positions")
                conn.execute(
                    "INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES(?,?,?) "
                    "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms",
                    ("search_bigrams_rebuild_done", "1", now),
                )
                return 0

        for offset in range(0, len(rows), 10):
            indexed: list[tuple[str, str]] = []
            group = rows[offset:offset + 10]
            for row in group:
                evidence = None
                evidence_path = row["evidence_path"]
                if evidence_path:
                    try:
                        evidence = json.loads(gzip.decompress(Path(evidence_path).read_bytes()))
                    except (OSError, EOFError, json.JSONDecodeError):
                        evidence = None
                searchable = _searchable_text(
                    tuple(row[key] for key in ("title", "summary", "source", "source_id", "source_url")),
                    evidence,
                )
                indexed.extend((term, str(row["item_id"])) for term in _search_terms(searchable))
            with self.connect() as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO system_information_search(term,item_id) VALUES (?,?)",
                    indexed,
                )
                next_cursor = str(group[-1]["item_id"])
                conn.execute(
                    "INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES(?,?,?) "
                    "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms",
                    ("search_bigrams_rebuild_cursor", next_cursor, now),
                )
        return len(rows)

    def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(payload.get("limit") or 50), 100))
        sort = "created_at_ms" if payload.get("sort") == "created" else "updated_at_ms"
        clauses: list[str] = []
        params: list[Any] = []
        if payload.get('view') == 'news':
            clauses.extend(['i.news_visible=1', 'i.parent_item_id IS NULL'])
        for key, column in (("business", "business_key"), ("source", "source"), ("stage", "stage"), ("status", "status"), ("result", "result"), ("failure_category", "failure_category")):
            value = str(payload.get(key) or "").strip()
            if value and value != "all":
                if key == "status" and value in {"failed", "processing", "backoffice", "direct_publish", "normal_stop"}:
                    groups = {
                        "failed": "i.status LIKE '%failed' OR i.status IN ('failed','error','exhausted')",
                        "processing": "i.status IN ('pending','judging','judged','deduping','deduped','searched','writing','written','formatting','formatted','publisher_pending','publishing','queued','processing','running','retry')",
                        "backoffice": "i.status IN ('ready_review','pending_review','backoffice')",
                        "direct_publish": "i.status IN ('auto_published','published','sent','succeeded')",
                        "normal_stop": "i.status IN ('discarded','duplicate','ignored','skipped','ended')",
                    }
                    clauses.append(f"({groups[value]})")
                else:
                    clauses.append(f"i.{column}=?")
                    params.append(value)
        since = timestamp_ms(payload.get("since"))
        until = timestamp_ms(payload.get("until"))
        if since is not None:
            clauses.append(f"i.{sort}>=?")
            params.append(since)
        if until is not None:
            clauses.append(f"i.{sort}<?")
            params.append(until)
        keyword = str(payload.get("q") or "").strip().casefold()
        if keyword:
            if len(keyword) < 2:
                raise ValueError("关键词至少需要两个字符")
            ordered_terms = sorted(_search_terms(keyword))
            if not ordered_terms:
                raise ValueError("关键词至少需要两个字符")
            if len(ordered_terms) > 400:
                raise ValueError("关键词最多支持 400 个字符")
            clauses.append(
                "i.item_id IN (SELECT item_id FROM system_information_search WHERE term IN ("
                + ",".join("?" for _ in ordered_terms)
                + ") GROUP BY item_id HAVING COUNT(*)=?)"
            )
            params.extend(ordered_terms)
            params.append(len(ordered_terms))
        base_where = " WHERE " + " AND ".join(clauses) if clauses else ""
        base_params = list(params)
        cursor = payload.get("cursor")
        if isinstance(cursor, dict):
            cursor_id = str(cursor.get("id") or "")
            cursor_time = cursor.get("time")
            if cursor_time is None:
                clauses.append(f"(i.{sort} IS NULL AND i.item_id<?)")
                params.append(cursor_id)
            else:
                cursor_time = int(cursor_time)
                clauses.append(f"(i.{sort}<? OR (i.{sort}=? AND i.item_id<?) OR i.{sort} IS NULL)")
                params.extend([cursor_time, cursor_time, cursor_id])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        # A new, mostly empty news index has no statistics yet. SQLite otherwise
        # chooses parent_item_id=NULL and scans every technical row before sorting.
        news_index = " INDEXED BY idx_system_information_news_activity" if payload.get('view') == 'news' else ""
        with self.connect() as conn:
            if keyword:
                rows = conn.execute(
                    f"""SELECT i.item_id,i.business_key,i.source,i.source_id,i.task_id,i.title,i.summary,i.source_url,
                        i.stage,i.status,i.result,i.failure_category,i.created_at_ms,i.updated_at_ms,i.observed_at_ms,
                        i.evidence_ref,i.legacy_history,i.admission,i.duplicate_json,i.source_name,i.source_updated_at_ms,e.path AS evidence_path
                        FROM system_information_items i LEFT JOIN system_information_evidence e ON e.evidence_ref=i.evidence_ref
                        {base_where}
                        ORDER BY i.{sort} DESC,i.item_id DESC""",
                    base_params,
                ).fetchall()
                total = 0
                items = []
                cursor_id = str(cursor.get("id") or "") if isinstance(cursor, dict) else ""
                for row in rows:
                    evidence = None
                    if row["evidence_path"]:
                        try:
                            evidence = json.loads(gzip.decompress(Path(row["evidence_path"]).read_bytes()))
                        except (OSError, EOFError, json.JSONDecodeError):
                            evidence = None
                    visible = expire_external_errors({'item': dict(row), 'evidence': evidence}, row['updated_at_ms'])
                    searchable = _searchable_text(
                        tuple(visible['item'][key] for key in ("title", "summary", "source", "source_id", "source_url")),
                        visible['evidence'],
                    )
                    if keyword not in searchable:
                        continue
                    total += 1
                    if isinstance(cursor, dict):
                        cursor_time = cursor.get("time")
                        row_time = row[sort]
                        if cursor_time is None:
                            if row_time is not None or str(row["item_id"]) >= cursor_id:
                                continue
                        elif row_time is not None:
                            cursor_time = int(cursor_time)
                            row_time = int(row_time)
                            if not (row_time < cursor_time or (row_time == cursor_time and str(row["item_id"]) < cursor_id)):
                                continue
                    if len(items) < limit + 1:
                        item = dict(row)
                        item.pop("evidence_path", None)
                        items.append(item)
            else:
                total = conn.execute(f"SELECT COUNT(*) FROM system_information_items i{news_index}{base_where}", base_params).fetchone()[0]
                rows = conn.execute(
                    f"""SELECT i.item_id,i.business_key,i.source,i.source_id,i.task_id,i.title,i.summary,i.source_url,
                        i.stage,i.status,i.result,i.failure_category,i.created_at_ms,i.updated_at_ms,i.observed_at_ms,
                        i.evidence_ref,i.legacy_history,i.admission,i.duplicate_json,i.source_name,i.source_updated_at_ms
                        FROM system_information_items i{news_index}{where}
                        ORDER BY i.{sort} DESC,i.item_id DESC LIMIT ?""",
                    [*params, limit + 1],
                ).fetchall()
                items = [dict(row) for row in rows]
            more = len(items) > limit
            items = items[:limit]
            for item in items:
                item['duplicate_target'] = json.loads(item.pop('duplicate_json') or 'null')
                cleaned = expire_external_errors(item, item.get('updated_at_ms'))
                if cleaned is not item:
                    item.clear()
                    item.update(cleaned)
            next_cursor = None
            if more and items:
                last = items[-1]
                next_cursor = {"time": last.get(sort), "id": last["item_id"]}
            lag = conn.execute("SELECT state_value FROM system_information_state WHERE state_key='main_tasks_lag' ").fetchone()
            sync = conn.execute("SELECT state_value FROM system_information_state WHERE state_key='main_tasks_last_success' ").fetchone()
            search_ready = conn.execute("SELECT state_value FROM system_information_state WHERE state_key='search_bigrams_rebuild_done'").fetchone()
        return {
            "items": items, "total": int(total), "limit": limit, "next_cursor": next_cursor,
            "index": {"generated_at_ms": int(sync["state_value"]) if sync else None, "lag": int(lag["state_value"]) if lag else None},
            "search_scope": "已登记的标题、正文摘要、来源、ID与URL；已清理原文不会被补搜。",
            "search_index_ready": bool(search_ready and search_ready["state_value"] == "1"),
        }

    def detail(self, item_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            item = conn.execute("SELECT * FROM system_information_items WHERE item_id=?", (item_id,)).fetchone()
            if item is None:
                return None
            events = conn.execute(
                "SELECT event_id,event_type,stage,status,result,happened_at_ms,observed_at_ms,summary,reason,error_code,evidence_ref,details_json FROM system_information_events WHERE item_id=? OR item_id IN (SELECT item_id FROM system_information_items WHERE parent_item_id=?) ORDER BY COALESCE(happened_at_ms,observed_at_ms),observed_at_ms,rowid",
                (item_id, item_id),
            ).fetchall()
            references = list({row['evidence_ref'] for row in [item, *events] if row['evidence_ref']})
            paths: dict[str, str] = {}
            for offset in range(0, len(references), 500):
                batch = references[offset:offset + 500]
                placeholders = ','.join('?' for _ in batch)
                paths.update({row['evidence_ref']: row['path'] for row in conn.execute(
                    f'SELECT evidence_ref,path FROM system_information_evidence WHERE evidence_ref IN ({placeholders})',
                    batch,
                )})
        # Keep evidence caching within this request so retention changes remain visible.
        evidence = {reference: self._read_evidence_file(path, reference) for reference, path in paths.items()}
        result = dict(item)
        result['duplicate_target'] = json.loads(result.pop('duplicate_json') or 'null')
        result["evidence"] = evidence.get(result.get("evidence_ref"))
        result["timeline"] = [dict(row) for row in events]
        for event in result["timeline"]:
            event["evidence"] = evidence.get(event.get("evidence_ref"))
            try:
                event["details"] = json.loads(event.pop("details_json"))
            except json.JSONDecodeError:
                event["details"] = {}
        expired_attempts = {
            event['details'].get('attempt_id') for event in result['timeline']
            if event.get('happened_at_ms') is not None and event['happened_at_ms'] < day_start_ms()
            and external_error(event) and event['details'].get('attempt_id') is not None
        }
        result['timeline'] = [
            event for event in result['timeline']
            if not (event.get('happened_at_ms') is not None and event['happened_at_ms'] < day_start_ms()
                    and (external_error(event) or event['details'].get('attempt_id') in expired_attempts))
        ]
        result = expire_external_errors(result, result.get('updated_at_ms'))
        return result

    def coverage(self) -> dict[str, Any]:
        with self.connect() as conn:
            counts = conn.execute(
                "SELECT business_key,source,COUNT(*) AS items FROM system_information_items GROUP BY business_key,source ORDER BY business_key,source"
            ).fetchall()
            states = {
                row["state_key"]: row["state_value"]
                for row in conn.execute("SELECT state_key,state_value FROM system_information_state")
            }
        return {
            "sources": [dict(row) for row in counts],
            "adapters": [
                {
                    "name": key.removeprefix("adapter_").removesuffix("_status"), "status": value,
                    "audit_tables": int(states.get(f"adapter_{key.removeprefix('adapter_').removesuffix('_status')}_audit_tables", "0") or 0),
                    "audit_skipped": int(states.get(f"adapter_{key.removeprefix('adapter_').removesuffix('_status')}_audit_skipped", "0") or 0),
                    "audit_watermark": int(states.get(f"source_audit_{key.removeprefix('adapter_').removesuffix('_status')}_watermark", "0") or 0),
                }
                for key, value in states.items() if key.startswith("adapter_") and key.endswith("_status")
            ],
            "projection": {
                "watermark": int(states.get("main_tasks_watermark", "0") or 0),
                "lag": int(states.get("main_tasks_lag", "0") or 0),
                "last_success_ms": int(states["main_tasks_last_success"]) if states.get("main_tasks_last_success") else None,
                "last_error": states.get("main_tasks_last_error") or None,
            },
        }
