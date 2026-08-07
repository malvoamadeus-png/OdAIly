import sqlite3
from datetime import UTC, datetime, timedelta, timezone

from packages.auditor.sqlite_repository import SQLiteAuditorRepository
from packages.competitor_monitor.fetchers import NewsflashItem
from packages.competitor_monitor.repository import parse_datetime
from packages.competitor_monitor.sqlite_repository import SQLiteCompetitorMonitorRepository
from packages.x_processing.searcher import SearchCache, SearchDocument


SHANGHAI_OFFSET = timezone(timedelta(hours=8))


def test_competitor_datetime_parser_normalizes_to_utc() -> None:
    parsed = parse_datetime("2026-08-07T08:05:05+08:00")

    assert parsed == datetime(2026, 8, 7, 0, 5, 5, tzinfo=UTC)
    assert parsed.isoformat().endswith("+00:00")


def test_competitor_repository_stores_reference_published_at_in_utc(tmp_path) -> None:
    database_path = tmp_path / "competitor.sqlite"
    repository = SQLiteCompetitorMonitorRepository(database_path)

    repository.save_items_for_pipeline(
        [
            NewsflashItem(
                source="odaily",
                source_item_id="507470",
                title="Wintermute注册美国经纪交易商牌照",
                content="测试正文",
                published_at="2026-08-07T08:05:05+08:00",
            )
        ]
    )

    with sqlite3.connect(database_path) as conn:
        stored = conn.execute(
            "SELECT published_at FROM odaily_reference_items WHERE source_item_id='507470'"
        ).fetchone()[0]

    assert stored == "2026-08-07T00:05:05+00:00"


def test_competitor_timestamp_repair_normalizes_legacy_reference_rows(tmp_path) -> None:
    database_path = tmp_path / "competitor-repair.sqlite"
    repository = SQLiteCompetitorMonitorRepository(database_path)
    SQLiteAuditorRepository(database_path).init_schema()
    repository.save_items_for_pipeline(
        [
            NewsflashItem(
                source="odaily",
                source_item_id="legacy-offset",
                title="Legacy",
                content="测试正文",
                published_at="2026-08-07T08:05:05+08:00",
            ),
            NewsflashItem(
                source="blockbeats",
                source_item_id="legacy-task-offset",
                title="Legacy task",
                content="测试正文",
                published_at="2026-08-07T08:06:05+08:00",
            ),
        ]
    )
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE odaily_reference_items SET published_at='2026-08-07T08:05:05+08:00' "
            "WHERE source_item_id='legacy-offset'"
        )
        conn.execute(
            "UPDATE tasks SET published_at='2026-08-07T08:06:05+08:00' "
            "WHERE source='blockbeats' AND source_item_id='legacy-task-offset'"
        )
        conn.execute(
            """
            INSERT INTO auditor_checks
                (source_item_id, content, content_hash, published_at, prompt_version)
            VALUES ('legacy-offset', '测试正文', 'hash',
                    '2026-08-07T08:05:05+08:00', 'test-v1')
            """
        )
        conn.commit()

    result = repository.repair_newsflash_timestamps()

    with sqlite3.connect(database_path) as conn:
        stored = conn.execute(
            "SELECT published_at FROM odaily_reference_items WHERE source_item_id='legacy-offset'"
        ).fetchone()[0]
        task_stored = conn.execute(
            "SELECT published_at FROM tasks WHERE source='blockbeats' "
            "AND source_item_id='legacy-task-offset'"
        ).fetchone()[0]
        auditor_stored = conn.execute(
            "SELECT published_at FROM auditor_checks WHERE source_item_id='legacy-offset'"
        ).fetchone()[0]
    assert result == {"updated_items": 3, "updated_events": 0}
    assert stored == "2026-08-07T00:05:05+00:00"
    assert task_stored == "2026-08-07T00:06:05+00:00"
    assert auditor_stored == "2026-08-07T00:05:05+00:00"


def test_search_cache_normalizes_new_rows_and_reads_legacy_offsets(tmp_path) -> None:
    database_path = tmp_path / "searcher.sqlite"
    cache = SearchCache(database_path)
    published = datetime(2026, 8, 7, 8, 5, 5, tzinfo=SHANGHAI_OFFSET)
    cache.upsert_document(
        SearchDocument(
            doc_type="odaily_reference",
            doc_id="new-utc",
            title="UTC",
            content="测试正文",
            source="odaily",
            published_at=published,
        )
    )

    with sqlite3.connect(database_path) as conn:
        stored = conn.execute(
            "SELECT published_at FROM documents WHERE doc_id='new-utc'"
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO documents
                (cache_key, doc_type, doc_id, source, title, content, published_at,
                 metadata_json, content_hash, updated_at)
            VALUES ('legacy-offset', 'odaily_reference', 'legacy-offset', 'odaily',
                    'Legacy', '测试正文', '2026-08-07T08:05:05+08:00', '{}',
                    'hash', '2026-08-07T00:05:06+00:00')
            """
        )
        conn.commit()

    documents = cache.list_odaily_reference_documents(
        since=datetime(2026, 8, 7, 0, 5, tzinfo=UTC)
    )

    assert stored == "2026-08-07T00:05:05+00:00"
    assert {document.doc_id for document in documents} == {"new-utc", "legacy-offset"}
