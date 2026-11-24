from __future__ import annotations

import json

from packages.common.storage import connect_sqlite
from packages.hyperliquid_news.copy import format_news
from packages.hyperliquid_news.naming import NamingStore, NamingWorker, REVIEW_MS, build_profile, parse_result
from packages.hyperliquid_news.store import NewsStore
from packages.x_processing.models import DraftBrief

A = "0x1234567890abcdef1234567890abcdef12345678"
B = "0xabcd567890abcdef1234567890abcdef12345678"
T = 1_800_000_000_000


def setup(tmp_path):
    path = tmp_path / "naming.sqlite"
    store = NewsStore(path)
    store.initialize()
    store.discover(A, source={"kind": "test"}, now_ms=T, limit=None)
    store.discover(B, source={"kind": "test"}, now_ms=T, limit=None)
    return store, NamingStore(path)


def test_legacy_manual_note_migrates_locked_and_empty_edit_wins_inflight(tmp_path):
    store, names = setup(tmp_path)
    store.save_global("address_note:" + A, {"address": A, "note": "Wintermute做市"}, [])
    names.initialize()
    assert names.get(A)["manual_locked"] is True
    assert names.get(A)["current_name"] == "Wintermute做市"
    claim = names.claim_due(now_ms=T, limit=2)
    assert [row["address"] for row in claim] == [B]
    names.set_manual(B, "", updated_by="editor@example.com")
    result = {"name": "0xabcd优质交易者", "kind": "stable", "reason": "证据", "selected_dimension": "d", "evidence_refs": ["d"],
              "coverage": {"dimensions": [{"evidence_id": "d", "available": True}]}}
    assert names.save_result(B, token=claim[0]["claim_token"], result=result, now_ms=T, model="gpt-6-luna") is False
    assert names.get(B)["manual_locked"] is True
    assert names.get(B)["current_name"] is None


def test_due_worker_reviews_future_addresses_and_uses_safe_prefix_without_state(tmp_path):
    store, names = setup(tmp_path)
    report = NamingWorker(store, names, batch_size=4).run_once(now_ms=T)
    assert report["saved"] == 2
    assert names.get(A)["current_name"] == "普通地址0x12"
    assert names.get(B)["current_name"] == "普通地址0xab"
    assert names.claim_due(now_ms=T + REVIEW_MS - 1, limit=10) == []
    assert len(names.claim_due(now_ms=T + REVIEW_MS, limit=10)) == 2


def test_profile_uses_latest_cross_table_position_including_flat_snapshot_and_compact_history(tmp_path):
    store, names = setup(tmp_path)
    with connect_sqlite(store.path) as conn:
        conn.execute("INSERT INTO hl_news_normal_state VALUES(?,?,?,?,?)", (A, T, json.dumps({"observed_ms": T, "positions": {"xyz:BRENTOIL": {"size": "3"}}}),
                     json.dumps({"assets": {"xyz:BRENTOIL": {"rounds": 8, "win_rate": "0.75", "closed_pnl_less_fees": "900", "start_ms": T-100, "end_ms": T}}, "first_observed_fill_ms": T-200, "last_observed_fill_ms": T}), "{}"))
        conn.execute("INSERT INTO hl_news_state VALUES(?,?,?,?,?)", (A, T + 1, json.dumps({"observed_ms": T+1, "positions": {}}), "{}", "{}"))
    profile = build_profile(store, A, now_ms=T+2)
    assert profile["positions"] == {}
    assert len(json.dumps(profile)) < 8000
    assert {d["evidence_id"] for d in profile["dimensions"]} >= {"asset_profit", "asset_win_rate", "current_positions", "account_performance", "recent_actions", "holding_duration", "global_rank"}
    asset_dimension = next(d for d in profile["dimensions"] if d["evidence_id"] == "asset_profit")
    assert asset_dimension["window_start_ms"] == T-200 and asset_dimension["window_end_ms"] == T


