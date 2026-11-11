from datetime import UTC, datetime
import sqlite3
import threading

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


def test_auditor_candidate_processing_does_not_block_other_writers(tmp_path, monkeypatch) -> None:
    import packages.auditor.sqlite_repository as auditor
    from packages.common.storage import connect_sqlite

    path = tmp_path / "auditor.sqlite"
    repository = SQLiteAuditorRepository(path)
    repository.init_schema()
    with connect_sqlite(path) as conn:
        conn.execute("CREATE TABLE odaily_reference_items (source_item_id TEXT PRIMARY KEY, source_url TEXT, title TEXT, content TEXT, published_at TEXT, metadata TEXT)")
        conn.execute("INSERT INTO odaily_reference_items VALUES ('1', NULL, 'title', 'body', '2026-08-07T00:05:05+00:00', '{}')")
        conn.execute("CREATE TABLE unrelated_business (value INTEGER)")
    monkeypatch.setattr(auditor, "datetime", FrozenDateTime)
    entered = threading.Event()
    release = threading.Event()
    original_hash = auditor.calculate_content_hash

    def delayed_hash(title, content):
        entered.set()
        assert release.wait(5)
        return original_hash(title, content)

    monkeypatch.setattr(auditor, "calculate_content_hash", delayed_hash)
    results = []
    thread = threading.Thread(target=lambda: results.append(repository.claim_task(worker_id="w", prompt_version="v", lookback_minutes=120)))
    thread.start()
    try:
        assert entered.wait(5)
        with sqlite3.connect(path, timeout=0.1) as other:
            other.execute("INSERT INTO unrelated_business VALUES (1)")
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert results[0] is not None


