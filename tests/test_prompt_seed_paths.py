from pathlib import Path

from packages.x_processing.repository import PROMPT_SEEDS


def test_prompt_seed_files_exist() -> None:
    missing = [path for _, path, _ in PROMPT_SEEDS.values() if not Path(path).exists()]

    assert missing == []
