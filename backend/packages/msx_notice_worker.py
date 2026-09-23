from __future__ import annotations

import os
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from packages.common.storage import connect_sqlite, load_storage_settings
from packages.local_pipeline import LocalPipelineClient
from packages.msx_notice import MSXNotice, MSXNoticeClient
from packages.x_processing.sqlite_repository import SQLITE_SCHEMA_SQL, _json


MSX_SOURCE = "msx"
MSX_SITE_KEY = "msx_notice_center"
MSX_DISPLAY_NAME = "MSX"
DEFAULT_INTERVAL_SECONDS = 600
DEFAULT_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class MSXNoticeRunStats:
    status: str
    candidate_count: int = 0
    seeded_count: int = 0
    new_count: int = 0
    saved_count: int = 0
    detail_errors: dict[str, str] | None = None


class MSXNoticeWorker:
    """Poll MSX notices and hand new full-text notices to local_pipeline."""

    def __init__(
        self,
        *,
        database_path: Path | None = None,
        client: MSXNoticeClient | None = None,
        pipeline_client: LocalPipelineClient | None = None,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        if interval_seconds < 1:
            raise ValueError("interval_seconds must be positive")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        self.database_path = database_path or load_storage_settings().sqlite_path
        self.client = client or MSXNoticeClient()
        self.pipeline_client = pipeline_client
        self.interval_seconds = interval_seconds
        self.page_size = page_size
        self.worker_id = f"msx-notice-{os.getpid()}"
        self._stop_event = threading.Event()
        self.init_schema()

    def init_schema(self) -> None:
        with connect_sqlite(self.database_path) as conn:
            conn.executescript(
                SQLITE_SCHEMA_SQL
                + """
                CREATE TABLE IF NOT EXISTS msx_notice_state(
                    singleton_key text PRIMARY KEY CHECK(singleton_key = 'global'),
                    seeded_at text,
                    last_polled_at text,
                    last_success_at text,
                    last_error text,
                    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS msx_notice_seen_items(
                    source_item_id text PRIMARY KEY,
                    seeded integer NOT NULL DEFAULT 0,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.execute("INSERT OR IGNORE INTO msx_notice_state(singleton_key) VALUES ('global')")
            conn.commit()

    def run_once(self) -> MSXNoticeRunStats:
        try:
            _total, notices = self.client.list_notices(page_index=1, page_size=self.page_size)
            if self._is_unseeded():
                self._seed(notices)
                stats = MSXNoticeRunStats(
                    status="success",
                    candidate_count=len(notices),
                    seeded_count=len(notices),
                )
                self._record_state(success=True)
                self._record_heartbeat(stats)
                return stats

            unseen = self._unseen(notices)
            detail_errors: dict[str, str] = {}
            new_count = 0
            saved_count = 0
            for notice in notices:
                source_item_id = self.source_item_id(notice)
                if source_item_id not in unseen:
                    continue
                try:
                    detail = self.client.get_detail(notice.id)
                    detailed_notice = replace(notice, **detail)
                    if not (detailed_notice.content or "").strip():
                        raise ValueError("MSX notice detail content is empty")
                    task_id = self._save_task(detailed_notice)
                    if self.pipeline_client is not None:
                        self.pipeline_client.submit_job(
                            job_type="write_flow",
                            task_id=task_id,
                            source=MSX_SOURCE,
                            source_item_id=source_item_id,
                        )
                    if self._mark_seen(source_item_id, seeded=False):
                        new_count += 1
                        saved_count += 1
                except Exception as exc:
                    detail_errors[source_item_id] = str(exc)

            stats = MSXNoticeRunStats(
                status="success" if not detail_errors else "parse_failed",
                candidate_count=len(notices),
                new_count=new_count,
                saved_count=saved_count,
                detail_errors=detail_errors,
            )
            self._record_state(success=not detail_errors, error=f"{len(detail_errors)} detail item(s) failed" if detail_errors else None)
            self._record_heartbeat(stats)
            return stats
        except Exception as exc:
            stats = MSXNoticeRunStats(status="fetch_failed", detail_errors={"list": str(exc)})
            self._record_state(success=False, error=str(exc))
            self._record_heartbeat(stats)
            return stats

    def run_forever(self) -> None:
        self._stop_event.clear()
        print(
            f"[odaily] MSX notice worker started. interval={self.interval_seconds}s "
            f"page_size={self.page_size}"
        )
        while not self._stop_event.is_set():
            stats = self.run_once()
            print(
                "[odaily] MSX notices "
                f"status={stats.status} candidates={stats.candidate_count} "
                f"seeded={stats.seeded_count} new={stats.new_count} saved={stats.saved_count}"
            )
            self._stop_event.wait(self.interval_seconds)

    def stop(self) -> None:
        self._stop_event.set()

    @staticmethod
    def source_item_id(notice: MSXNotice) -> str:
        return f"{MSX_SOURCE}:{notice.id}"

    def _is_unseeded(self) -> bool:
        with connect_sqlite(self.database_path) as conn:
            row = conn.execute("SELECT seeded_at FROM msx_notice_state WHERE singleton_key='global'").fetchone()
        return not row or not row["seeded_at"]

    def _seed(self, notices: list[MSXNotice]) -> None:
        with connect_sqlite(self.database_path) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO msx_notice_seen_items(source_item_id, seeded) VALUES (?, 1)",
                ((self.source_item_id(notice),) for notice in notices),
            )
            conn.execute(
                "UPDATE msx_notice_state SET seeded_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE singleton_key='global'"
            )
            conn.commit()

    def _unseen(self, notices: list[MSXNotice]) -> set[str]:
        ids = [self.source_item_id(notice) for notice in notices]
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        with connect_sqlite(self.database_path) as conn:
            rows = conn.execute(
                f"SELECT source_item_id FROM msx_notice_seen_items WHERE source_item_id IN ({placeholders})",
                ids,
            ).fetchall()
        return set(ids) - {str(row["source_item_id"]) for row in rows}

    def _save_task(self, notice: MSXNotice) -> int:
        source_item_id = self.source_item_id(notice)
        published_at = datetime.fromisoformat(notice.published_at)
        metadata = {
            "site_key": MSX_SITE_KEY,
            "site_display_name": MSX_DISPLAY_NAME,
            "source_kind": MSX_SOURCE,
            "category": notice.category,
            "links": notice.links or [],
            "writer_template_key": "msx_notice_writer",
            "omit_site_attribution": True,
        }
        raw_payload = notice.to_json()
        with connect_sqlite(self.database_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO tasks(
                    source, source_item_id, source_url, title, content,
                    published_at, raw_payload, metadata, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    MSX_SOURCE,
                    source_item_id,
                    notice.detail_url,
                    notice.title,
                    notice.content or "",
                    published_at.astimezone(UTC).isoformat(),
                    _json(raw_payload),
                    _json(metadata),
                ),
            )
            row = conn.execute(
                "SELECT id FROM tasks WHERE source=? AND source_item_id=?",
                (MSX_SOURCE, source_item_id),
            ).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError(f"failed to save MSX task: {source_item_id}")
        return int(row["id"])

    def _mark_seen(self, source_item_id: str, *, seeded: bool) -> bool:
        with connect_sqlite(self.database_path) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO msx_notice_seen_items(source_item_id, seeded) VALUES (?, ?)",
                (source_item_id, int(seeded)),
            )
            conn.commit()
        return cur.rowcount > 0

    def _record_state(self, *, success: bool, error: str | None = None) -> None:
        with connect_sqlite(self.database_path) as conn:
            conn.execute(
                """
                UPDATE msx_notice_state
                SET last_polled_at=CURRENT_TIMESTAMP,
                    last_success_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE last_success_at END,
                    last_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE singleton_key='global'
                """,
                (int(success), error),
            )
            conn.commit()

    def _record_heartbeat(self, stats: MSXNoticeRunStats) -> None:
        error = None
        if stats.detail_errors:
            error = "; ".join(f"{key}: {value}" for key, value in stats.detail_errors.items())[:2000]
        with connect_sqlite(self.database_path) as conn:
            conn.execute(
                """
                INSERT INTO pipeline_worker_heartbeats(
                    component, worker_id, status, last_seen_at, last_success_at, last_error, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(component, worker_id) DO UPDATE SET
                    status=excluded.status,
                    last_seen_at=excluded.last_seen_at,
                    last_success_at=COALESCE(excluded.last_success_at, pipeline_worker_heartbeats.last_success_at),
                    last_error=excluded.last_error,
                    metadata=excluded.metadata,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    "msx_notice",
                    self.worker_id,
                    "ok" if stats.status == "success" else "failed",
                    datetime.now(UTC).isoformat(),
                    datetime.now(UTC).isoformat() if stats.status == "success" else None,
                    error,
                    _json({
                        "candidate_count": stats.candidate_count,
                        "seeded_count": stats.seeded_count,
                        "new_count": stats.new_count,
                        "saved_count": stats.saved_count,
                    }),
                ),
            )
            conn.commit()
