from __future__ import annotations

import sqlite3
from pathlib import Path

from packages.investment_leads.analysis import ModelOutput, _normalize_assessment
from packages.investment_leads.service import InvestmentLeadService, _iso


def test_assessment_requires_reason_and_keeps_impact_separate_from_confidence():
    assert _normalize_assessment({"impact": "high", "confidence": "low"})["impact"] == "unknown"
    assessment = _normalize_assessment({
        "impact": "high", "confidence": "low", "expectation_change": "扩建预期下降",
        "market_acceptance": "供电限制影响扩建", "key_uncertainty": "政策权限",
    })
    assert (assessment["impact"], assessment["confidence"]) == ("high", "low")
    assert _normalize_assessment({"impact": [], "confidence": {}, "expectation_change": "a", "market_acceptance": "b"})["impact"] == "unknown"


def test_assessment_survives_revisions_and_filters_include_ungraded_history():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    service = InvestmentLeadService(Path(":memory:"), connection=db)
    now = _iso()
    assessment = _normalize_assessment({"impact": "high", "confidence": "low", "expectation_change": "扩建受限", "market_acceptance": "供电影响扩建"})
    lead = {"subject": "供电风险", "driver_logic": "供电风险影响扩建预期", "assets": [
        {"name": "Example", "ticker": "EX", "assessment": assessment}], "source_claims": []}
    lead_id = service._save_lead(lead, [], ModelOutput([], "example", None, 0, 0, 0), now)
    assert service.list_leads(impact="high")["items"][0]["id"] == lead_id
    assert service.lead_detail(lead_id)["revisions"][0]["assets"][0]["assessment"] == assessment
    old = {"subject": "旧线索", "driver_logic": "旧逻辑", "assets": [{"name": "Old", "ticker": "OLD"}], "source_claims": []}
    old_id = service._save_lead(old, [], ModelOutput([], "example", None, 0, 0, 0), now)
    db.execute("UPDATE investment_lead_revisions SET prompt_version='investment_leads_derive_v4' WHERE lead_id=?", (old_id,))
    assert [item["id"] for item in service.list_leads(impact="unknown")["items"]] == [old_id]
    assert service.list_leads()["total"] == 2
