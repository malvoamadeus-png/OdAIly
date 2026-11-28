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


def test_deep_playbook_end_to_end_escalation_upgrade_and_mode_filtering():
    from packages.investment_leads.analysis import (
        PLAYBOOK_PROMPT_VERSION,
        PlaybookContextOutput,
        PlaybookOutput,
        _normalize_playbook,
        _playbook_to_lead,
    )
    from packages.investment_leads.service import persist_capture_materials

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE hottopic_accounts(screen_name_lower TEXT PRIMARY KEY,status TEXT NOT NULL,"
        "created_at TEXT NOT NULL,investment_leads_enabled INTEGER NOT NULL DEFAULT 1,"
        "investment_leads_enabled_at TEXT)"
    )
    now = _iso()
    db.execute(
        "INSERT INTO hottopic_accounts(screen_name_lower,status,created_at,investment_leads_enabled,investment_leads_enabled_at) "
        "VALUES('alice','followed',?,1,?)",
        (now, now),
    )

    sample_playbook_raw = {
        "headline_call": "不追空已执行 ETH，主攻做空 WLFI，美股开市盯紧 ALTS 补跌。",
        "primary_bet": {
            "target": "WLFI", "ticker": "WLFI", "market": "Crypto", "direction": "做空",
            "asymmetry_pitch": "左侧赔率高，2-6 小时时间止损可控。",
        },
        "hidden_motive_redteam": {
            "most_lethal_hypothesis": "资金池存在紧迫流动性需求或保证金压力。",
            "why_not_normal": "战略地址大幅浮亏短时急售，违背常规持仓叙事。",
        },
        "counterparty_run_simulation": {
            "crowd_blindspot": "大众只盯着 ETH 卖单本身。",
            "market_maker_reaction": "做市商可能无公告先缩深度与降库存。",
            "short_hunter_angle": "空头顺着 WLFI 流动性和 Dolomite 借贷传导施压。",
        },
        "staged_playbook": [
            {
                "stage": "第一阶段", "opportunity": "主标的信用折价", "direction": "做空",
                "targets": ["WLFI"], "logic": "卖方国库急售打击主代币信心。", "current_status": "部分发生",
            },
            {
                "stage": "第三阶段", "opportunity": "借贷协议抵押风险", "direction": "观察",
                "targets": ["Dolomite"], "logic": "公开资料确认 Dolomite 为 WLFI Markets 借贷底层。", "current_status": "重点验证",
            },
            {
                "stage": "第四阶段", "opportunity": "美股财库滞后补跌", "direction": "做空",
                "targets": ["ALT5 Sigma (Nasdaq: ALTS)"], "logic": "SEC 8-K 披露其建立 WLFI 财库策略。", "current_status": "等待开市",
            },
        ],
        "assumption_boundary": "仅原帖与公开绑定为事实，其余为左侧假设。",
        "risk_and_invalidation": {
            "trade_nature": "左侧赔率博弈",
            "confirming_signals": ["WLFI 相对 ETH 持续走弱"],
            "thesis_stop_loss": "官方披露正常换仓且无后续抛售。",
            "time_stop_loss": "2-6 小时内无相对弱势则退出。",
        },
    }

    class FakeAnalyzer:
        primary_model = "gpt-6-luna"

        def scan(self, materials):
            return ModelOutput(
                [{
                    "material_id": m["material_id"],
                    "content_hash": m["content_hash"],
                    "classification": "driver_candidate",
                    "playbook_trigger": "anomaly_behavior",
                    "subject": "WLFI 战略存储地址亏本急售 ETH",
                    "action": "分批抛售 11978 ETH",
                    "change_object": "战略储备",
                    "event_time": "过去5小时",
                    "impact_relations": ["流动性压力"],
                    "summary": "WLFI 战略地址亏 48% 急售 ETH",
                    "reason": "具名项目方高代价反常动作",
                } for m in materials],
                "gpt-6-luna", None, 100, 40, 1,
            )

        def derive(self, candidate, related_events, existing_leads):
            # Simulate conservative derive returning empty for anomaly-only ETH sell
            return ModelOutput([], "gpt-6-luna", None, 80, 20, 1)

        def search_playbook_context(self, candidate):
            return PlaybookContextOutput(
                summary_text="公开资料显示 WLFI Markets 采用 Dolomite，ALT5 Sigma (Nasdaq: ALTS) 建立 WLFI 财库。",
                citations=[{"url": "https://worldlibertyfinancial.com/markets", "title": "WLFI Markets"}],
                queries=["WLFI Dolomite ALT5"],
                actual_model="gpt-6-luna",
                input_tokens=300,
                output_tokens=120,
            )

        def generate_playbook(self, candidate, related_events, *, context_text, citations, trigger_type):
            playbook = _normalize_playbook(
                sample_playbook_raw,
                trigger_type=trigger_type,
                citations=citations,
                research_summary=context_text,
            )
            lead = _playbook_to_lead(candidate, playbook)
            return PlaybookOutput(
                playbook=playbook, lead=lead, actual_model="gpt-6-luna",
                fallback_reason=None, input_tokens=400, output_tokens=200, calls=1,
            )

    service = InvestmentLeadService(Path(":memory:"), analyzer=FakeAnalyzer(), connection=db)
    service.flush_seconds = 0
    post_time = _iso()
    saved = persist_capture_materials(
        db,
        account_lower="alice",
        materials=[{
            "material_id": "tweet:wlfi-1",
            "author_screen_name": "alice",
            "role": "source_post",
            "relation": "original",
            "created_at_iso": post_time,
            "activity_at": post_time,
            "activity_time_complete": True,
            "url": "https://x.com/alice/status/1",
            "text": "WLFI 战略存储地址亏 48% 急售 11978 ETH",
        }],
        observed_at=post_time,
    )
    assert saved == 1
    # Add a standard lead to verify mixed feed & mode filtering
    std_id = service._save_lead(
        {
            "subject": "普通经营线索",
            "driver_logic": "公司营收增长",
            "assets": [{"name": "RegularCo", "ticker": "REG", "direction": "偏多", "assessment": {"impact": "medium", "confidence": "medium", "expectation_change": "增长", "market_acceptance": "明确"}}],
            "source_claims": [],
        },
        [],
        ModelOutput([], "gpt-6-luna", None, 0, 0, 0),
        now,
    )

    counts = service.process_once()
    assert counts["scanned"] == 1
    assert counts["derived"] == 1
    assert counts["playbooks"] == 1

    all_leads = service.list_leads()
    assert all_leads["total"] == 2
    playbook_leads = service.list_leads(mode="playbook")
    assert playbook_leads["total"] == 1
    pb_item = playbook_leads["items"][0]
    assert pb_item["playbook"]["mode"] == "deep_playbook"
    assert pb_item["playbook"]["trigger_type"] == "anomaly_behavior"
    assert pb_item["playbook"]["primary_bet"]["direction"] == "做空"
    assert any(a["ticker"] == "ALTS" for a in pb_item["assets"])
    # Search hits content inside playbook_json (e.g. Dolomite)
    assert service.list_leads(query="Dolomite")["total"] == 1
    assert [item["id"] for item in service.list_leads(mode="standard")["items"]] == [std_id]

    detail = service.lead_detail(pb_item["id"])
    assert detail is not None
    assert detail["playbook"]["staged_playbook"][2]["current_status"] == "等待开市"
    assert detail["revisions"][0]["actualModel"] == "gpt-6-luna"
    rev = db.execute("SELECT prompt_version FROM investment_lead_revisions WHERE lead_id=?", (pb_item["id"],)).fetchone()
    assert rev["prompt_version"] == PLAYBOOK_PROMPT_VERSION

    dash = service.dashboard()
    assert dash["results"]["playbooks"] == 1
    assert dash["usage"]["playbookCalls"] == 2
    assert dash["usage"]["inputTokens"] == 100 + 80 + 300 + 400


