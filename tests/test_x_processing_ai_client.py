from __future__ import annotations

import pytest

from packages.x_processing.ai_client import extract_response_text


def test_extract_response_text_skips_response_items_with_null_content() -> None:
    payload = {
        "output_text": None,
        "output": [
            {"type": "reasoning", "content": None},
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "标题\n\n正文"},
                ],
            },
        ],
    }

    assert extract_response_text(payload) == "标题\n\n正文"


def test_extract_response_text_raises_when_no_text_can_be_extracted() -> None:
    payload = {
        "output_text": None,
        "output": [
            {"type": "reasoning", "content": None},
        ],
    }

    with pytest.raises(ValueError, match="did not contain output_text"):
        extract_response_text(payload)
