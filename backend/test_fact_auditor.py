from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from packages.common.storage import connect_sqlite
from packages.fact_auditor.ai import FactReviewAI
from packages.fact_auditor.engine import FactReviewEngine
from packages.fact_auditor.original import validate_public_url
from packages.fact_auditor.repository import FactReviewRepository
from packages.fact_auditor.settings import FactAuditorSettings
from packages.fact_auditor.worker import FactAuditorWorker
from packages.newsflash_operations import NewsflashOperationsRepository
from packages.x_processing.sqlite_repository import SQLITE_SCHEMA_SQL


class FakeAI:
    def __init__(self, gate=None, comparison=None, external=None):
        self.reviews = [gate or {"needs_review": True, "reason": "重大减持影响交易", "questions": []},
                        comparison or {"status": "passed", "summary": "与原文一致", "issues": [], "questions": []}]
        self.external = external
        self.search_calls = 0

    def review(self, prompt):
        return {"result": self.reviews.pop(0), "model": "gpt-5.6-luna"}

    def search(self, prompt):
        self.search_calls += 1
        if isinstance(self.external, Exception):
            raise self.external
        return self.external


CONTENT = "Vlad 卖出90%股票"
ORIGINAL = "Vlad sold 90% of Class A shares, retaining his Class B shares."
QUESTION = {"claim": CONTENT, "question": "90%的分母及股票类别是什么？", "kind": "missing_scope",
            "source_evidence": "90%", "reason": "重大减持未明确分母，可能把某类股写成全部权益"}


def review(ai, *, original=ORIGINAL, content=CONTENT):
    return FactReviewEngine(ai).review(title="", content=content,
        original_loader=lambda: {"status": "available" if original else "unavailable", "text": original, "url": "", "reason": "原文不可用"})


def test_low_value_copy_skips_original_and_search():
    ai = FakeAI(gate={"needs_review": False, "reason": "一般市场展望", "questions": []})
    result = FactReviewEngine(ai).review(title="", content="AI交易面临财报考验",
        original_loader=lambda: pytest.fail("Skipped stories must not fetch original"))
    assert result["status"] == "skipped"
    assert ai.search_calls == 0


def test_omitted_share_class_is_reported_without_search():
    ai = FakeAI(comparison={"status": "flagged", "summary": "省略类别扩大减持范围",
        "issues": [{"original": CONTENT, "evidence": "Class A shares", "reason": "让读者误以为卖掉全部持股的90%"}], "questions": []})
    result = review(ai)
    assert result["status"] == "flagged"
    assert ai.search_calls == 0


def test_unrelated_original_is_not_treated_as_proof_news_is_false():
    ai = FakeAI(comparison={"status": "source_mismatch", "summary": "原文讲Multicoin，稿件讲Jupiter",
        "issues": [{"original": "Jupiter基金亏损", "evidence": "Multicoin invests in crypto", "reason": "原始材料与稿件不是同一事件"}], "questions": []})
    result = review(ai, original="Multicoin invests in crypto", content="Jupiter基金亏损")
    assert result["status"] == "flagged"
    assert result["stages"]["comparison"]["result"]["status"] == "source_mismatch"


def test_external_requires_tool_provenance_for_evidence():
    gate = {"needs_review": True, "reason": "减持口径不清", "questions": [QUESTION]}
    finding = {**QUESTION, "verdict": "supported", "reason": "证实", "evidence": [{"url": "https://invented.example/report", "quote": "90%"}]}
    ai = FakeAI(gate=gate, comparison={"status": "passed", "summary": "待确认减持分母", "issues": [], "questions": [QUESTION]}, external={"result": {"summary": "已查", "findings": [finding]}, "sources": ["https://actual.example/report"]})
    result = review(ai)
    assert result["status"] == "unverifiable"
    assert result["stages"]["external"]["result"]["findings"][0]["verdict"] == "unverified"


def test_comparison_can_identify_material_scope_even_when_copy_matches_original():
    gate = {"needs_review": True, "reason": "重大减持口径缺失", "questions": [QUESTION]}
    finding = {**QUESTION, "verdict": "contradicted", "reason": "90%仅为A类股，并非全部权益",
               "evidence": [{"url": "https://issuer.example/filing", "quote": "retained Class B shares"}]}
    ai = FakeAI(gate=gate, comparison={"status": "passed", "summary": "待确认减持分母", "issues": [], "questions": [QUESTION]}, external={"result": {"summary": "减持范围有误", "findings": [finding]}, "sources": ["https://issuer.example/filing"]})
    result = review(ai, original=CONTENT)
    assert result["status"] == "flagged"
    assert ai.search_calls == 1


def test_missing_original_is_not_a_pass():
    assert review(FakeAI(), original="")["status"] == "unverifiable"


def test_search_failure_is_failure_not_pass():
    result = review(FakeAI(comparison={"status": "passed", "summary": "待确认减持分母", "issues": [], "questions": [QUESTION]}, external=TimeoutError("search timeout")))
    assert result["status"] == "failed"
    assert result["stages"]["comparison"]["result"]["status"] == "passed"