def test_position_name_expires_on_reversal_and_auto_note_is_removed(tmp_path):
    store, names = setup(tmp_path)
    names.initialize()
    with connect_sqlite(store.path) as conn:
        conn.execute("INSERT INTO hl_news_normal_state VALUES(?,?,?,?,?)", (A, T, json.dumps({"observed_ms": T, "positions": {"xyz:BRENTOIL": {"size": "2"}}}), "{}", "{}"))
    claim = names.claim_due(now_ms=T, limit=1)[0]
    result = {"name": "原油多头0x12", "kind": "position", "coin": "xyz:BRENTOIL", "side": "long", "reason": "当前持仓", "selected_dimension": "current_positions", "evidence_refs": ["current_positions"],
              "coverage": {"dimensions": [{"evidence_id": "current_positions", "key": "current_positions", "available": True}]}}
    assert names.save_result(A, token=claim["claim_token"], result=result, now_ms=T, model="gpt-6-luna")
    assert store.metadata("address_note:" + A, {})["origin"] == "auto"
    store.commit_observation(address=A, now_ms=T+10, state={"observed_ms": T+10, "positions": {}}, history={}, coverage={}, fills=[], candidates=[], state_table="hl_news_normal_state")
    assert names.get(A)["status"] == "expired"
    assert store.metadata("address_note:" + A, {}) == {}


def test_invalid_name_output_and_final_copy_replacement_preserve_footer_and_amount():
    try:
        parse_result('{"name":"交易达人","kind":"position","coin":null,"side":null,"selected_dimension":"x","evidence_refs":["x"]}', A)
    except ValueError:
        pass
    else:
        raise AssertionError("name without exact address prefix must be rejected")
    event = {"addresses": [A], "coin": "xyz:BRENTOIL", "kinds": ["large_position"], "evidence": {"address_names": {A: "原油多头0x12"}}}
    draft = DraftBrief(title="某地址持有原油多仓", content="某地址持有价值100万美元原油多仓。")
    result = format_news(draft, event)
    assert "原油多头0x12" in result.title and "原油多头0x12" in result.content
    assert "100万美元" in result.content and result.content.endswith("地址：" + A)


def test_inflight_position_claim_is_rejected_after_account_snapshot_reverses(tmp_path):
    store, names = setup(tmp_path)
    names.initialize()
    with connect_sqlite(store.path) as conn:
        conn.execute("INSERT INTO hl_news_normal_state VALUES(?,?,?,?,?)", (A, T, json.dumps({"observed_ms": T, "positions": {"xyz:BRENTOIL": {"size": "2"}}}), "{}", "{}"))
    claim = names.claim_due(now_ms=T, limit=1)[0]
    with connect_sqlite(store.path) as conn:
        conn.execute("INSERT INTO hl_news_state VALUES(?,?,?,?,?)", (A, T+5, json.dumps({"observed_ms": T+5, "positions": {"xyz:BRENTOIL": {"size": "-2"}}}), "{}", "{}"))
    result = {"name": "原油多头0x12", "kind": "position", "coin": "xyz:BRENTOIL", "side": "long", "reason": "当前持仓", "selected_dimension": "current_positions", "evidence_refs": ["current_positions"],
              "coverage": {"dimensions": [{"evidence_id": "current_positions", "key": "current_positions", "available": True}]}}
    assert not names.save_result(A, token=claim["claim_token"], result=result, now_ms=T+6, model="gpt-6-luna")


