from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.editor_plugin_api import (
    EditorPluginRequestModel,
    EditorPluginUnauthorizedError,
    format_validation_error,
    parse_bearer_token,
)


def test_parse_bearer_token_accepts_valid_header() -> None:
    assert parse_bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"


@pytest.mark.parametrize("value", ["", "Token abc", "Bearer   "])
def test_parse_bearer_token_rejects_invalid_header(value: str) -> None:
    with pytest.raises(EditorPluginUnauthorizedError):
        parse_bearer_token(value)


def test_editor_plugin_request_model_normalizes_payload() -> None:
    model = EditorPluginRequestModel.model_validate(
        {
            "source_type": " x_post ",
            "platform": " X ",
            "post_text": "  hello world  ",
            "author_display_name": "  Alice  ",
            "author_handle": "  @alice  ",
        }
    )
    assert model.source_type == "x_post"
    assert model.platform == "x"
    assert model.post_text == "hello world"
    assert model.author_display_name == "Alice"
    assert model.author_handle == "@alice"


def test_editor_plugin_request_model_requires_post_text() -> None:
    with pytest.raises(ValidationError):
        EditorPluginRequestModel.model_validate(
            {
                "source_type": "x_post",
                "platform": "x",
                "post_text": "   ",
            }
        )


def test_format_validation_error_returns_first_field_message() -> None:
    with pytest.raises(ValidationError) as excinfo:
        EditorPluginRequestModel.model_validate(
            {
                "source_type": "x_post",
                "platform": "x",
                "post_text": "   ",
            }
        )
    assert format_validation_error(excinfo.value) == "post_text: Value error, post_text is required"
