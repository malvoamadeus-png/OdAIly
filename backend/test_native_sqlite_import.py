from __future__ import annotations

import os
import gzip
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from psycopg import connect
from psycopg.sql import Identifier, SQL


def test_system_information_preflight_skips_rebuildable_search_postings(tmp_path) -> None:
    from packages.common.native_sqlite_import import (
        NATIVE_SOURCES,
        SCHEMA_INITIALIZERS,
        SourceMapping,
        TableMapping,
        preflight_native_sqlite_source,
    )

    source_path = tmp_path / "system-information.sqlite"
    with sqlite3.connect(source_path) as source:
        source.executescript(
            """
            CREATE TABLE system_information_items(item_id TEXT PRIMARY KEY);
            CREATE TABLE system_information_search(item_id TEXT NOT NULL, term TEXT NOT NULL,
                PRIMARY KEY(item_id, term));
            INSERT INTO system_information_items VALUES ('item-1');
            INSERT INTO system_information_search VALUES ('item-1', 'historical-token');
            """
        )

    mapping = NATIVE_SOURCES["system_information"]
    assert "system_information_search" in mapping.retired_tables
    assert "system_information_search" not in {table.source_table for table in mapping.tables}
    fixture_mapping = SourceMapping(
        "system_information",
        "system_observability",
        (TableMapping("system_information_items", "system_observability", "system_information_items"),),
        retired_tables=("system_information_search",),
    )
    result = preflight_native_sqlite_source(
        source_key="system_information", source_path=source_path, source_mapping=fixture_mapping
    )
    assert result["ready_for_import"]
    assert not result["unknown_tables"]
    assert {table.source_table for table in mapping.tables} >= {"system_information_items"}
    assert "system_information" in SCHEMA_INITIALIZERS
    assert not [
        key for key, source in NATIVE_SOURCES.items()
        if any(not table.legacy_typed for table in source.tables) and key not in SCHEMA_INITIALIZERS
    ]


def test_main_import_mapping_retains_compound_seen_item_identity() -> None:
    from packages.common.native_sqlite_import import NATIVE_SOURCES

    mapping = NATIVE_SOURCES["main"]
    seen = next(table for table in mapping.tables if table.source_table == "non_mainstream_media_seen_items")
    assert seen.target_table == "non_mainstream_media_seen_items"
    assert not seen.legacy_typed


def test_observability_source_identity_map_covers_known_file_basenames() -> None:
    from packages.common.native_sqlite_import import _observability_source_schema_map

    source_map = _observability_source_schema_map()
    assert source_map["/opt/OdAIly/data/database/odaily.sqlite"] == "main"
    assert source_map["/opt/OdAIly/data/runtime/hottopic.sqlite"] == "hottopic"
    assert source_map["/data/OdAIly/system-observability/system_information.sqlite"] == "system_observability"
    assert source_map["/data/OdAIly/hyperliquid/hyperliquid.sqlite"] == "hyperliquid_news"
    expected = {
        "odaily.sqlite": "main",
        "hottopic.sqlite": "hottopic",
        "system_information.sqlite": "system_observability",
        "business_news.sqlite": "business_news",
        "pipeline_timing.sqlite": "pipeline_timing",
    }
    for basename, schema in expected.items():
        assert source_map[basename] == schema


def test_import_postgres_connection_uses_migration_timeouts(monkeypatch) -> None:
    from contextlib import contextmanager
    from packages.common import native_sqlite_import as importer
    from packages.common.storage import PostgresSettings

    settings = PostgresSettings("local", 5, 30_000, 5_000, 60_000, 600_000, 60_000, 300_000, 2, 5, 16, "test")
    statements = []
    class Connection:
        def execute(self, statement, params):
            statements.append((statement, params))
    @contextmanager
    def connect(**kwargs):
        yield Connection()
    monkeypatch.setattr(importer, "load_postgres_settings", lambda: settings)
    monkeypatch.setattr(importer, "connect_postgres", connect)
    with importer._connect_native_import_postgres(schema="fixture", application_name="test"):
        pass
    assert "600000ms" in statements[0][1]
    assert "60000ms" in statements[0][1]
    assert "300000ms" in statements[0][1]