def test_automatic_name_is_used_once_in_namespace_and_details_are_read_only(tmp_path):
    store, names = setup(tmp_path)
    names.initialize()
    claim = names.claim_due(now_ms=T, limit=1)[0]
    result = {"name": "近期BTC高胜率交易员0x12", "kind": "stable", "reason": "近期窗口", "selected_dimension": "asset_win_rate", "evidence_refs": ["asset_win_rate"],
              "coverage": {"dimensions": [{"evidence_id": "asset_win_rate", "label": "胜率", "available": True, "data": {"BTC": {"rounds": 12, "win_rate": "0.8", "window_start_ms": T-100, "window_end_ms": T-1}}}]}}
    assert names.save_result(A, token=claim["claim_token"], result=result, now_ms=T, model="gpt-6-luna")
    event = {"addresses": [A], "coin": "xyz:BRENTOIL", "kinds": [], "observed_ms": T, "evidence": {"position": {"size": "1"}, "address_names": {}}}
    event = store.with_address_notes(event)
    assert event["evidence"]["address_names"][A] == "近期BTC高胜率交易员0x12"
    assert event["evidence"]["address_notes"][A] == ""
    older = {**event, 'observed_ms': T-1}
    assert A not in store.with_address_notes(older)['evidence']['address_names']
    from packages.hyperliquid_news.naming import naming_details
    before = names.get(A)
    details = naming_details(store, A, now_ms=T+1)
    assert details["selected_evidence"]["evidence_id"] == "asset_win_rate"
    assert names.get(A)["checked_ms"] == before["checked_ms"]


def test_manual_empty_note_stays_locked_across_repeated_initialization(tmp_path):
    store, names = setup(tmp_path)
    names.initialize()
    names.set_manual(A, "", updated_by="editor@example.com")
    names.initialize()
    assert names.get(A)["manual_locked"] is True
    assert names.get(A)["current_name"] is None
    assert all(row["address"] != A for row in names.claim_due(now_ms=T, limit=10))


def test_ai_success_path_validates_evidence_and_appends_prefix():
    class Client:
        def generate_text(self, *, model, prompt, text_format, reasoning_effort):
            assert model == "gpt-6-luna"
            assert "不能把小额盈利写成爆赚" in prompt
            return json.dumps({"name": "近期BTC高胜率交易员", "kind": "stable", "coin": None, "side": None,
                "reason": "12轮中胜率80%", "selected_dimension": "asset_win_rate", "evidence_refs": ["asset_win_rate"]}, ensure_ascii=False)
    import tempfile
    from pathlib import Path
    path = Path(tempfile.mkdtemp()) / "ai.sqlite"
    store = NewsStore(path); store.initialize(); store.discover(A, source={"kind": "test"}, now_ms=T, limit=None)
    names = NamingStore(path); names.initialize()
    with connect_sqlite(path) as conn:
        conn.execute("INSERT INTO hl_news_normal_state VALUES(?,?,?,?,?)", (A, T, json.dumps({"observed_ms": T, "positions": {}}),
            json.dumps({"assets": {"BTC": {"rounds": 12, "win_rate": "0.8", "closed_pnl_less_fees": "3000000"}},
                        "first_observed_fill_ms": T-1000, "last_observed_fill_ms": T}), "{}"))
    report = NamingWorker(store, names, ai_client=Client(), batch_size=1).run_once(now_ms=T)
    assert report["saved"] == 1
    assert names.get(A)["current_name"] == "近期BTC高胜率交易员0x12"


def test_real_asset_field_names_stale_positions_and_zero_rounds(tmp_path):
    store, names = setup(tmp_path)
    names.initialize()
    with connect_sqlite(store.path) as conn:
        conn.execute("INSERT INTO hl_news_normal_state VALUES(?,?,?,?,?)", (A, T, json.dumps({"observed_ms": T, "positions": {"BTC": {"size": "2"}}}),
            json.dumps({"start_ms": T-2000, "end_ms": T, "assets": {"BTC": {"rounds": 0, "win_rate": None,
                "first_observed_fill_ms": T-1000, "last_observed_fill_ms": T, "closed_pnl_less_fees": "900"}}}), "{}"))
    dims = {d['key']: d for d in build_profile(store, A, now_ms=T+2*86400000)['dimensions']}
    assert dims['asset_profit']['data']['BTC']['first_observed_fill_ms'] == T-1000
    assert not dims['asset_win_rate']['available']
    assert not dims['current_positions']['available']
    assert dims['current_positions']['data'] == {}


