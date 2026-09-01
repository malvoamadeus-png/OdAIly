from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from packages.meme_scanner import fast_narrative, narrative_v2


ADDRESS = "0x" + "a" * 40


def args(tmp_path):
    return SimpleNamespace(
        chain="robinhood",
        contract=ADDRESS,
        symbol="PICKLES",
        output=str(tmp_path / "narrative.json"),
        output_dir=str(tmp_path),
        gpt_model="gpt-5.6-luna",
        gpt_timeout=10,
        writer_base_url="https://writer.invalid/v1",
        writer_api_key="test-key",
    )


def bundle(evidence):
    return {
        "version": fast_narrative.INTERFACE_VERSION,
        "status": "success" if evidence else "empty",
        "evidence": evidence,
        "diagnostics": {"errors": {}, "performance": {}},
    }


def test_fast_narrative_uses_luna_result_and_anonymizes_fomo(tmp_path):
    provider = fast_narrative.InMemoryFastEvidenceAdapter(bundle([{
        "id": "thesis:1",
        "source": "fomo_thesis",
        "statement": "Pickles is a Robinhood Agent",
        "observedAt": "2026-09-01T00:00:00Z",
        "metadata": {"author": "must-not-leak"},
    }]))
    writer = {
        "primary_type": "app_linked",
        "angle_material_ids": ["thesis:1"],
        "reader_text": "某信源表示，PICKLES 是 Robinhood Agent。",
        "used_material_ids": ["thesis:1"],
        "discarded_material_ids": [],
    }
    with patch.object(fast_narrative, "write_json_with_metrics", return_value=(writer, {})) as call:
        result = fast_narrative.run(args(tmp_path), provider=provider)

    assert call.call_args.kwargs["model"] == "gpt-5.6-luna"
    assert result["status"] == "success"
    assert "某信源表示" in result["reader_text"]
    assert "FOMO" not in result["reader_text"]
    assert result["grok_research"] == {}
    assert result["gmgn_supplement"] == []


def test_fast_narrative_rejects_named_fomo_attribution(tmp_path):
    provider = fast_narrative.InMemoryFastEvidenceAdapter(bundle([{
        "id": "thesis:1",
        "source": "fomo_thesis",
        "statement": "Pickles is a Robinhood Agent",
    }]))
    writer = {
        "primary_type": "app_linked",
        "angle_material_ids": ["thesis:1"],
        "reader_text": "FOMO Thesis 用户表示 PICKLES 是 Robinhood Agent。",
        "used_material_ids": ["thesis:1"],
    }
    with patch.object(fast_narrative, "write_json_with_metrics", return_value=(writer, {})):
        with pytest.raises(narrative_v2.NarrativeStageError) as error:
            fast_narrative.run(args(tmp_path), provider=provider)
    assert error.value.stage == "final_validation"


def test_fast_narrative_skips_writer_when_all_sources_are_empty(tmp_path):
    provider = fast_narrative.InMemoryFastEvidenceAdapter(bundle([]))
    with patch.object(fast_narrative, "write_json_with_metrics") as writer:
        result = fast_narrative.run(args(tmp_path), provider=provider)
    writer.assert_not_called()
    assert result["status"] == "empty"
    assert result["decision_code"] == "no_materials"