def test_known_source_issue_survives_search_failure():
    compared = {"status": "flagged", "summary": "省略限定", "issues": [{"original": CONTENT, "evidence": "Class A", "reason": "减持范围被放大"}], "questions": [QUESTION]}
    result = review(FakeAI(comparison=compared, external=TimeoutError("search timeout")))
    assert result["status"] == "flagged"
    assert result["incomplete"] is True


def test_fabricated_comparison_quote_does_not_become_a_reported_error():
    compared = {"status": "flagged", "summary": "错误", "issues": [{"original": CONTENT, "evidence": "not in source", "reason": "错误"}], "questions": []}
    assert review(FakeAI(comparison=compared))["status"] == "failed"


def test_screening_questions_do_not_survive_a_clean_comparison():
    ai = FakeAI(gate={"needs_review": True, "reason": "值得比对", "questions": [QUESTION]})
    result = review(ai)
    assert result["status"] == "passed"
    assert result["questions"] == []
    assert ai.search_calls == 0


def test_missing_original_does_not_launch_speculative_search():
    ai = FakeAI(gate={"needs_review": True, "reason": "值得比对", "questions": [QUESTION]})
    result = review(ai, original="")
    assert result["status"] == "unverifiable"
    assert result["questions"] == []
    assert ai.search_calls == 0


def test_unrelated_original_cannot_spawn_external_questions():
    ai = FakeAI(comparison={"status": "source_mismatch", "summary": "材料不对应",
        "issues": [{"original": CONTENT, "evidence": "Other story", "reason": "材料不对应"}],
        "questions": [QUESTION]})
    assert review(ai, original="Other story")["status"] == "flagged"
    assert ai.search_calls == 0


@pytest.mark.parametrize("overrides", [{"source_evidence": "fabricated quote"}, {"source_evidence": ""}, {"reason": ""}])
def test_search_question_requires_source_anchor_and_material_reason(overrides):
    ai = FakeAI(comparison={"status": "passed", "summary": "待核实", "issues": [],
        "questions": [{**QUESTION, **overrides}]})
    assert review(ai)["status"] == "failed"
    assert ai.search_calls == 0


def test_unverified_negative_entity_translation_is_still_searched():
    content = "鸣石投资关联基金亏损43%"
    question = {"claim": "鸣石投资", "question": "Shanghai Minghong Investment Co.对应鸣石投资吗？",
        "kind": "name_mapping", "source_evidence": "Shanghai Minghong Investment Co.",
        "reason": "中文名为编辑补充，负面事件可能认错机构"}
    ai = FakeAI(comparison={"status": "passed", "summary": "名称转换待核实", "issues": [], "questions": [question]},
        external={"result": {"summary": "认错机构", "findings": [{**question, "verdict": "contradicted",
            "reason": "名称对应错误", "evidence": [{"url": "https://issuer.example/about", "quote": "Shanghai Minghong"}]}]},
            "sources": ["https://issuer.example/about"]})
    result = review(ai, content=content, original="Shanghai Minghong Investment Co. linked fund lost 43%")
    assert result["status"] == "flagged"
    assert ai.search_calls == 1


@pytest.fixture
def repository(tmp_path):
    path = tmp_path / "test.sqlite"
    with connect_sqlite(path) as conn:
        conn.executescript(SQLITE_SCHEMA_SQL)
        conn.commit()
    return FactReviewRepository(path)


def add_reference(repository, *, title="减持", content=CONTENT, raw_payload="{}"):
    with connect_sqlite(repository.path) as conn:
        conn.execute("INSERT INTO odaily_reference_items(source_item_id,title,content,published_at,raw_payload) VALUES(?,?,?,?,?)",
            ("123", title, content, datetime.now(UTC).astimezone().isoformat(), raw_payload))
        conn.commit()


def claim(repository, worker="one", attempts=3):
    return repository.claim(worker_id=worker, lookback_minutes=120, max_attempts=attempts)


def test_claim_lease_dedup_and_content_change_invalidate_old_result(repository):
    add_reference(repository)
    first = claim(repository)
    assert first is not None
    assert claim(repository, "two") is None
    repository.save(first, {"status": "passed", "summary": "通过"}, worker_id="one")
    assert claim(repository) is None
    with connect_sqlite(repository.path) as conn:
        conn.execute("UPDATE odaily_reference_items SET content='改稿' WHERE source_item_id='123'")
        conn.commit()
    operations = NewsflashOperationsRepository(repository.path)
    current = operations.list_newsflashes({})["items"][0]["fact_review"]
    assert current["status"] == "unreviewed"
    assert current["id"] is None
    second = claim(repository)
    assert second["id"] != first["id"]
    old = repository.detail("123", first["id"])
    assert old["is_current"] is False
    assert old["check"]["content"] == CONTENT


