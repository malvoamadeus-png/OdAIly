from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from packages.business_news.repository import BusinessNewsRepository
from packages.business_news.storage import business_news_path, migrate_business_news
from packages.system_observability.projector import MainTaskProjector, TableSpec, table_specs_for_business_path
from packages.system_observability.store import SystemInformationStore


def seed(source: Path) -> BusinessNewsRepository:
    repo = BusinessNewsRepository(source)
    update = {"update_id": 7, "message": {"message_id": 8, "date": 1793275200, "chat": {"id": -100123, "type": "supergroup", "title": "fixture"}, "from": {"id": 99}, "text": "一条足够长的虚构商务消息用于测试数据库迁移"}}
    repo.ingest_update(update, source_urls=[], own_bot_id="1", bot_id="fixture")
    repo.save_group(chat_id="-100123", customer_name="fixture", enabled=True)
    update["update_id"] = 9
    update["message"]["message_id"] = 10
    repo.ingest_update(update, source_urls=[], own_bot_id="1", bot_id="fixture")
    return repo


def test_migration_preserves_inbox_cursor_and_isolates_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "main.sqlite"
    destination = tmp_path / "business.sqlite"
    before = seed(source)
    expected = before.list_dashboard({})
    result = migrate_business_news(source, destination, None)
    assert result["business_message_inbox"] == 1
    monkeypatch.setenv("ODAILY_BUSINESS_NEWS_SQLITE_PATH", str(destination))
    assert business_news_path() == destination
    after = BusinessNewsRepository(destination)
    assert after.list_dashboard({}) == expected
    assert after.last_update_id("fixture") == 9
    with sqlite3.connect(source) as main:
        main.execute("BEGIN IMMEDIATE")
        claimed = after.claim_pending(1)
        assert len(claimed) == 1
        main.rollback()
    with sqlite3.connect(destination) as business:
        assert business.execute("SELECT COUNT(*) FROM odaily_observability_outbox WHERE table_name='business_message_inbox' AND operation='update'").fetchone()[0] == 1
    assert before.list_dashboard({}) == expected
    with pytest.raises(ValueError, match="Destination"):
        migrate_business_news(source, destination, None)


def test_migration_refuses_inflight_and_unverified_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "main.sqlite"
    destination = tmp_path / "business.sqlite"
    repo = seed(source)
    assert len(repo.claim_pending(1)) == 1
    with pytest.raises(RuntimeError, match="in-flight"):
        migrate_business_news(source, destination, None)
    assert not destination.exists()
    sqlite3.connect(destination).close()
    monkeypatch.setenv("ODAILY_BUSINESS_NEWS_SQLITE_PATH", str(destination))
    with pytest.raises(RuntimeError, match="verified migration"):
        business_news_path()


def test_migrated_database_missing_a_business_table_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "main.sqlite"
    destination = tmp_path / "business.sqlite"
    seed(source)
    migrate_business_news(source, destination, None)
    with sqlite3.connect(destination) as conn:
        conn.execute("DROP TABLE business_telegram_state")
    monkeypatch.setenv("ODAILY_BUSINESS_NEWS_SQLITE_PATH", str(destination))
    with pytest.raises(RuntimeError, match="verified migration"):
        business_news_path()


def test_migration_requires_observability_caught_up(tmp_path: Path) -> None:
    source = tmp_path / "main.sqlite"
    destination = tmp_path / "business.sqlite"
    seed(source)
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE odaily_observability_outbox(seq INTEGER PRIMARY KEY,table_name TEXT)")
        conn.execute("INSERT INTO odaily_observability_outbox VALUES(3,'business_message_inbox')")
    obs_path = tmp_path / "observability.sqlite"
    obs = SystemInformationStore(obs_path)
    obs.initialize()
    obs.set_state("source_audit_main_watermark", "2")
    with pytest.raises(RuntimeError, match="not yet projected"):
        migrate_business_news(source, destination, obs_path)
    assert not destination.exists()
    obs.set_state("source_audit_main_watermark", "3")
    assert migrate_business_news(source, destination, obs_path)["business_message_inbox"] == 1


def test_business_projection_keeps_existing_item_identity(tmp_path: Path) -> None:
    source = tmp_path / "main.sqlite"
    destination = tmp_path / "business.sqlite"
    seed(source)
    migrate_business_news(source, destination, None)
    obs = SystemInformationStore(tmp_path / "observability.sqlite")
    obs.initialize()
    main_spec = TableSpec("main", "business_message_inbox", "business_news", "business_telegram")
    migrated_spec = next(spec for spec in table_specs_for_business_path(separate=True) if spec.table == "business_message_inbox")
    old = MainTaskProjector(source, obs, additional_databases={"main": source}, table_specs=(main_spec,))
    old._project_additional(1_794_000_000_000, 0)
    with obs.connect() as conn:
        before = [tuple(row) for row in conn.execute("SELECT item_id,source_database FROM system_information_items")]
    assert len(before) == 1
    assert len(BusinessNewsRepository(destination).claim_pending(1)) == 1
    new = MainTaskProjector(source, obs, additional_databases={"business": destination}, table_specs=(migrated_spec,))
    new._project_additional(1_794_000_016_000, 0)
    with obs.connect() as conn:
        after = [tuple(row) for row in conn.execute("SELECT item_id,source_database FROM system_information_items")]
        status_events = conn.execute("SELECT COUNT(*) FROM system_information_events WHERE item_id=? AND status='processing'", (before[0][0],)).fetchone()[0]
    assert len(after) == 1
    assert after[0][0] == before[0][0]
    assert after[0][1] == str(destination)
    assert status_events >= 1


def test_migration_preserves_autoincrement_and_rejects_unknown_fields(tmp_path: Path) -> None:
    source = tmp_path / "main.sqlite"
    destination = tmp_path / "business.sqlite"
    seed(source)
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='business_message_inbox'")
        conn.execute("ALTER TABLE business_telegram_groups ADD COLUMN unexpected TEXT")
    with pytest.raises(RuntimeError, match="Unexpected schema"):
        migrate_business_news(source, destination, None)
    assert not destination.exists()
    with sqlite3.connect(source) as conn:
        conn.execute("ALTER TABLE business_telegram_groups DROP COLUMN unexpected")
    migrate_business_news(source, destination, None)
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='business_message_inbox'").fetchone()[0] == 100


def test_migration_refuses_unrecognized_business_triggers(tmp_path: Path) -> None:
    source = tmp_path / "main.sqlite"
    destination = tmp_path / "business.sqlite"
    seed(source)
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TRIGGER custom_business_trigger AFTER INSERT ON business_telegram_groups BEGIN SELECT 1; END")
    with pytest.raises(RuntimeError, match="Unknown business news trigger"):
        migrate_business_news(source, destination, None)
    assert not destination.exists()