def test_auditor_rechecks_candidate_and_lease_before_claim(tmp_path, monkeypatch) -> None:
    import packages.auditor.sqlite_repository as auditor
    from packages.common.storage import connect_sqlite

    path = tmp_path / "auditor.sqlite"
    repository = SQLiteAuditorRepository(path)
    repository.init_schema()
    with connect_sqlite(path) as conn:
        conn.execute("CREATE TABLE odaily_reference_items (source_item_id TEXT PRIMARY KEY, source_url TEXT, title TEXT, content TEXT, published_at TEXT, metadata TEXT)")
        conn.execute("INSERT INTO odaily_reference_items VALUES ('1', NULL, 'title', 'body', '2026-08-07T00:05:05+00:00', '{}')")
    monkeypatch.setattr(auditor, "datetime", FrozenDateTime)
    barrier = threading.Barrier(2)
    original_hash = auditor.calculate_content_hash

    def simultaneous_hash(title, content):
        barrier.wait(timeout=5)
        return original_hash(title, content)

    monkeypatch.setattr(auditor, "calculate_content_hash", simultaneous_hash)
    results = []
    threads = [threading.Thread(target=lambda: results.append(repository.claim_task(worker_id="w", prompt_version="v", lookback_minutes=120))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert sum(task is not None for task in results) == 1


def test_auditor_does_not_claim_stale_source_snapshot(tmp_path, monkeypatch) -> None:
    import packages.auditor.sqlite_repository as auditor
    from packages.common.storage import connect_sqlite

    path = tmp_path / "auditor.sqlite"
    repository = SQLiteAuditorRepository(path)
    repository.init_schema()
    with connect_sqlite(path) as conn:
        conn.execute("CREATE TABLE odaily_reference_items (source_item_id TEXT PRIMARY KEY, source_url TEXT, title TEXT, content TEXT, published_at TEXT, metadata TEXT)")
        conn.execute("INSERT INTO odaily_reference_items VALUES ('1', NULL, 'title', 'body', '2026-08-07T00:05:05+00:00', '{}')")
    monkeypatch.setattr(auditor, "datetime", FrozenDateTime)
    entered = threading.Event()
    release = threading.Event()
    original_hash = auditor.calculate_content_hash

    def delayed_hash(title, content):
        entered.set()
        assert release.wait(5)
        return original_hash(title, content)

    monkeypatch.setattr(auditor, "calculate_content_hash", delayed_hash)
    results = []
    thread = threading.Thread(target=lambda: results.append(repository.claim_task(worker_id="w", prompt_version="v", lookback_minutes=120)))
    thread.start()
    try:
        assert entered.wait(5)
        with connect_sqlite(path) as conn:
            conn.execute("UPDATE odaily_reference_items SET content='updated' WHERE source_item_id='1'")
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert results == [None]
    with connect_sqlite(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM auditor_checks").fetchone()[0] == 0


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


def test_auditor_and_writer3_use_time_index_for_actual_candidate_queries(tmp_path, monkeypatch) -> None:
    import packages.auditor.sqlite_repository as auditor
    import packages.writer3.sqlite_repository as writer3
    from packages.common.storage import connect_sqlite
    from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository

    path = tmp_path / "queries.sqlite"
    SQLiteXProcessingRepository(path)
    writer3_repository = SQLiteWriter3Repository(path)
    writer3_repository.init_schema()
    auditor_repository = SQLiteAuditorRepository(path)
    auditor_repository.init_schema()
    with connect_sqlite(path) as conn:
        conn.executemany(
            "INSERT INTO odaily_reference_items(source_item_id,title,content,published_at) VALUES(?,?,?,?)",
            [
                ("early", "early", "body", "2026-08-06T19:00:00-05:00"),
                ("later", "later", "body", "2026-08-07T08:05:05+08:00"),
                ("outside", "outside", "body", "2026-08-05T00:00:00+00:00"),
            ],
        )
    monkeypatch.setattr(auditor, "datetime", FrozenDateTime)
    monkeypatch.setattr(writer3, "datetime", FrozenDateTime)
    captured: dict[str, str] = {}
    original_read = auditor.connect_sqlite_readonly
    original_write = writer3.connect_sqlite

    def trace_connection(original, kind):
        def connect(*args, **kwargs):
            conn = original(*args, **kwargs)
            def capture(sql):
                upper = sql.upper()
                if "FROM ODAILY_REFERENCE_ITEMS" in upper and "ORDER BY JULIANDAY" in upper:
                    captured[kind] = sql
            conn.set_trace_callback(capture)
            return conn
        return connect

    monkeypatch.setattr(auditor, "connect_sqlite_readonly", trace_connection(original_read, "audit"))
    monkeypatch.setattr(writer3, "connect_sqlite", trace_connection(original_write, "writer3"))
    first = auditor_repository.claim_task(worker_id="audit", prompt_version="v1", lookback_minutes=120)
    second = writer3_repository.claim_task(worker_id="writer", start_after=datetime(2026, 8, 6, tzinfo=UTC), freshness_window_seconds=7200)
    assert first is not None and first.source_item_id == "early"
    assert second is not None and second.source_item_id == "early"
    assert set(captured) == {"audit", "writer3"}
    with sqlite3.connect(path) as conn:
        for name, sql in captured.items():
            plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
            assert any("idx_odaily_reference_julian" in line for line in plan), (name, plan)
            assert not any("TEMP B-TREE FOR ORDER BY" in line for line in plan), (name, plan)


def test_fact_auditor_finds_current_version_through_time_index(tmp_path, monkeypatch) -> None:
    import packages.fact_auditor.repository as fact
    from packages.common.storage import connect_sqlite
    from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository

    path = tmp_path / "fact.sqlite"
    SQLiteXProcessingRepository(path)
    repository = fact.FactReviewRepository(path)
    with connect_sqlite(path) as conn:
        conn.execute("INSERT INTO odaily_reference_items(source_item_id,title,content,published_at) VALUES(?,?,?,?)",
                     ("1", "title", "body", "2026-08-07T08:05:05+08:00"))
    monkeypatch.setattr(fact, "datetime", FrozenDateTime)
    captured = []
    original_connect = fact.connect_sqlite

    def traced_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        def capture(sql):
            upper = sql.upper()
            if "FROM ODAILY_REFERENCE_ITEMS R" in upper and "NOT EXISTS" in upper:
                captured.append(sql)
        conn.set_trace_callback(capture)
        return conn

    monkeypatch.setattr(fact, "connect_sqlite", traced_connect)
    task = repository.claim(worker_id="fact", lookback_minutes=120, max_attempts=3)
    assert task is not None and task["source_item_id"] == "1"
    assert len(captured) == 1
    with sqlite3.connect(path) as conn:
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + captured[0])]
    assert any("idx_odaily_reference_julian" in line for line in plan), plan
