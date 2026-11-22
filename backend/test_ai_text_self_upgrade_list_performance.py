from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from io import BytesIO
from types import SimpleNamespace
import gzip
import json

from packages.ai_text_self_upgrade.repository import AITextSelfUpgradeRepository
from test_pg_ai_review_repositories import isolated_postgres


def test_manual_recent_list_filters_before_paging_and_omits_large_snapshots(tmp_path):
    repository = AITextSelfUpgradeRepository(tmp_path / "upgrade.sqlite")
    check_manual_recent_list(repository)


def test_postgres_manual_recent_list_filters_before_paging_and_omits_large_snapshots(isolated_postgres):
    from packages.ai_text_self_upgrade.postgres_repository import PostgresAITextSelfUpgradeRepository

    check_manual_recent_list(PostgresAITextSelfUpgradeRepository())


def check_manual_recent_list(repository):
    now = datetime.now(UTC)
    for index, (source, hours, basis, resolution) in enumerate([
        ("text_auditor", 1, "manual", "reviewer_correct"),
        ("text_auditor", 2, "manual", "writer_correct"),
        ("text_auditor", 3, "manual", None),
        ("text_auditor", 80, "manual", "writer_correct"),
        ("text_auditor", 1, "explicit_non_manual_metadata", "writer_correct"),
        ("fact_auditor", 1, "manual", "writer_correct"),
    ]):
        key = f"case-{index}"
        repository.insert_case({
            "case_key": key, "source_type": source, "auditor_check_id": index + 1,
            "source_item_id": key, "content_hash": key, "audited_title": key,
            "audited_content": "large snapshot " * 10000, "source_content": "large source " * 10000,
            "task_id": None, "published_at": (now - timedelta(hours=hours)).isoformat(),
            "audit_result": {"summary": "summary", "manual_rejection": {"authoring_basis": basis},
                             "issues": [{"original": "large issue " * 10000}]},
        })
        if resolution:
            repository.decide(key, decision=resolution, actor="test")

    first = repository.list_items({"scope": "manual_recent", "status": "confirmed", "limit": 1})
    second = repository.list_items({"scope": "manual_recent", "status": "confirmed", "limit": 1, "offset": 1})
    assert first["total"] == second["total"] == 2
    assert first["confirmed_count"] == 2
    assert {first["items"][0]["case_key"], second["items"][0]["case_key"]} == {"case-0", "case-1"}
    assert first["items"][0]["audit_result"]["summary"] == "summary"
    assert "audited_content" not in first["items"][0]
    assert "issues" not in first["items"][0]["audit_result"]
    assert len(json.dumps(first["items"])) < 3000
    assert repository.list_items({"scope": "manual_recent", "status": "pending"})["total"] == 1
    assert repository.list_items({"scope": "manual_recent", "status": "all"})["total"] == 3
    assert repository.list_items({"status": "all"})["total"] == 6
    assert repository.detail("case-0")["audited_content"].startswith("large snapshot")


def test_self_upgrade_detail_compresses_without_changing_payload():
    from packages.editor_plugin_api import EditorPluginApiHandler

    class Handler:
        path = "/console/ai-text-self-upgrade"
        headers = {"Accept-Encoding": "gzip"}
        wfile = BytesIO()
        sent = {}
        server = SimpleNamespace(service=SimpleNamespace(api_settings=SimpleNamespace(cors_allow_origin="*")))
        def send_response(self, status): pass
        def send_header(self, name, value): self.sent[name] = value
        def end_headers(self): pass
        def _json_default(self, value): return str(value)
        def _send_common_headers(self): EditorPluginApiHandler._send_common_headers(self)

    handler = Handler()
    payload = {"item": {"audited_content": "案例正文" * 10000}}
    EditorPluginApiHandler._send_json(handler, HTTPStatus.OK, payload)
    assert handler.sent["Content-Encoding"] == "gzip"
    assert len(handler.wfile.getvalue()) < 2000
    assert json.loads(gzip.decompress(handler.wfile.getvalue())) == payload
