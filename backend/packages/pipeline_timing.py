from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any

from packages.common.postgres import build_psycopg_connect_kwargs, load_database_url


PIPELINE_TIMING_WINDOWS = (24, 72, 168)
PIPELINE_TIMING_SOURCES = ("x", "blockbeats", "panews", "jinse", "non_mainstream_media", "ai_source", "jin10")
PIPELINE_TIMING_RETENTION_DAYS = 14

STAGE_LABELS: dict[str, str] = {
    "judge": "判断",
    "search": "查重",
    "write": "写作",
    "format": "定稿",
    "publisher_decision": "发布者判定",
    "publish_finalize": "发布收尾",
    "total": "总耗时",
}

FLOW_LABELS: dict[str, str] = {
    "x_regular": "X常规",
    "x_ai_source_account": "X-AI信源账号",
    "competitor": "竞品",
    "non_mainstream_media": "Crypto信源全文",
    "ai_source": "AI信源全文",
    "jin10": "金十",
}

FLOW_ORDER = ("x_regular", "x_ai_source_account", "competitor", "non_mainstream_media", "ai_source", "jin10")


PIPELINE_TIMING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pipeline_timing_snapshots (
    id bigserial PRIMARY KEY,
    window_hours integer NOT NULL,
    generated_at timestamptz NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (window_hours, generated_at)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_timing_snapshots_generated
ON pipeline_timing_snapshots(generated_at DESC, window_hours);

ALTER TABLE pipeline_timing_snapshots ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL PRIVILEGES ON TABLE pipeline_timing_snapshots FROM anon;
        REVOKE ALL PRIVILEGES ON SEQUENCE pipeline_timing_snapshots_id_seq FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL PRIVILEGES ON TABLE pipeline_timing_snapshots FROM authenticated;
        REVOKE ALL PRIVILEGES ON SEQUENCE pipeline_timing_snapshots_id_seq FROM authenticated;
    END IF;
END $$;
"""


@dataclass(frozen=True)
class PipelineTimingRow:
    task_id: int
    source: str
    status: str
    created_at: datetime
    metadata: dict[str, Any]
    news_type: str | None
    publisher_decision: str | None
    judge_completed_at: datetime | None
    search_completed_at: datetime | None
    write_completed_at: datetime | None
    format_completed_at: datetime | None
    publisher_decided_at: datetime | None
    publish_completed_at: datetime | None


def _import_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
    except Exception as exc:  # pragma: no cover - dependency guard.
        raise RuntimeError("psycopg is required for pipeline timing snapshots") from exc
    return psycopg, dict_row, Jsonb


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _seconds_between(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    seconds = (end - start).total_seconds()
    if seconds < 0:
        return None
    return round(seconds, 3)


def _metric(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "mean_seconds": None,
            "median_seconds": None,
        }
    return {
        "count": len(values),
        "mean_seconds": round(mean(values), 3),
        "median_seconds": round(median(values), 3),
    }


def _summary(*, sample_count: int, total_values: list[float]) -> dict[str, Any]:
    completed_count = len(total_values)
    return {
        "sample_count": sample_count,
        "completed_count": completed_count,
        "completion_rate": round(completed_count / sample_count, 4) if sample_count else 0.0,
        **_metric(total_values),
    }


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def pipeline_flow_key(row: PipelineTimingRow) -> str:
    if row.source == "x":
        return "x_ai_source_account" if _truthy(row.metadata.get("x_account_is_ai_source")) else "x_regular"
    if row.source in {"blockbeats", "panews", "jinse"}:
        return "competitor"
    return row.source


def pipeline_stage_durations(row: PipelineTimingRow) -> dict[str, float | None]:
    if row.source in {"x", "jin10"}:
        judge = _seconds_between(row.created_at, row.judge_completed_at)
        search = _seconds_between(row.judge_completed_at, row.search_completed_at)
        write = _seconds_between(row.search_completed_at, row.write_completed_at)
    else:
        search = _seconds_between(row.created_at, row.search_completed_at)
        judge = _seconds_between(row.search_completed_at, row.judge_completed_at)
        write = _seconds_between(row.judge_completed_at, row.write_completed_at)

    return {
        "judge": judge,
        "search": search,
        "write": write,
        "format": _seconds_between(row.write_completed_at, row.format_completed_at),
        "publisher_decision": _seconds_between(row.format_completed_at, row.publisher_decided_at),
        "publish_finalize": _seconds_between(row.publisher_decided_at, row.publish_completed_at),
        "total": _seconds_between(row.created_at, row.publish_completed_at),
    }


def build_pipeline_timing_dashboard(
    rows: list[PipelineTimingRow],
    *,
    generated_at: datetime | None = None,
    windows: tuple[int, ...] = PIPELINE_TIMING_WINDOWS,
) -> dict[str, Any]:
    generated = generated_at or datetime.now(UTC)
    normalized_rows = sorted(rows, key=lambda row: row.created_at, reverse=True)
    return {
        "generated_at": _iso(generated),
        "windows": [
            _build_window_payload(normalized_rows, generated_at=generated, hours=hours)
            for hours in windows
        ],
    }


def _build_window_payload(rows: list[PipelineTimingRow], *, generated_at: datetime, hours: int) -> dict[str, Any]:
    cutoff = generated_at - timedelta(hours=hours)
    window_rows = [row for row in rows if row.created_at >= cutoff]
    stage_values = _collect_stage_values(window_rows)
    total_values = stage_values["total"]
    flow_payloads: list[dict[str, Any]] = []
    for flow_key in FLOW_ORDER:
        flow_rows = [row for row in window_rows if pipeline_flow_key(row) == flow_key]
        if not flow_rows:
            continue
        flow_stage_values = _collect_stage_values(flow_rows)
        flow_payloads.append(
            {
                "flow_key": flow_key,
                "flow_name": FLOW_LABELS.get(flow_key, flow_key),
                **_summary(sample_count=len(flow_rows), total_values=flow_stage_values["total"]),
                "by_stage": _stage_payloads(flow_stage_values),
            }
        )
    return {
        "hours": hours,
        "label": "7d" if hours == 168 else f"{hours}h",
        "overall": _summary(sample_count=len(window_rows), total_values=total_values),
        "by_stage": _stage_payloads(stage_values),
        "by_flow": flow_payloads,
        "status_breakdown": _status_breakdown(window_rows),
    }


def _collect_stage_values(rows: list[PipelineTimingRow]) -> dict[str, list[float]]:
    values: dict[str, list[float]] = {stage: [] for stage in STAGE_LABELS}
    for row in rows:
        durations = pipeline_stage_durations(row)
        for stage, seconds in durations.items():
            if seconds is not None:
                values[stage].append(seconds)
    return values


def _stage_payloads(values: dict[str, list[float]]) -> list[dict[str, Any]]:
    return [
        {
            "stage_key": stage,
            "stage_name": label,
            **_metric(values.get(stage, [])),
        }
        for stage, label in STAGE_LABELS.items()
    ]


def _status_breakdown(rows: list[PipelineTimingRow]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return [{"status": status, "count": count} for status, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


class PipelineTimingLocalStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_timing_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pipeline_timing_snapshots_generated
                ON pipeline_timing_snapshots(generated_at DESC)
                """
            )
            conn.commit()

    def latest(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json
                FROM pipeline_timing_snapshots
                ORDER BY generated_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return json.loads(str(row["payload_json"]))

    def latest_generated_at(self) -> datetime | None:
        latest = self.latest()
        if not latest:
            return None
        return parse_iso_datetime(latest.get("generated_at"))

    def save(self, payload: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pipeline_timing_snapshots (generated_at, payload_json, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    str(payload["generated_at"]),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    _iso(now),
                ),
            )
            conn.commit()

    def prune_old(self, *, retention_days: int = PIPELINE_TIMING_RETENTION_DAYS) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        with self._connect() as conn:
            conn.execute("DELETE FROM pipeline_timing_snapshots WHERE generated_at < ?", (_iso(cutoff),))
            conn.commit()


class PostgresPipelineTimingRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or load_database_url()
        self._psycopg, self._dict_row, self._Jsonb = _import_psycopg()
        self.application_name = "odaily-pipeline-timing"

    def _connect(self, *, autocommit: bool = False):
        return self._psycopg.connect(
            self.database_url,
            **build_psycopg_connect_kwargs(
                row_factory=self._dict_row,
                autocommit=autocommit,
                application_name=self.application_name,
            ),
        )

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(PIPELINE_TIMING_SCHEMA_SQL)
            conn.commit()

    def list_recent_rows(self, *, max_hours: int = max(PIPELINE_TIMING_WINDOWS)) -> list[PipelineTimingRow]:
        with self._connect(autocommit=True) as conn:
            rows = conn.execute(
                """
                SELECT
                    t.id AS task_id,
                    t.source,
                    t.status,
                    t.created_at,
                    t.metadata,
                    p.news_type,
                    p.publisher_decision,
                    p.judge_completed_at,
                    p.search_completed_at,
                    p.write_completed_at,
                    p.format_completed_at,
                    p.publisher_decided_at,
                    p.publish_completed_at
                FROM tasks t
                LEFT JOIN x_task_pipeline p ON p.task_id = t.id
                WHERE t.created_at >= now() - (%(max_hours)s || ' hours')::interval
                  AND t.source = ANY(%(sources)s)
                ORDER BY t.created_at DESC, t.id DESC
                """,
                {"max_hours": max_hours, "sources": list(PIPELINE_TIMING_SOURCES)},
            ).fetchall()
        return [
            PipelineTimingRow(
                task_id=int(row["task_id"]),
                source=str(row["source"]),
                status=str(row["status"]),
                created_at=row["created_at"],
                metadata=row.get("metadata") or {},
                news_type=row.get("news_type"),
                publisher_decision=row.get("publisher_decision"),
                judge_completed_at=row.get("judge_completed_at"),
                search_completed_at=row.get("search_completed_at"),
                write_completed_at=row.get("write_completed_at"),
                format_completed_at=row.get("format_completed_at"),
                publisher_decided_at=row.get("publisher_decided_at"),
                publish_completed_at=row.get("publish_completed_at"),
            )
            for row in rows
        ]

    def archive_dashboard(self, payload: dict[str, Any]) -> None:
        generated_at = parse_iso_datetime(payload.get("generated_at")) or datetime.now(UTC)
        with self._connect() as conn:
            for window in payload.get("windows") or []:
                window_hours = int(window.get("hours") or 0)
                if window_hours <= 0:
                    continue
                conn.execute(
                    """
                    INSERT INTO pipeline_timing_snapshots (window_hours, generated_at, payload)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (window_hours, generated_at) DO UPDATE
                    SET payload = EXCLUDED.payload
                    """,
                    (window_hours, generated_at, self._Jsonb(window)),
                )
            conn.commit()


class PipelineTimingSnapshotService:
    def __init__(
        self,
        *,
        local_store: PipelineTimingLocalStore,
        repository: PostgresPipelineTimingRepository,
        refresh_interval_seconds: float = 3600.0,
    ) -> None:
        self.local_store = local_store
        self.repository = repository
        self.refresh_interval_seconds = max(60.0, float(refresh_interval_seconds))
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="pipeline-timing-snapshotter", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def dashboard(self) -> dict[str, Any]:
        latest = self.local_store.latest()
        if latest is None:
            return {"generated_at": None, "windows": [], "last_error": self._last_error}
        if self._last_error:
            return {**latest, "last_error": self._last_error}
        return latest

    def refresh_once(self) -> dict[str, Any]:
        rows = self.repository.list_recent_rows(max_hours=max(PIPELINE_TIMING_WINDOWS))
        payload = build_pipeline_timing_dashboard(rows)
        self.local_store.save(payload)
        self.local_store.prune_old()
        try:
            self.repository.archive_dashboard(payload)
        except Exception as exc:
            print(f"[odaily] pipeline timing snapshot archive skipped error={exc}")
        return payload

    def _run(self) -> None:
        print("[odaily] pipeline timing snapshotter started")
        while not self._stop_event.is_set():
            try:
                latest_generated_at = self.local_store.latest_generated_at()
                is_due = latest_generated_at is None or datetime.now(UTC) - latest_generated_at >= timedelta(
                    seconds=self.refresh_interval_seconds
                )
                if is_due:
                    self.refresh_once()
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
                print(f"[odaily] pipeline timing snapshot failed: {exc}")
            self._wake_event.wait(self.refresh_interval_seconds)
            self._wake_event.clear()


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
