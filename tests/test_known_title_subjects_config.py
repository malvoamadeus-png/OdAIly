from __future__ import annotations

import json

from packages.x_processing.known_title_subjects_config import (
    load_known_title_subject_names,
    save_known_title_subject_names,
)


def test_known_title_subjects_default_to_seed_when_file_missing(tmp_path) -> None:
    assert load_known_title_subject_names(path=tmp_path / "missing.json") == ["Rune", "Cobie", "Vitalik", "CZ"]


def test_known_title_subjects_save_normalizes_local_config(tmp_path) -> None:
    path = tmp_path / "known_title_subjects.json"

    saved = save_known_title_subject_names("CZ、Vitalik\nCZ, 赵长鹏", updated_by="admin@example.com", path=path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert saved.names == ["CZ", "Vitalik", "赵长鹏"]
    assert payload["names"] == ["CZ", "Vitalik", "赵长鹏"]
    assert payload["updated_by"] == "admin@example.com"
    assert load_known_title_subject_names(path=path) == ["CZ", "Vitalik", "赵长鹏"]
