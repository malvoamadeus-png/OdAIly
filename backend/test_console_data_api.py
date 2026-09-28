import json
import sqlite3

from packages.console_data_api import ConsoleDataApi


def test_console_data_hydrates_legacy_duplicate_target_from_local_archive(tmp_path) -> None:
    database_path = tmp_path / "odaily.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY, source TEXT, source_item_id TEXT, source_url TEXT,
                title TEXT, content TEXT, status TEXT, created_at TEXT, metadata TEXT
            );
            CREATE TABLE x_task_pipeline (
                task_id INTEGER PRIMARY KEY, search_result TEXT
            );
            CREATE TABLE odaily_reference_items (
                source_item_id TEXT PRIMARY KEY, title TEXT, source_url TEXT
            );
            """,
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "non_mainstream_media", "source-1", "https://example.test/source-1", "输入", "正文", "duplicate", "2026-09-27 14:00:00", "{}"),
        )
        connection.execute(
            "INSERT INTO x_task_pipeline VALUES (?, ?)",
            (1, json.dumps({"is_duplicate": True, "duplicate_target_type": "odaily_published", "duplicate_target_id": "odaily-9"})),
        )
        connection.execute(
            "INSERT INTO odaily_reference_items VALUES (?, ?, ?)",
            ("odaily-9", "已发布的重复快讯", "https://www.odaily.news/news/odaily-9"),
        )
        connection.commit()

    result = ConsoleDataApi(database_path).execute(
        {
            "table": "tasks",
            "operation": "select",
            "select": "id,source,source_item_id,source_url,title,content,status,created_at,metadata,x_task_pipeline(search_result)",
        }
    )

    assert result[0]["x_task_pipeline"]["search_result"]["duplicate_target"] == {
        "target_type": "odaily_published",
        "target_id": "odaily-9",
        "title": "已发布的重复快讯",
        "source_url": "https://www.odaily.news/news/odaily-9",
    }