def test_native_import_does_not_run_full_source_foreign_key_check(tmp_path, monkeypatch) -> None:
    from packages.common import native_sqlite_import as importer

    source_path = tmp_path / "source.sqlite"
    with sqlite3.connect(source_path) as source:
        source.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")

    traced_sql = []
    sqlite_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = sqlite_connect(*args, **kwargs)
        connection.set_trace_callback(traced_sql.append)
        return connection

    def stop_after_source_schema(_connection, _mappings):
        raise RuntimeError("stop after source schema inspection")

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    monkeypatch.setattr(importer, "_order_by_foreign_keys", stop_after_source_schema)
    mapping = importer.SourceMapping(
        "fixture", "fixture", (importer.TableMapping("items", "fixture", "items"),)
    )

    with pytest.raises(RuntimeError, match="stop after source schema inspection"):
        importer.import_native_sqlite_source(
            source_key="fixture", source_path=source_path, source_mapping=mapping
        )

    assert not any(statement.strip().lower().startswith("pragma foreign_key_check") for statement in traced_sql)


def test_native_import_initial_pass_does_not_count_or_digest_scan_before_copy(
    native_import_target, tmp_path, monkeypatch
) -> None:
    from packages.common import native_sqlite_import as importer

    source_path = tmp_path / "source.sqlite"
    with sqlite3.connect(source_path) as source:
        source.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        source.executemany("INSERT INTO items VALUES(?,?)", [(index, f"value-{index}") for index in range(1, 5)])

    traced_sql = []
    sqlite_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = sqlite_connect(*args, **kwargs)
        connection.set_trace_callback(traced_sql.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    mapping = importer.SourceMapping("fixture", native_import_target, (
        importer.TableMapping("items", native_import_target, "items"),
    ))
    with connect(os.environ["ODAILY_TEST_POSTGRES_DSN"], autocommit=True) as conn:
        conn.execute(SQL("CREATE TABLE {}.items(id bigint PRIMARY KEY,value text NOT NULL)").format(Identifier(native_import_target)))

    report = importer.import_native_sqlite_source(
        source_key="fixture", source_path=source_path, source_mapping=mapping, batch_size=2
    )

    normalized = [statement.strip().lower() for statement in traced_sql]
    data_reads = [statement for statement in normalized if statement.startswith('select * from "items"')]
    assert data_reads
    assert data_reads[0].endswith("limit 2")
    assert not any(statement.startswith('select count(*) from "items"') for statement in normalized)
    assert sum(statement.startswith('select * from "items" order by "id"') for statement in data_reads) == 2
    assert report["tables"][0]["source_rows"] == 4


def test_checkpoint_prefix_validation_rejects_changed_source_rows() -> None:
    from packages.common.native_sqlite_import import _validate_checkpoint_source_prefix

    rows = [{"id": 1, "value": "same"}, {"id": 2, "value": "changed"}]
    digest = lambda row: int.from_bytes(hashlib.sha256(row["value"].encode()).digest(), "big")
    expected = digest(rows[0]) + digest({"id": 2, "value": "old"})
    with pytest.raises(RuntimeError, match="checkpoint prefix mismatch"):
        _validate_checkpoint_source_prefix(
            rows=rows, source_pk=("id",), last_identity=(2,), imported_rows=2,
            digest_sum=expected, row_digest=digest,
        )


def test_fact_checks_keyset_queries_use_rowid_without_sorting(tmp_path) -> None:
    source_path = tmp_path / "fact-checks.sqlite"
    with sqlite3.connect(source_path) as source:
        source.execute(
            "CREATE TABLE fact_checks(id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)"
        )
        source.executemany(
            "INSERT INTO fact_checks(payload) VALUES (?)",
            [(f"payload-{index}",) for index in range(1, 101)],
        )

        first_batch_plan = [
            row[3].upper()
            for row in source.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM fact_checks ORDER BY id LIMIT ?", (20,)
            )
        ]
        next_batch_plan = [
            row[3].upper()
            for row in source.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM fact_checks WHERE id>? ORDER BY id LIMIT ?",
                (40, 20),
            )
        ]

    assert any("SCAN FACT_CHECKS" in detail for detail in first_batch_plan)
    assert not any("TEMP B-TREE" in detail for detail in first_batch_plan)
    assert any("SEARCH FACT_CHECKS USING INTEGER PRIMARY KEY (ROWID>?)" in detail for detail in next_batch_plan)
    assert not any("TEMP B-TREE" in detail for detail in next_batch_plan)