def test_policy_change_creates_new_review_and_retains_old_history(repository, monkeypatch):
    from packages.fact_auditor import repository as module
    add_reference(repository)
    old = claim(repository)
    repository.save(old, {"status": "flagged", "summary": "旧策略"}, worker_id="one")
    monkeypatch.setattr(module, "POLICY_VERSION", "test-narrower-policy")
    assert repository.detail("123")["check"] is None
    assert repository.detail("123", old["id"])["is_current"] is False
    new = claim(repository)
    assert new["id"] != old["id"]
    assert new["policy_version"] == "test-narrower-policy"
    assert len(repository.detail("123")["history"]) == 2


def test_source_link_change_invalidates_old_review(repository):
    add_reference(repository, raw_payload=json.dumps({"sourceUrl": "https://first.example/report"}))
    first = claim(repository)
    repository.save(first, {"status": "passed"}, worker_id="one")
    with connect_sqlite(repository.path) as conn:
        conn.execute("UPDATE odaily_reference_items SET raw_payload=?", (json.dumps({"sourceUrl": "https://second.example/report"}),))
        conn.commit()
    assert repository.detail("123")["check"] is None
    assert claim(repository)["id"] != first["id"]


def test_exhausted_lease_is_failed_not_permanently_processing(repository):
    add_reference(repository)
    task = claim(repository, attempts=1)
    with connect_sqlite(repository.path) as conn:
        conn.execute("UPDATE fact_checks SET locked_until=? WHERE id=?", ((datetime.now(UTC)-timedelta(hours=1)).isoformat(), task["id"]))
        conn.commit()
    assert claim(repository, attempts=1) is None
    assert repository.detail("123")["check"]["status"] == "failed"


def test_overview_filter_and_counts_include_pending_and_completed(repository):
    add_reference(repository)
    operations = NewsflashOperationsRepository(repository.path)
    assert operations.list_newsflashes({"fact_status": "unreviewed"})["total"] == 1
    task = claim(repository)
    repository.save(task, {"status": "flagged", "summary": "范围有误"}, worker_id="one")
    result = operations.list_newsflashes({"fact_status": "flagged"})
    assert result["total"] == 1
    assert result["fact_review_counts"] == {"flagged": 1}
    assert result["items"][0]["fact_review"]["id"] == task["id"]
    assert operations.list_newsflashes({"fact_status": "pending"})["total"] == 0
    detail = operations.execute("fact_review", {"source_item_id": "123"}, actor_email="editor@example.com")
    assert detail["check"]["summary"] == "范围有误"


def test_manual_retry_preserves_execution_records_and_rejects_obsolete_copy(repository):
    add_reference(repository)
    first = claim(repository)
    repository.save(first, {"status": "failed", "summary": "模型通道不可用"}, worker_id="one")
    assert repository.retry(first["id"])["status"] == "pending"
    detail = repository.detail("123")
    assert detail["check"]["attempts"] == 0
    assert detail["runs"][0]["summary"] == "模型通道不可用"
    with connect_sqlite(repository.path) as conn:
        conn.execute("UPDATE odaily_reference_items SET content='已改稿'")
        conn.commit()
    with pytest.raises(ValueError, match="当前稿件"):
        repository.retry(first["id"])


def test_worker_reuses_exclusion_scope_and_match_target(repository):
    add_reference(repository, title="Bitget活动", content="商务稿")
    worker = FactAuditorWorker(repository, FactAuditorSettings(), ai=Mock())
    with connect_sqlite(repository.path) as conn:
        conn.execute("INSERT INTO source_exclusion_rule_groups(rule_key,name,scopes,terms,match_target) VALUES(?,?,?,?,?)",
            ("brand", "商务品牌", '["competitor"]', '["Bitget"]', "title"))
        conn.commit()
    result = worker.run_once()
    assert result["status"] == "exempt"
    assert result["exclusion_rules"][0]["rule_key"] == "brand"
    worker.engine.ai.review.assert_not_called()


def test_worker_deterministic_quote_skips_models(repository):
    add_reference(repository, title="美元兑日元行情", content="Odaily星球日报讯 据 Gate 数据，美元兑日元上涨。")
    ai = Mock()
    result = FactAuditorWorker(repository, FactAuditorSettings(), ai=ai).run_once()
    assert result["status"] == "exempt"
    ai.review.assert_not_called()


def test_original_fetch_rejects_private_network():
    with patch("packages.fact_auditor.original.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
        with pytest.raises(ValueError, match="public"):
            validate_public_url("https://internal.example/report")


def test_search_adapter_rejects_plain_model_answer_without_search():
    ai = FactReviewAI(FactAuditorSettings(api_key="test"))
    response = Mock()
    response.headers = {"Content-Type": "application/json"}
    response.json.return_value = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}]}
    response.text = json.dumps(response.json.return_value)
    with patch("packages.fact_auditor.ai.requests.post", return_value=response):
        with pytest.raises(ValueError, match="completed web_search_call"):
            ai.search("test")
