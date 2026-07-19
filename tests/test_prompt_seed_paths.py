from pathlib import Path

import pytest

from packages.x_processing.models import PromptTemplateVersion, TaskRecord
from packages.x_processing.repository import PROMPT_SEEDS
from packages.x_processing.worker import build_structured_writer_prompt, build_writer_prompt


def test_prompt_seed_files_exist() -> None:
    missing = [path for _, path, _ in PROMPT_SEEDS.values() if not Path(path).exists()]

    assert missing == []


@pytest.mark.parametrize("template_key", ["x_regular_writer", "x_onchain_writer", "x_funding_writer"])
def test_writer_prompt_seeds_do_not_teach_field_label_wrappers(template_key: str) -> None:
    _, path, _ = PROMPT_SEEDS[template_key]
    content = Path(path).read_text(encoding="utf-8")

    assert "标题为：" not in content
    assert "正文为：" not in content


def test_structured_writer_prompt_uses_json_field_boundaries_only() -> None:
    task = TaskRecord(
        id=1,
        source="x",
        source_item_id="post-1",
        source_url="https://x.com/example/status/1",
        title="Example",
        content="Ostium published an incident update.",
        metadata={"effective_author_name": "Ostium"},
    )
    prompt = PromptTemplateVersion(
        id=1,
        template_key="x_onchain_writer",
        version_number=1,
        content="Write a concise Chinese newsflash.",
    )

    structured = build_structured_writer_prompt(task=task, prompt=prompt, known_subjects=[])
    plain = build_writer_prompt(task=task, prompt=prompt)

    assert "请严格输出一行标题、空一行、正文" not in structured
    assert "JSON 的 title 字段只放标题文本" in structured
    assert "JSON 的 content 字段只放正文文本" in structured
    assert "请严格输出一行标题、空一行、正文" in plain