def test_compound_primary_key_keyset_query_uses_index_range(tmp_path) -> None:
    source_path = tmp_path / "compound-key.sqlite"
    with sqlite3.connect(source_path) as source:
        source.execute(
            "CREATE TABLE items(site_key TEXT NOT NULL, source_item_id TEXT NOT NULL, payload TEXT, "
            "PRIMARY KEY(site_key, source_item_id))"
        )
        source.executemany(
            "INSERT INTO items VALUES (?, ?, ?)",
            [(f"site-{site}", f"item-{item}", "payload") for site in range(4) for item in range(20)],
        )
        plan = [
            row[3].upper()
            for row in source.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM items WHERE (site_key,source_item_id)>(?,?) "
                "ORDER BY site_key,source_item_id LIMIT ?",
                ("site-1", "item-10", 10),
            )
        ]

    assert any("SEARCH ITEMS USING INDEX SQLITE_AUTOINDEX_ITEMS_1" in detail for detail in plan)
    assert not any("TEMP B-TREE" in detail for detail in plan)


def test_compound_primary_key_import_resumes_from_last_identity(native_import_target, tmp_path, monkeypatch) -> None:
    from packages.common import native_sqlite_import as importer

    source_path = tmp_path / "compound-resume.sqlite"
    with sqlite3.connect(source_path) as source:
        source.execute(
            "CREATE TABLE items(site_key TEXT NOT NULL, source_item_id TEXT NOT NULL, payload TEXT, "
            "PRIMARY KEY(site_key, source_item_id))"
        )
        source.executemany(
            "INSERT INTO items VALUES (?, ?, ?)",
            [("site-a", f"item-{index}", f"payload-{index}") for index in range(5)],
        )

    mapping = importer.SourceMapping("fixture", native_import_target, (
        importer.TableMapping("items", native_import_target, "items"),
    ))
    with connect(os.environ["ODAILY_TEST_POSTGRES_DSN"], autocommit=True) as conn:
        conn.execute(SQL("CREATE TABLE {}.items(site_key text NOT NULL,source_item_id text NOT NULL,payload text,PRIMARY KEY(site_key,source_item_id))").format(Identifier(native_import_target)))

    original_connect = importer._connect_native_import_postgres
    copy_count = 0
    @contextmanager
    def interrupt_after_first_batch(*, schema, application_name):
        nonlocal copy_count
        with original_connect(schema=schema, application_name=application_name) as conn:
            yield conn
        if application_name == "odaily-native-sqlite-import":
            copy_count += 1
            if copy_count == 1:
                raise RuntimeError("fixture interruption")
    monkeypatch.setattr(importer, "_connect_native_import_postgres", interrupt_after_first_batch)

    with pytest.raises(RuntimeError, match="fixture interruption"):
        importer.import_native_sqlite_source(
            source_key="fixture", source_path=source_path, source_mapping=mapping, batch_size=2
        )

    monkeypatch.setattr(importer, "_connect_native_import_postgres", original_connect)
    traced_sql = []
    sqlite_connect = sqlite3.connect
    def traced_connect(*args, **kwargs):
        connection = sqlite_connect(*args, **kwargs)
        connection.set_trace_callback(traced_sql.append)
        return connection
    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    importer.import_native_sqlite_source(
        source_key="fixture", source_path=source_path, source_mapping=mapping, batch_size=2
    )

    resume_reads = [statement.strip().lower() for statement in traced_sql
                    if statement.strip().lower().startswith('select * from "items" where')]
    assert resume_reads
    assert all('("site_key","source_item_id")>(' in statement for statement in resume_reads)
    with connect(os.environ["ODAILY_TEST_POSTGRES_DSN"]) as conn:
        assert conn.execute(SQL("SELECT site_key,source_item_id,payload FROM {}.items ORDER BY site_key,source_item_id").format(Identifier(native_import_target))).fetchall() == [
            ("site-a", f"item-{index}", f"payload-{index}") for index in range(5)
        ]


