from datetime import UTC, datetime

from packages.auditor.sqlite_repository import SQLiteAuditorRepository
from packages.writer3.sqlite_repository import SQLiteWriter3Repository


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        current = cls(2026, 8, 7, 0, 5, 12, tzinfo=UTC)
        return current if tz is None else current.astimezone(tz)


def test_claim_task_accepts_recent_published_at_with_shanghai_offset(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "auditor.sqlite"
    repository = SQLiteAuditorRepository(database_path)
    repository.init_schema()

    from packages.common.storage import connect_sqlite

    with connect_sqlite(database_path) as conn:
        conn.execute(
            """
            CREATE TABLE odaily_reference_items (
                source_item_id text PRIMARY KEY,
                source_url text,
                title text,
                content text NOT NULL,
                published_at text,
                metadata text NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO odaily_reference_items
                (source_item_id, source_url, title, content, published_at, metadata)
            VALUES (?, ?, ?, ?, ?, '{}')
            """,
            (
                "507470",
                "https://www.odaily.news/zh-CN/newsflash/507470",
                "Wintermute注册美国经纪交易商牌照",
                "测试正文",
                "2026-08-07T08:05:05+08:00",
            ),
        )
        conn.commit()

    monkeypatch.setattr("packages.auditor.sqlite_repository.datetime", FrozenDateTime)

    task = repository.claim_task(
        worker_id="test-auditor",
        prompt_version="test-v1",
        lookback_minutes=120,
    )

    assert task is not None
    assert task.source_item_id == "507470"
    assert task.published_at == datetime(2026, 8, 7, 0, 5, 5, tzinfo=UTC)


def test_writer3_claim_task_accepts_recent_published_at_with_shanghai_offset(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "writer3.sqlite"
    repository = SQLiteWriter3Repository(database_path)
    repository.init_schema()

    from packages.common.storage import connect_sqlite

    with connect_sqlite(database_path) as conn:
        conn.execute(
            """
            INSERT INTO odaily_reference_items
                (source_item_id, source_url, title, content, published_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "507470",
                "https://www.odaily.news/zh-CN/newsflash/507470",
                "Wintermute注册美国经纪交易商牌照",
                "测试正文",
                "2026-08-07T08:05:05+08:00",
            ),
        )
        conn.commit()

    monkeypatch.setattr("packages.writer3.sqlite_repository.datetime", FrozenDateTime)

    task = repository.claim_task(
        worker_id="test-writer3",
        start_after=datetime(2026, 8, 6, tzinfo=UTC),
        freshness_window_seconds=1200,
    )

    assert task is not None
    assert task.source_item_id == "507470"
    assert task.published_at == datetime(2026, 8, 7, 0, 5, 5, tzinfo=UTC)