def test_position_reply_requeues_after_concurrent_reversal(tmp_path):
    store, names = setup(tmp_path)
    class ReversingClient:
        def generate_text(self, **kwargs):
            store.commit_observation(address=A, now_ms=T+1, state={'observed_ms': T+1, 'positions': {'BTC': {'size': '-1'}}},
                history={}, coverage={}, fills=[], candidates=[], state_table='hl_news_state')
            return json.dumps({'name': 'BTC大多头', 'kind': 'position', 'coin': 'BTC', 'side': 'long', 'reason': '持仓',
                'selected_dimension': 'current_positions', 'evidence_refs': ['current_positions']})
    store.commit_observation(address=A, now_ms=T, state={'observed_ms': T, 'positions': {'BTC': {'size': '1'}}},
        history={}, coverage={}, fills=[], candidates=[], state_table='hl_news_normal_state')
    NamingWorker(store, names, ai_client=ReversingClient(), batch_size=1).run_once(now_ms=T+2)
    assert names.get(A)['current_name'] is None
    assert names.get(A)['inflight_token'] is None
    assert any(r['address'] == A for r in names.claim_due(now_ms=T+3))


def test_naming_cli_once_initializes_temporary_store(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from packages.hyperliquid_news import commands
    from packages.common import config
    from packages.x_processing import ai_client
    monkeypatch.setattr(commands, 'load_hyperliquid_storage_path', lambda: tmp_path/'cli.sqlite')
    monkeypatch.setattr(commands, 'load_storage_settings', lambda: SimpleNamespace(sqlite_path=tmp_path/'main.sqlite'))
    monkeypatch.setattr(config, 'load_x_processing_settings', lambda: SimpleNamespace(openai_api_key='test',
        openai_base_url='https://example.invalid/v1', openai_api_style='responses', writer_request_timeout_seconds=1,
        retry=SimpleNamespace(max_attempts=1, backoff_seconds=0)))
    monkeypatch.setattr(ai_client, 'OpenAIResponsesClient', lambda **kwargs: object())
    assert commands.run_command(SimpleNamespace(hyperliquid_action='run-naming', once=True, batch_size=4, interval_seconds=30)) == 0


def test_hyperliquid_init_and_status_use_postgres_without_sqlite_pipeline(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    from packages.hyperliquid_news import commands

    class FakeRepository:
        def init_schema(self):
            calls.append('init_schema')

        def seed_prompt_templates(self, *, root_dir):
            calls.append(('seed_prompt_templates', root_dir))

    class FakeStore:
        backend = 'postgres'

        def initialize(self):
            calls.append('store_initialize')

    calls = []
    monkeypatch.setattr(commands, 'load_hyperliquid_storage_path', lambda: tmp_path / 'hl.sqlite')
    monkeypatch.setattr(commands, 'load_storage_settings', lambda: SimpleNamespace(backend='postgres', sqlite_path=tmp_path / 'main.sqlite'))
    monkeypatch.setattr(commands, 'create_news_store', lambda _path: FakeStore())
    monkeypatch.setattr('packages.x_processing.repository.create_x_processing_repository', lambda _url: FakeRepository())
    monkeypatch.setattr(commands, 'get_paths', lambda: SimpleNamespace(root_dir=tmp_path))
    assert commands.run_command(SimpleNamespace(hyperliquid_action='init')) == 0
    assert calls == ['init_schema', ('seed_prompt_templates', tmp_path), 'store_initialize']

    monkeypatch.setattr('packages.hyperliquid_news.dashboard.dashboard', lambda _store, **kwargs: calls.append(kwargs) or {'initialized': True})
    assert commands.run_command(SimpleNamespace(hyperliquid_action='status', page=0)) == 0
    assert calls[-1] == {'pipeline_database_path': None, 'page': 0}
    assert '"initialized": true' in capsys.readouterr().out


def test_duplicate_feature_and_prefix_extend_actual_address_suffix(tmp_path):
    store, names = setup(tmp_path)
    other = A[:4]+'f'+A[5:]
    store.discover(other, source={'kind':'test'}, now_ms=T, limit=None)
    names.initialize()
    result = {'name':'普通地址0x12', 'kind':'stable', 'selected_dimension':'address_prefix', 'evidence_refs':['address_prefix'],
        'coverage':{'dimensions':[{'evidence_id':'address_prefix','available':True}]}}
    claims={r['address']:r for r in names.claim_due(now_ms=T,limit=10)}
    for address in (A,other):
        assert names.save_result(address,token=claims[address]['claim_token'],result=result,now_ms=T,model='gpt-6-luna')
    assert names.get(A)['current_name'] != names.get(other)['current_name']
    assert names.get(other)['current_name'].endswith(other[:5])


def test_invalid_model_reply_is_repaired_once_with_same_model(tmp_path):
    store, names = setup(tmp_path)
    names.initialize()
    class Client:
        calls=0
        def generate_text(self, **kwargs):
            self.calls+=1
            assert kwargs['model']=='gpt-6-luna'
            value={'name':'普通地址','kind':'stable','coin':None,'side':None,'reason':'缺少特长','evidence_refs':['address_prefix']}
            if self.calls==2: value['selected_dimension']='address_prefix'
            return json.dumps(value)
    client=Client()
    profile=build_profile(store,A,now_ms=T)
    result=NamingWorker(store,names,ai_client=client)._choose(profile,A)
    assert result['name']=='普通地址0x12'
    assert client.calls==2


def test_manual_note_completely_replaces_existing_ai_name_and_clears_details(tmp_path):
    store, names = setup(tmp_path)
    names.initialize()
    claim = names.claim_due(now_ms=T, limit=1)[0]
    ai_result = {
        "name": "近期BTC高胜率交易员0x12",
        "kind": "stable",
        "reason": "近期窗口胜率80%",
        "selected_dimension": "asset_win_rate",
        "evidence_refs": ["asset_win_rate"],
        "coverage": {"dimensions": [{"evidence_id": "asset_win_rate", "label": "胜率", "available": True,
            "data": {"BTC": {"rounds": 12, "win_rate": "0.8", "window_start_ms": T - 100, "window_end_ms": T - 1}}}]},
    }
    assert names.save_result(A, token=claim["claim_token"], result=ai_result, now_ms=T, model="gpt-6-luna")
    store.save_address_note(A, "AguilaTrades", updated_by="editor@example.com")
    row = names.get(A)
    assert row["manual_locked"] is True
    assert row["current_name"] == "AguilaTrades"
    assert row["model"] is None
    assert row["checked_ms"] == 0
    assert row["evidence"] == {}
    assert row["reason"] == "人工备注，程序不覆盖"

    from packages.hyperliquid_news.naming import naming_details
    details = naming_details(store, A, now_ms=T + 10)
    assert details["selected_evidence"] is None
    assert details["dimensions"] == []
    assert details["reason"] == "人工备注，程序不覆盖"

    event = {
        "addresses": [A],
        "coin": "BTC",
        "kinds": ["highlighted_address_action"],
        "observed_ms": T + 10,
        "evidence": {
            "highlights": ["historical_loss"],
            "previous_position": {"size": "0", "mark": "60000", "notional": "0"},
            "position": {"size": "-275.41", "mark": "60000", "notional": "16524600"},
            "actions": {"open_short_notional": "16524600"},
            "history": {},
        },
    }
    enriched = store.with_address_notes(event)
    assert enriched["evidence"]["address_names"] == {A: "AguilaTrades"}
    assert enriched["evidence"]["address_notes"] == {A: ""}
    assert enriched["evidence"]["address_name_evidence"] == {}

    final = format_news(
        DraftBrief(
            title="某地址增持约1,651万美元BTC空单",
            content="2026年11月24日23:08至23:12，某地址开立价值约1,652万美元的BTC空单。",
        ),
        enriched,
    )
    assert final.title.startswith("AguilaTrades")
    assert "2026 年 11 月 24 日 23:08 至 23:12，AguilaTrades开立" in final.content
    assert "胜率" not in final.content
    assert "某地址" not in final.title and "某地址" not in final.content