def test_sqlite_temp_directory_is_explicit_and_validated(tmp_path, monkeypatch) -> None:
    from packages.common import native_sqlite_import as importer

    data_root = tmp_path / "data"
    data_root.mkdir()
    temp_directory = data_root / "migration-tmp"
    temp_directory.mkdir()
    monkeypatch.setattr(importer, "DATA_ROOT", data_root)
    original = getattr(sqlite3, "temp_directory", None)
    monkeypatch.delenv("ODAILY_SQLITE_TMPDIR", raising=False)
    monkeypatch.delenv("SQLITE_TMPDIR", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)
    importer._configure_sqlite_temp_directory()
    assert getattr(sqlite3, "temp_directory", None) == original
    monkeypatch.setenv("ODAILY_SQLITE_TMPDIR", str(temp_directory))
    importer._configure_sqlite_temp_directory()
    assert getattr(sqlite3, "temp_directory", None) == str(temp_directory.resolve())
    monkeypatch.setenv("ODAILY_SQLITE_TMPDIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="under /data"):
        importer._configure_sqlite_temp_directory()
    monkeypatch.setenv("ODAILY_SQLITE_TMPDIR", str(data_root / "missing"))
    with pytest.raises(RuntimeError, match="does not exist"):
        importer._configure_sqlite_temp_directory()
    if original is None:
        delattr(sqlite3, "temp_directory")
    else:
        sqlite3.temp_directory = original


def test_cli_can_initialize_schemas_without_source_path(monkeypatch, capsys) -> None:
    from packages.common import native_sqlite_import

    called = []
    monkeypatch.setattr(native_sqlite_import, "initialize_native_schemas", lambda: called.append(True) or {"initialized": ["main"]})
    assert native_sqlite_import.main(["--initialize-schemas"]) == 0
    assert called == [True]
    assert '"main"' in capsys.readouterr().out


@pytest.fixture
def native_import_target(monkeypatch):
    dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set ODAILY_TEST_POSTGRES_DSN to run native SQLite import checks")
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", dsn)
    monkeypatch.setenv("ODAILY_POSTGRES_POOL_MAX_SIZE", "2")
    schema = "test_native_import_" + uuid.uuid4().hex[:10]
    with connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
    yield schema
    from packages.common.pg_runtime import close_postgres_pools

    close_postgres_pools()
    with connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(Identifier(schema)))