def test_high_impact_standard_derive_escalates_and_upgrades_existing_lead_in_place():
    from packages.investment_leads.analysis import (
        PlaybookContextOutput,
        PlaybookOutput,
        _normalize_playbook,
        _playbook_to_lead,
    )
    from packages.investment_leads.service import persist_capture_materials

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE hottopic_accounts(screen_name_lower TEXT PRIMARY KEY,status TEXT NOT NULL,"
        "created_at TEXT NOT NULL,investment_leads_enabled INTEGER NOT NULL DEFAULT 1,"
        "investment_leads_enabled_at TEXT)"
    )
    now = _iso()
    db.execute(
        "INSERT INTO hottopic_accounts(screen_name_lower,status,created_at,investment_leads_enabled,investment_leads_enabled_at) "
        "VALUES('bob','followed',?,1,?)",
        (now, now),
    )

    class UpgradeAnalyzer:
        primary_model = "gpt-6-luna"

        def scan(self, materials):
            return ModelOutput(
                [{
                    "material_id": m["material_id"],
                    "content_hash": m["content_hash"],
                    "classification": "driver_candidate",
                    "playbook_trigger": "",
                    "subject": "ICE 采用 Hyperliquid",
                    "action": "计划推出受监管永续市场",
                    "change_object": "美国永续市场",
                    "event_time": "拟议",
                    "impact_relations": ["估值重估"],
                    "summary": "ICE 拟基于 Hyperliquid 推出美国受监管永续市场",
                    "reason": "重大机构合作",
                } for m in materials],
                "gpt-6-luna", None, 50, 20, 1,
            )

        def derive(self, candidate, related_events, existing_leads):
            return ModelOutput(
                [{
                    "matched_lead_id": "",
                    "subject": "ICE 拟采用 Hyperliquid",
                    "driver_logic": "合规渠道扩张提升 HYPE 估值预期",
                    "conditions": "需获监管批准",
                    "assets": [{
                        "name": "Hyperliquid", "ticker": "HYPE", "market": "Crypto",
                        "direction": "偏多", "relation": "生态规模预期扩大",
                        "economic_channel": "valuation", "tradable": True,
                        "driver_type": "theme", "premise_type": "event_conditional",
                        "catalyst": "ICE 宣布合作计划",
                        "assessment": {
                            "impact": "high", "confidence": "medium",
                            "expectation_change": "机构采用预期跃升",
                            "market_acceptance": "市场对合规通道高度敏感",
                            "key_uncertainty": "费用分配", "reconsider_if": "审批受阻",
                        },
                    }],
                    "source_claims": [],
                    "source_material_ids": [candidate["material_id"]],
                }],
                "gpt-6-luna", None, 60, 30, 1,
            )

        def search_playbook_context(self, candidate):
            return PlaybookContextOutput(
                summary_text="ICE (NYSE: ICE) 与 CME (NASDAQ: CME) 在美国衍生品市场竞争。",
                citations=[{"url": "https://example.com/ice", "title": "ICE"}],
                queries=["ICE Hyperliquid"],
                actual_model="gpt-6-luna", input_tokens=90, output_tokens=30,
            )

        def generate_playbook(self, candidate, related_events, *, context_text, citations, trigger_type):
            pb = _normalize_playbook(
                {
                    "headline_call": "HYPE 先吃机构背书溢价，美股开市关注 ICE 与 CME 相对强弱。",
                    "primary_bet": {"target": "HYPE", "ticker": "HYPE", "market": "Crypto", "direction": "做多", "asymmetry_pitch": "合规预期催化"},
                    "hidden_motive_redteam": {"most_lethal_hypothesis": "ICE 借加密原生基础设施抢占美国永续入口。", "why_not_normal": "传统巨头罕见采用链上清算架构。"},
                    "counterparty_run_simulation": {"crowd_blindspot": "忽视费用分配条款。", "market_maker_reaction": "扩大波动定价。", "short_hunter_angle": "若无代币价值捕获则冲高回落。"},
                    "staged_playbook": [
                        {"stage": "第一阶段", "opportunity": "主标的溢价", "direction": "做多", "targets": ["HYPE"], "logic": "合规预期抬升。", "current_status": "部分发生"},
                        {"stage": "第四阶段", "opportunity": "传统交易所重估", "direction": "做多", "targets": ["ICE (NYSE: ICE)"], "logic": "美股开盘后重估衍生品增量。", "current_status": "等待开市"},
                    ],
                    "assumption_boundary": "监管尚未批准，条款未披露。",
                    "risk_and_invalidation": {"trade_nature": "右侧催化跟随", "confirming_signals": ["条款公布"], "thesis_stop_loss": "合作撤回", "time_stop_loss": "2 周内无进展"},
                },
                trigger_type=trigger_type, citations=citations, research_summary=context_text,
            )
            return PlaybookOutput(
                playbook=pb, lead=_playbook_to_lead(candidate, pb),
                actual_model="gpt-6-luna", fallback_reason=None, input_tokens=110, output_tokens=70, calls=1,
            )

    service = InvestmentLeadService(Path(":memory:"), analyzer=UpgradeAnalyzer(), connection=db)
    service.flush_seconds = 0
    post_time = _iso()
    saved = persist_capture_materials(
        db,
        account_lower="bob",
        materials=[{
            "material_id": "tweet:ice-1",
            "author_screen_name": "bob",
            "role": "source_post",
            "relation": "original",
            "created_at_iso": post_time,
            "activity_at": post_time,
            "activity_time_complete": True,
            "url": "https://x.com/bob/status/2",
            "text": "ICE 与 Trade[XYZ] 宣布计划基于 Hyperliquid 推出美国受监管永续市场",
        }],
        observed_at=post_time,
    )
    assert saved == 1

    counts = service.process_once()
    assert counts["scanned"] == 1
    assert counts["derived"] == 1
    assert counts["playbooks"] == 1
    # Upgraded in place: still 1 lead, not duplicated into 2
    leads = service.list_leads()
    assert leads["total"] == 1
    item = leads["items"][0]
    assert item["revision"] == 2
    assert item["playbook"]["mode"] == "deep_playbook"
    assert any(a["ticker"] == "HYPE" for a in item["assets"])
    assert any(a["ticker"] == "ICE" for a in item["assets"])


