import sqlite3
from datetime import UTC, datetime, timedelta, timezone

from packages.auditor.sqlite_repository import SQLiteAuditorRepository
from packages.competitor_monitor.events import EventAssignment
from packages.competitor_monitor.fetchers import NewsflashItem
from packages.competitor_monitor.repository import parse_datetime, parse_newsflash_published_at
from packages.competitor_monitor.sqlite_repository import SQLiteCompetitorMonitorRepository
from packages.x_processing.searcher import SearchCache, SearchDocument


SHANGHAI_OFFSET = timezone(timedelta(hours=8))


def test_competitor_datetime_parser_normalizes_to_utc() -> None:
    parsed = parse_datetime("2026-08-07T08:05:05+08:00")

    assert parsed == datetime(2026, 8, 7, 0, 5, 5, tzinfo=UTC)
    assert parsed.isoformat().endswith("+00:00")


def test_panews_system_published_at_is_shifted_one_minute() -> None:
    raw = "2026-08-07T08:05:05+08:00"

    assert parse_newsflash_published_at("panews", raw) == datetime(2026, 8, 7, 0, 6, 5, tzinfo=UTC)
    assert parse_newsflash_published_at("blockbeats", raw) == datetime(2026, 8, 7, 0, 5, 5, tzinfo=UTC)


def test_competitor_repository_applies_panews_correction_to_tasks_and_items(tmp_path) -> None:
    database_path = tmp_path / "panews-correction.sqlite"
    repository = SQLiteCompetitorMonitorRepository(database_path)
    item = NewsflashItem(
        source="panews",
        source_item_id="panews-1",
        title="PANews",
        content="测试正文",
        published_at="2026-08-07T08:05:05+08:00",
        raw_payload={"publishedAt": "2026-08-07T08:05:05+08:00"},
    )

    repository.save_items_for_pipeline([item])
    record = repository.upsert_newsflash_items([item])[0]

    with sqlite3.connect(database_path) as conn:
        task_stored = conn.execute(
            "SELECT published_at FROM tasks WHERE source='panews' AND source_item_id='panews-1'"
        ).fetchone()[0]
        item_stored = conn.execute(
            "SELECT published_at FROM newsflash_items WHERE source='panews' AND source_item_id='panews-1'"
        ).fetchone()[0]

    assert record.published_at == datetime(2026, 8, 7, 0, 6, 5, tzinfo=UTC)
    assert task_stored == "2026-08-07T00:06:05+00:00"
    assert item_stored == "2026-08-07T00:06:05+00:00"


def test_panews_correction_changes_event_first_source(tmp_path) -> None:
    database_path = tmp_path / "panews-first-source.sqlite"
    repository = SQLiteCompetitorMonitorRepository(database_path)
    records = repository.upsert_newsflash_items(
        [
            NewsflashItem(
                source="panews",
                source_item_id="panews-first",
                title="PANews first",
                content="测试正文",
                published_at="2026-08-07T08:05:05+08:00",
                raw_payload={"publishedAt": "2026-08-07T08:05:05+08:00"},
            ),
            NewsflashItem(
                source="odaily",
                source_item_id="odaily-first",
                title="Odaily first",
                content="测试正文",
                published_at="2026-08-07T08:05:05+08:00",
            ),
        ]
    )
    event_id = repository.create_event_with_source(records[0])
    repository.assign_item_to_event(
        EventAssignment(
            item_id=records[1].id,
            event_id=event_id,
            role="supporting",
            match_method="test",
            similarity=1.0,
            matched_item_id=records[0].id,
        )
    )

    with sqlite3.connect(database_path) as conn:
        event = conn.execute(
            "SELECT first_source,first_published_at FROM newsflash_events WHERE event_id=?",
            (event_id,),
        ).fetchone()

    assert event == ("odaily", "2026-08-07T00:05:05+00:00")


def test_panews_timestamp_repair_is_idempotent(tmp_path) -> None:
    database_path = tmp_path / "panews-repair.sqlite"
    repository = SQLiteCompetitorMonitorRepository(database_path)
    repository.save_items_for_pipeline(
        [
            NewsflashItem(
                source="panews",
                source_item_id="panews-repair",
                title="PANews repair",
                content="测试正文",
                published_at="2026-08-07T08:05:05+08:00",
                raw_payload={"publishedAt": "2026-08-07T08:05:05+08:00"},
            )
        ]
    )
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE tasks SET published_at='2026-08-07T08:05:05+08:00' "
            "WHERE source='panews' AND source_item_id='panews-repair'"
        )
        conn.commit()

    first = repository.repair_newsflash_timestamps()
    second = repository.repair_newsflash_timestamps()

    assert first["updated_items"] == 1
    assert second == {"updated_items": 0, "updated_events": 0}


def test_panews_timestamp_repair_can_be_limited_to_source_and_date_range(tmp_path) -> None:
    database_path = tmp_path / "panews-repair-range.sqlite"
    repository = SQLiteCompetitorMonitorRepository(database_path)
    repository.save_items_for_pipeline(
        [
            NewsflashItem(
                source="panews",
                source_item_id="panews-old",
                title="PANews old",
                content="测试正文",
                published_at="2026-07-20T08:05:05+08:00",
                raw_payload={"publishedAt": "2026-07-20T08:05:05+08:00"},
            ),
            NewsflashItem(
                source="panews",
                source_item_id="panews-recent",
                title="PANews recent",
                content="测试正文",
                published_at="2026-08-07T08:05:05+08:00",
                raw_payload={"publishedAt": "2026-08-07T08:05:05+08:00"},
            ),
            NewsflashItem(
                source="blockbeats",
                source_item_id="blockbeats-recent",
                title="BlockBeats recent",
                content="测试正文",
                published_at="2026-08-07T08:05:05+08:00",
                raw_payload={"publishedAt": "2026-08-07T08:05:05+08:00"},
            ),
        ]
    )
    with sqlite3.connect(database_path) as conn:
        conn.execute("UPDATE tasks SET published_at='2026-08-07T00:05:05+00:00' WHERE source='panews' AND source_item_id='panews-recent'")
        conn.commit()

    result = repository.repair_newsflash_timestamps(
        source="panews",
        since=datetime(2026, 7, 28, tzinfo=UTC),
        until=datetime(2026, 8, 29, tzinfo=UTC),
    )

    with sqlite3.connect(database_path) as conn:
        recent = conn.execute("SELECT published_at FROM tasks WHERE source='panews' AND source_item_id='panews-recent'").fetchone()[0]
        old = conn.execute("SELECT published_at FROM tasks WHERE source='panews' AND source_item_id='panews-old'").fetchone()[0]
        other = conn.execute("SELECT published_at FROM tasks WHERE source='blockbeats' AND source_item_id='blockbeats-recent'").fetchone()[0]

    assert result["updated_items"] == 1
    assert recent == "2026-08-07T00:06:05+00:00"
    assert old == "2026-07-20T00:06:05+00:00"
    assert other == "2026-08-07T00:05:05+00:00"


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