def test_native_import_resumes_committed_batches_and_preserves_fk_identity(
    native_import_target, tmp_path, monkeypatch
) -> None:
    from packages.common.native_sqlite_import import (
        SourceMapping,
        TableMapping,
        import_native_sqlite_source,
    )

    schema = native_import_target
    source_path = tmp_path / "source.sqlite"
    traced_sql = []
    sqlite_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = sqlite_connect(*args, **kwargs)
        connection.set_trace_callback(traced_sql.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    with sqlite3.connect(source_path) as source:
        source.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE parent(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES parent(id), value TEXT NOT NULL);
            INSERT INTO parent VALUES(7, 'preserve-primary-key');
            INSERT INTO child VALUES(11, 7, 'first'), (12, 7, 'interrupt-here'), (13, 7, 'last');
            """
        )
    mapping = SourceMapping(
        "fixture",
        schema,
        (TableMapping("child", schema, "child"), TableMapping("parent", schema, "parent")),
    )
    with connect(os.environ["ODAILY_TEST_POSTGRES_DSN"], autocommit=True) as conn:
        conn.execute(SQL("CREATE TABLE {}.parent(id bigint PRIMARY KEY, name text NOT NULL)").format(Identifier(schema)))
        conn.execute(SQL("CREATE TABLE {}.child(id bigint PRIMARY KEY, parent_id bigint NOT NULL REFERENCES {}.parent(id), value text NOT NULL)").format(Identifier(schema), Identifier(schema)))
        conn.execute(
            SQL("""CREATE FUNCTION {}.reject_fixture_row() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.id=12 THEN RAISE EXCEPTION 'fixture interruption'; END IF; RETURN NEW; END $$""").format(Identifier(schema))
        )
        conn.execute(SQL("CREATE TRIGGER reject_fixture_row BEFORE INSERT ON {}.child FOR EACH ROW EXECUTE FUNCTION {}.reject_fixture_row()").format(Identifier(schema), Identifier(schema)))

    with pytest.raises(Exception, match="fixture interruption"):
        import_native_sqlite_source(
            source_key="fixture", source_path=source_path, source_mapping=mapping, batch_size=1
        )

    with connect(os.environ["ODAILY_TEST_POSTGRES_DSN"], autocommit=True) as conn:
        conn.execute(SQL("DROP TRIGGER reject_fixture_row ON {}.child").format(Identifier(schema)))
        conn.execute(SQL("DROP FUNCTION {}.reject_fixture_row()").format(Identifier(schema)))

    report = import_native_sqlite_source(
        source_key="fixture", source_path=source_path, source_mapping=mapping, batch_size=1
    )
    assert report["table_count"] == 2
    assert len(report["foreign_key_audits"]) == 1
    assert not any(statement.strip().lower().startswith("pragma foreign_key_check") for statement in traced_sql)
    with connect(os.environ["ODAILY_TEST_POSTGRES_DSN"]) as conn:
        assert conn.execute(SQL("SELECT id,name FROM {}.parent").format(Identifier(schema))).fetchall() == [
            (7, "preserve-primary-key")
        ]
        assert conn.execute(SQL("SELECT id,parent_id,value FROM {}.child ORDER BY id").format(Identifier(schema))).fetchall() == [
            (11, 7, "first"), (12, 7, "interrupt-here"), (13, 7, "last")
        ]


def test_native_import_preserves_prompt_active_version_queue_state_json_null_and_sequence(
    native_import_target, tmp_path
) -> None:
    from packages.common.native_sqlite_import import SourceMapping, TableMapping, import_native_sqlite_source

    schema = native_import_target
    source_path = tmp_path / "representative.sqlite"
    with sqlite3.connect(source_path) as source:
        source.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE prompt_templates(template_key TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                active_version_id INTEGER, feature_mode_enabled INTEGER NOT NULL, feature_mode_text TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE prompt_template_versions(id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_key TEXT NOT NULL REFERENCES prompt_templates(template_key), version_number INTEGER NOT NULL,
                content TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL, published_at TEXT, deleted_at TEXT,
                UNIQUE(template_key,version_number));
            CREATE TABLE queue(id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL, payload TEXT, created_at TEXT NOT NULL);
            INSERT INTO prompt_templates VALUES ('judge-x','Judge X',12,1,'feature','2026-01-02T03:04:05+00:00','2026-01-03T03:04:05+00:00');
            INSERT INTO prompt_template_versions VALUES (11,'judge-x',1,'old','history','2026-01-01T00:00:00+00:00',NULL,NULL);
            INSERT INTO prompt_template_versions VALUES (12,'judge-x',2,'active','live','2026-01-02T03:04:05+00:00','2026-01-02T03:05:00+00:00',NULL);
            INSERT INTO queue VALUES (40,'running','{"nested":{"n":1.25},"tags":["a","b"]}','2026-01-02T03:04:05+00:00');
            INSERT INTO queue VALUES (41,'pending',NULL,'2026-01-02T03:05:05+00:00');
            """
        )
    mapping = SourceMapping("representative", schema, (
        TableMapping("prompt_template_versions", schema, "prompt_template_versions"),
        TableMapping("prompt_templates", schema, "prompt_templates"),
        TableMapping("queue", schema, "queue"),
    ))
    with connect(os.environ["ODAILY_TEST_POSTGRES_DSN"], autocommit=True) as conn:
        conn.execute(SQL("CREATE TABLE {}.prompt_templates(template_key text PRIMARY KEY, display_name text NOT NULL, active_version_id bigint, feature_mode_enabled boolean NOT NULL, feature_mode_text text NOT NULL, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL)").format(Identifier(schema)))
        conn.execute(SQL("CREATE TABLE {}.prompt_template_versions(id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, template_key text NOT NULL REFERENCES {}.prompt_templates(template_key), version_number integer NOT NULL, content text NOT NULL, note text, created_at timestamptz NOT NULL, published_at timestamptz, deleted_at timestamptz, UNIQUE(template_key,version_number))").format(Identifier(schema), Identifier(schema)))
        conn.execute(SQL("CREATE TABLE {}.queue(id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,status text NOT NULL,payload jsonb,created_at timestamptz NOT NULL)").format(Identifier(schema)))

    report = import_native_sqlite_source(source_key="representative", source_path=source_path, source_mapping=mapping, batch_size=1)
    assert report["table_count"] == 3
    with connect(os.environ["ODAILY_TEST_POSTGRES_DSN"]) as conn:
        active = conn.execute(SQL("SELECT t.active_version_id,v.version_number,v.content FROM {}.prompt_templates t JOIN {}.prompt_template_versions v ON v.id=t.active_version_id").format(Identifier(schema), Identifier(schema))).fetchone()
        assert active == (12, 2, "active")
        queue = conn.execute(SQL("SELECT id,status,payload,created_at FROM {}.queue ORDER BY id").format(Identifier(schema))).fetchall()
        assert queue[0][0:3] == (40, "running", {"nested": {"n": 1.25}, "tags": ["a", "b"]})
        assert queue[1][0:3] == (41, "pending", None)
        assert queue[0][3].astimezone(UTC) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        next_id = conn.execute(SQL("INSERT INTO {}.queue(status,payload,created_at) VALUES('pending',NULL,now()) RETURNING id").format(Identifier(schema))).fetchone()[0]
        assert next_id > 41