def test_playbook_24h_dedup_attaches_source_and_cleans_legacy_fields():
    from packages.investment_leads.analysis import (
        PlaybookContextOutput,
        PlaybookOutput,
        _normalize_playbook,
        _playbook_to_lead,
    )
    from packages.investment_leads.service import persist_capture_materials

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE hottopic_accounts(screen_name_lower TEXT PRIMARY KEY,status TEXT NOT NULL,"
        "created_at TEXT NOT NULL,investment_leads_enabled INTEGER NOT NULL DEFAULT 1,"
        "investment_leads_enabled_at TEXT)"
    )
    now = _iso()
    db.execute(
        "INSERT INTO hottopic_accounts(screen_name_lower,status,created_at,investment_leads_enabled,investment_leads_enabled_at) "
        "VALUES('carol','followed',?,1,?)",
        (now, now),
    )

    search_calls = 0

    class DedupAnalyzer:
        primary_model = "gpt-6-luna"

        def scan(self, materials):
            return ModelOutput(
                [{
                    "material_id": m["material_id"],
                    "content_hash": m["content_hash"],
                    "classification": "driver_candidate",
                    "playbook_trigger": "major_catalyst",
                    "subject": "OpenAI 与 Apple",
                    "action": "提起反诉",
                    "change_object": "商业机密与供应商关系",
                    "event_time": "刚刚",
                    "impact_relations": ["合作受损"],
                    "summary": "OpenAI 对 Apple 提起反诉并提出涉及供应商施压的商业机密指控。",
                    "reason": "巨头级冲突升级",
                } for m in materials],
                "gpt-6-luna", None, 40, 15, 1,
            )

        def derive(self, candidate, related_events, existing_leads):
            return ModelOutput([], "gpt-6-luna", None, 20, 10, 1)

        def search_playbook_context(self, candidate):
            nonlocal search_calls
            search_calls += 1
            return PlaybookContextOutput(
                summary_text="Apple (NASDAQ: AAPL) 与 OpenAI 合作。",
                citations=[{"url": "https://example.com/aapl", "title": "Apple OpenAI"}],
                queries=["OpenAI Apple"],
                actual_model="gpt-6-luna", input_tokens=50, output_tokens=20,
            )

        def generate_playbook(self, candidate, related_events, *, context_text, citations, trigger_type):
            pb = _normalize_playbook(
                {
                    "headline_call": "第一枪做空 AAPL。",
                    "primary_bet": {"target": "Apple", "ticker": "AAPL", "market": "US Stock", "direction": "做空", "asymmetry_pitch": "事件折价"},
                    "hidden_motive_redteam": {"most_lethal_hypothesis": "争夺 AI 硬件入口", "why_not_normal": "正面反诉升级"},
                    "counterparty_run_simulation": {"crowd_blindspot": "只看索赔额", "market_maker_reaction": "抬高期权波动率", "short_hunter_angle": "押注合作受损"},
                    "staged_playbook": [
                        {"stage": "第一阶段", "opportunity": "主标的折价", "direction": "做空", "targets": ["AAPL"], "logic": "合作受阻", "current_status": "等待开市"},
                    ],
                    "assumption_boundary": "仅原帖与公开资料为事实。",
                    "risk_and_invalidation": {
                        "trade_nature": "左侧赔率博弈|跨市场时差套利",
                        "confirming_signals": ["开盘走弱"],
                        "thesis_stop_loss": "迅速和解",
                        "time_stop_loss": "1-2 个交易时段",
                    },
                },
                trigger_type=trigger_type,
                citations=citations,
                research_summary=context_text,
                fallback_event_summary=candidate["summary"],
            )
            return PlaybookOutput(
                playbook=pb, lead=_playbook_to_lead(candidate, pb),
                actual_model="gpt-6-luna", fallback_reason=None, input_tokens=70, output_tokens=40, calls=1,
            )

    service = InvestmentLeadService(Path(":memory:"), analyzer=DedupAnalyzer(), connection=db)
    service.flush_seconds = 0
    t1 = _iso()
    persist_capture_materials(
        db,
        account_lower="carol",
        materials=[{
            "material_id": "tweet:oa-1", "author_screen_name": "carol", "role": "source_post", "relation": "original",
            "created_at_iso": t1, "activity_at": t1, "activity_time_complete": True,
            "url": "https://x.com/carol/status/10", "text": "OpenAI 对 Apple 提起反诉",
        }],
        observed_at=t1,
    )
    service.process_once()
    assert search_calls == 1
    first_list = service.list_leads(mode="playbook")
    assert first_list["total"] == 1
    first_pb = first_list["items"][0]["playbook"]
    assert first_pb["event_summary"] == "OpenAI 对 Apple 提起反诉并提出涉及供应商施压的商业机密指控。"
    assert first_pb["risk_and_invalidation"]["trade_nature"] == "左侧赔率博弈"

    # Second repost of the same event within 24h should attach as source without re-running web search
    t2 = _iso()
    persist_capture_materials(
        db,
        account_lower="carol",
        materials=[{
            "material_id": "tweet:oa-2", "author_screen_name": "carol", "role": "source_post", "relation": "original",
            "created_at_iso": t2, "activity_at": t2, "activity_time_complete": True,
            "url": "https://x.com/carol/status/11", "text": "转述报道：OpenAI 对 Apple 提起反诉并指控供应商施压",
        }],
        observed_at=t2,
    )
    service.process_once()
    assert search_calls == 1
    assert service.list_leads(mode="playbook")["total"] == 1
    detail = service.lead_detail(first_list["items"][0]["id"])
    assert detail is not None
    assert len(detail["sources"]) == 2

    # Mark as "没必要深度剧本" updates both investment_leads and linked investment_lead_materials
    lead_id = first_list["items"][0]["id"]
    marked = service.set_playbook_feedback(lead_id, "没必要深度剧本")
    assert marked["playbookFeedback"] == "没必要深度剧本"
    assert marked["updatedMaterials"] == 2
    assert service.list_leads(mode="playbook")["items"][0]["playbookFeedback"] == "没必要深度剧本"
    mat_flags = [
        row["playbook_feedback"]
        for row in db.execute("SELECT playbook_feedback FROM investment_lead_materials").fetchall()
    ]
    assert mat_flags == ["没必要深度剧本", "没必要深度剧本"]

    # Toggle back to empty clears both lead and linked materials
    cleared = service.set_playbook_feedback(lead_id, "")
    assert cleared["playbookFeedback"] == ""
    assert cleared["playbookFeedbackAt"] is None
    assert service.list_leads(mode="playbook")["items"][0]["playbookFeedback"] == ""
    assert all(
        row["playbook_feedback"] == ""
        for row in db.execute("SELECT playbook_feedback FROM investment_lead_materials").fetchall()
    )
