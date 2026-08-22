from __future__ import annotations

import json

from packages.common.config import DEFAULT_GPT_WRITER_MODEL, XProcessingSettings
from packages.non_mainstream_media.classifier import MixedSourceClassifier, build_mixed_source_classifier


class RecordingTextClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate_text(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return json.dumps({"target": "ai", "reason": "产业主题明确"}, ensure_ascii=False)


def test_mixed_source_classifier_omits_reasoning_by_default() -> None:
    client = RecordingTextClient()
    classifier = MixedSourceClassifier(client=client)

    result = classifier.classify_fulltext(
        site_display_name="Business Insider",
        title="AI chip investment expands",
        content="A semiconductor company expands its AI data-center investment.",
    )

    assert result.target == "ai"
    assert classifier.model == DEFAULT_GPT_WRITER_MODEL
    assert classifier.reasoning_effort is None
    assert client.calls[0]["model"] == DEFAULT_GPT_WRITER_MODEL
    assert client.calls[0]["reasoning_effort"] is None


def test_mixed_source_classifier_uses_writer_model_from_settings() -> None:
    classifier = build_mixed_source_classifier(XProcessingSettings(openai_api_key="test-key"))

    assert classifier.model == DEFAULT_GPT_WRITER_MODEL
    assert classifier.reasoning_effort is None