def test_small_evidence_cross_source_import_remains_readable_after_sqlite_sources_removed(
    native_import_target, tmp_path
) -> None:
    from packages.common.native_sqlite_import import SourceMapping, TableMapping, import_native_sqlite_source
    from packages.system_observability.postgres_store import PostgresSystemInformationStore

    schema = native_import_target
    content = b'{"kind":"migration-fixture","value":7}'
    evidence_ref = hashlib.sha256(content).hexdigest()
    compressed = gzip.compress(content, mtime=0)
    metadata_path = tmp_path / "system-information.sqlite"
    evidence_path = tmp_path / "small-evidence.sqlite"
    legacy_marker = f"{tmp_path}/evidence/_sqlite/{evidence_ref}"
    with sqlite3.connect(metadata_path) as source:
        source.execute("CREATE TABLE system_information_evidence(evidence_ref TEXT PRIMARY KEY,path TEXT NOT NULL,byte_size INTEGER NOT NULL,created_at_ms INTEGER NOT NULL)")
        source.execute("INSERT INTO system_information_evidence VALUES(?,?,?,?)", (evidence_ref, legacy_marker, len(content), 1770000000000))
    with sqlite3.connect(evidence_path) as source:
        source.execute("CREATE TABLE evidence(ref TEXT PRIMARY KEY,payload BLOB NOT NULL,created_at_ms INTEGER)")
        source.execute("INSERT INTO evidence VALUES(?,?,?)", (evidence_ref, compressed, 1770000000000))

    with connect(os.environ["ODAILY_TEST_POSTGRES_DSN"], autocommit=True) as conn:
        conn.execute(SQL("CREATE TABLE {}.system_information_evidence(evidence_ref text PRIMARY KEY,path text NOT NULL,byte_size bigint NOT NULL,created_at_ms bigint NOT NULL)").format(Identifier(schema)))
        conn.execute(SQL("CREATE TABLE {}.small_evidence(evidence_ref text PRIMARY KEY,compressed_payload bytea NOT NULL,byte_size bigint NOT NULL CHECK(byte_size>=0),created_at timestamptz NOT NULL)").format(Identifier(schema)))

    import_native_sqlite_source(
        source_key="system_information_fixture", source_path=metadata_path,
        source_mapping=SourceMapping("system_information_fixture", schema, (
            TableMapping("system_information_evidence", schema, "system_information_evidence"),
        )), batch_size=1,
    )
    import_native_sqlite_source(
        source_key="small_evidence_fixture", source_path=evidence_path,
        source_mapping=SourceMapping("small_evidence_fixture", schema, (
            TableMapping("evidence", schema, "small_evidence", column_map=(("ref", "evidence_ref"), ("payload", "compressed_payload")), transform="small_evidence"),
        )), batch_size=1,
    )

    metadata_path.unlink()
    evidence_path.unlink()
    reader = PostgresSystemInformationStore(schema=schema)
    assert reader.load_evidence(evidence_ref) == {"kind": "migration-fixture", "value": 7}


