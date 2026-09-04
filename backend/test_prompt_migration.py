from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from src.main import parse_args, x_process_publish_prompt_version_command
from packages.x_processing.prompt_rules import X_ARTICLE_WRITER_RULES
from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository


def test_article_prompt_migration_preserves_active_content_and_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "odaily.sqlite"
    repository = SQLiteXProcessingRepository(database_path)
    repository.seed_prompt_templates(root_dir=Path(__file__).resolve().parents[1])

    seeded = repository.get_active_prompt("x_regular_writer")
    legacy_content = seeded.content.replace(f"\n\n{X_ARTICLE_WRITER_RULES}", "")
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE prompt_template_versions SET content=? WHERE id=?",
            (legacy_content, seeded.id),
        )
        conn.commit()
    before = repository.get_active_prompt("x_regular_writer")
    migrated, changed = repository.append_prompt_version(
        template_key="x_regular_writer",
        appendix=X_ARTICLE_WRITER_RULES,
        note="append X Article compact editing rules",
    )

    assert changed is True
    assert migrated.id != before.id
    assert migrated.version_number == before.version_number + 1
    assert migrated.content.startswith(before.content)
    assert X_ARTICLE_WRITER_RULES in migrated.content
    assert repository.get_active_prompt("x_regular_writer").id == migrated.id

    with sqlite3.connect(database_path) as conn:
        versions = conn.execute(
            "SELECT id, content FROM prompt_template_versions WHERE template_key=? ORDER BY version_number",
            ("x_regular_writer",),
        ).fetchall()
    assert [row[0] for row in versions] == [before.id, migrated.id]
    assert versions[0][1] == before.content

    repeated, repeated_changed = repository.append_prompt_version(
        template_key="x_regular_writer",
        appendix=X_ARTICLE_WRITER_RULES,
        note="append X Article compact editing rules",
    )

    assert repeated_changed is False
    assert repeated.id == migrated.id
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM prompt_template_versions WHERE template_key=?",
            ("x_regular_writer",),
        ).fetchone()[0] == 2


def test_prompt_migration_cli_selects_the_supported_template(monkeypatch, tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "odaily.sqlite"
    repository = SQLiteXProcessingRepository(database_path)
    repository.seed_prompt_templates(root_dir=Path(__file__).resolve().parents[1])
    seeded = repository.get_active_prompt("x_regular_writer")
    legacy_content = seeded.content.replace(f"\n\n{X_ARTICLE_WRITER_RULES}", "")
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE prompt_template_versions SET content=? WHERE id=?",
            (legacy_content, seeded.id),
        )
        conn.commit()

    monkeypatch.setenv("ODAILY_SQLITE_PATH", str(database_path))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "main.py",
            "x-process-publish-prompt-version",
            "--template-key",
            "x_regular_writer",
        ],
    )

    args = parse_args()
    assert args.template_key == "x_regular_writer"
    assert x_process_publish_prompt_version_command(args) == 0
    assert "changed=true" in capsys.readouterr().out
    assert X_ARTICLE_WRITER_RULES in repository.get_active_prompt("x_regular_writer").content