def test_system_information_import_preserves_external_gzip_evidence_path(
    native_import_target, tmp_path
) -> None:
    from packages.common.native_sqlite_import import SourceMapping, TableMapping, import_native_sqlite_source
    from packages.system_observability.postgres_store import PostgresSystemInformationStore

    schema = native_import_target
    evidence_dir = tmp_path / "legacy-evidence"
    evidence_dir.mkdir()
    content = json.dumps({"legacy": "保留原有外部 gzip 文件"}, ensure_ascii=False).encode("utf-8")
    evidence_ref = hashlib.sha256(content).hexdigest()
    file_path = evidence_dir / f"{evidence_ref}.json.gz"
    file_path.write_bytes(gzip.compress(content, mtime=0))
    source_path = tmp_path / "system-information-external.sqlite"
    with sqlite3.connect(source_path) as source:
        source.execute("CREATE TABLE system_information_evidence(evidence_ref TEXT PRIMARY KEY,path TEXT NOT NULL,byte_size INTEGER NOT NULL,created_at_ms INTEGER NOT NULL)")
        source.execute("INSERT INTO system_information_evidence VALUES(?,?,?,?)", (evidence_ref, str(file_path), len(content), 1770000000000))
    with connect(os.environ["ODAILY_TEST_POSTGRES_DSN"], autocommit=True) as conn:
        conn.execute(SQL("CREATE TABLE {}.system_information_evidence(evidence_ref text PRIMARY KEY,path text NOT NULL,byte_size bigint NOT NULL,created_at_ms bigint NOT NULL)").format(Identifier(schema)))
    import_native_sqlite_source(
        source_key="system_information_external_fixture", source_path=source_path,
        source_mapping=SourceMapping("system_information_external_fixture", schema, (
            TableMapping("system_information_evidence", schema, "system_information_evidence"),
        )), batch_size=1,
    )
    source_path.unlink()
    reader = PostgresSystemInformationStore(schema=schema)
    assert reader.load_evidence(evidence_ref) == {"legacy": "保留原有外部 gzip 文件"}
