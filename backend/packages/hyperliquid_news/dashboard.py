from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite
from .models import Rules
from .store import NewsStore
from .eligibility import news_eligibility
from .scenarios import SCENARIOS, indexed_scenario_condition, scenario_cards, scenario_condition

KIND_LABELS = {
    'large_position': '巨大持仓', 'oi_share': '未平仓量占比突出', 'market_cap_share': '相当于市值占比突出',
    'large_profit': '巨额浮盈', 'large_loss': '巨额浮亏', 'highlighted_address_action': '有历史亮点地址的重要操作',
    'large_position_action': '大仓重要加减仓', 'large_reversal': '大仓反手', 'profit_giveback': '浮盈回吐',
    'profit_to_loss': '由盈转亏', 'pnl_sign_flip': '浮盈浮亏转换',
    'loss_addition': '亏损后继续加仓', 'large_realized_result': '大额主动平仓盈亏',
    'near_liquidation': '接近清算价', 'actual_liquidation': '实际清算', 'address_divergence': '多空分歧',
    'newly_observed_address_position': '新观察地址快速建仓',
}


def _shape_address_row(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    note_raw = item.pop('note_payload', None)
    block_raw = item.pop('block_payload', None)
    note_obj = note_raw if isinstance(note_raw, dict) else json.loads(note_raw or '{}')
    block_obj = block_raw if isinstance(block_raw, dict) else json.loads(block_raw or '{}')
    manual_locked = bool(item.get('name_manual_locked'))
    raw_name = str(item.get('current_name') or '').strip()
    if not manual_locked and raw_name.startswith('普通地址'):
        raw_name = ''
    note_text = str(note_obj.get('note') or '')
    if not manual_locked and note_obj.get('origin') == 'auto' and note_text.startswith('普通地址'):
        note_text = ''
    blocked = bool(block_obj.get('blocked'))
    position_count = int(item.get('position_count') or 0)
    has_fills = bool(item.get('has_fills'))
    acct_qualified = bool(item.get('acct_qualified'))
    acct_tier = str(item.get('acct_tier') or '')
    is_manual = bool(item.get('is_manual'))
    has_candidates = bool(item.get('has_candidates'))
    is_checked = bool(item.get('is_checked'))
    if blocked:
        category = 'blocked'
        perp_badge = '已拉黑'
    elif is_checked and not is_manual and not (acct_qualified and acct_tier != 'non_perp') and not has_fills and position_count == 0 and not has_candidates:
        category = 'filtered'
        perp_badge = '非合约/无交易'
    else:
        category = 'active'
        if position_count > 0:
            perp_badge = f'持仓中 {position_count}'
        elif acct_qualified:
            perp_badge = '合约大户'
        elif has_fills:
            perp_badge = '近期有成交'
        elif is_manual:
            perp_badge = '人工关注'
        else:
            perp_badge = '待首检'
    item['current_name'] = raw_name or None
    item['note'] = note_text
    item['blocked'] = blocked
    item['name_manual_locked'] = manual_locked
    item['position_count'] = position_count
    item['has_fills'] = has_fills
    item['acct_qualified'] = acct_qualified
    item['address_category'] = category
    item['perp_badge'] = perp_badge
    item['display_name'] = note_text if manual_locked else (raw_name or f"{item['address'][:4].lower()}地址")
    for internal in ('acct_tier', 'is_manual', 'has_candidates', 'is_checked'):
        item.pop(internal, None)
    return item


def dashboard(store: NewsStore, *, pipeline_database_path: Path | None = None, page: int = 0,
              page_size: int | None = None, scenario: str | None = None, dataset: str = 'live',
              view: str = 'scenarios', address_query: str = '', address_status: str = 'active') -> dict[str, Any]:
    if page < 0:
        raise ValueError('Invalid page')
    if dataset not in ('live', 'test'):
        raise ValueError('Invalid dataset')
    if view not in ('scenarios', 'addresses', 'rules', 'timeline'):
        raise ValueError('Invalid view')
    if view == 'timeline' and dataset != 'live':
        raise ValueError('Timeline only supports the live dataset')
    page_size = page_size if page_size is not None else (10 if view == 'timeline' else 15)
    if not 1 <= page_size <= 100:
        raise ValueError('Invalid page size')
    if not isinstance(address_query, str) or len(address_query) > 100 or address_status not in ('all', 'active', 'filtered', 'blocked'):
        raise ValueError('Invalid address search')
    if scenario:
        scenario_condition(scenario, dataset=dataset)
    if getattr(store, "backend", "sqlite") == "postgres":
        pipeline_database_path = None
        return _postgres_dashboard(store, page=page, page_size=page_size, scenario=scenario,
                                    dataset=dataset, view=view, address_query=address_query,
                                    address_status=address_status)
    if not store.path.exists():
        return {'initialized': False, 'events': [], 'addresses': [], 'rules': asdict(Rules()), 'labels': KIND_LABELS,
                'last_pass': {}, 'has_next': False, 'page': page, 'counts': {}, 'scenarios': scenario_cards(dataset=dataset), 'scenario': scenario, 'dataset': dataset, 'view': view}
    with connect_sqlite(store.path) as conn:
        separate_pipeline_database = (pipeline_database_path is not None and
                                      pipeline_database_path.resolve() != store.path.resolve())
        if separate_pipeline_database:
            conn.execute('ATTACH DATABASE ? AS odaily_main', (str(pipeline_database_path.resolve()),))
        pipeline_prefix = 'odaily_main.' if separate_pipeline_database else ''
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'hl_news_candidates' not in tables:
            return {'initialized': False, 'events': [], 'addresses': [], 'rules': asdict(Rules()), 'labels': KIND_LABELS,
                    'last_pass': {}, 'has_next': False, 'page': page, 'counts': {}, 'scenarios': scenario_cards(dataset=dataset), 'scenario': scenario, 'dataset': dataset, 'view': view}
        dispatch = dataset == 'live' and 'hl_news_dispatch' in tables
        indexed_live = dataset == 'live' and 'hl_news_candidate_scenarios' in tables
        condition = (indexed_scenario_condition(scenario) if indexed_live else
                     scenario_condition(scenario, dataset=dataset)) if scenario else '1=1'
        candidate_source = 'hl_news_candidates' if dataset == 'live' else ('hl_news_test_cases' if 'hl_news_test_cases' in tables else '(SELECT NULL AS payload, NULL AS observed_ms, NULL AS event_key WHERE 0)')
        timeline = view == 'timeline'
        candidate_alias = 'e' if timeline else 'c'
        joined = f'''LEFT JOIN hl_news_dispatch d ON d.event_key={candidate_alias}.event_key
                     LEFT JOIN {pipeline_prefix}tasks t ON t.id=d.task_id
                     LEFT JOIN {pipeline_prefix}x_task_pipeline p ON p.task_id=t.id''' if dispatch else ''
        if timeline:
            columns = '''d.status AS dispatch_status,d.task_id,t.status AS task_status,
                         p.draft_title,p.draft_content,p.final_title,p.final_content,
                         p.write_completed_at,p.format_completed_at,p.publish_completed_at''' if dispatch else '''
                         NULL AS dispatch_status,NULL AS task_id,NULL AS task_status,
                         NULL AS draft_title,NULL AS draft_content,NULL AS final_title,NULL AS final_content,NULL AS write_completed_at,
                         NULL AS format_completed_at,NULL AS publish_completed_at'''
            if address_query.strip():
                query = address_query.strip().lower()
                exact = len(query) == 42 and query.startswith('0x')
                address_filter = 'a.address_lower=?' if exact else "a.address_lower LIKE ? ESCAPE '\\'"
                address_value = query if exact else query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                deduplicate = '' if exact else 'GROUP BY e.event_key'
                rows = conn.execute(f'''SELECT e.event_key,e.observed_ms,e.coin,e.kinds,e.addresses,
                    COALESCE((SELECT json_group_array(cs.scenario_key) FROM hl_news_candidate_scenarios cs
                              WHERE cs.event_key=e.event_key),'[]') AS scenario_keys,{columns}
                    FROM hl_news_candidate_addresses a
                    JOIN hl_news_candidate_timeline e ON e.event_key=a.event_key {joined}
                    WHERE {address_filter}
                    {deduplicate} ORDER BY a.observed_ms DESC,a.event_key ASC LIMIT ? OFFSET ?''',
                    (address_value, page_size+1, page*page_size)).fetchall()
            else:
                rows = conn.execute(f'''SELECT e.event_key,e.observed_ms,e.coin,e.kinds,e.addresses,
                    COALESCE((SELECT json_group_array(cs.scenario_key) FROM hl_news_candidate_scenarios cs
                              WHERE cs.event_key=e.event_key),'[]') AS scenario_keys,{columns}
                    FROM hl_news_candidate_timeline e {joined}
                    ORDER BY e.observed_ms DESC,e.event_key ASC LIMIT ? OFFSET ?''',
                    (page_size+1, page*page_size)).fetchall()
        else:
            columns = '''d.status AS dispatch_status,d.error AS dispatch_error,d.task_id,t.status AS task_status,
                         p.draft_title,p.draft_content,p.final_title,p.final_content,p.writer_model,
                         p.search_result,p.search_completed_at,p.prompt_version_id,v.version_number AS prompt_version_number,p.write_completed_at,p.format_completed_at,p.publish_completed_at,
                         v.template_key AS prompt_template_key,p.push_result,p.last_error,p.publisher_reason_code''' if dispatch else 'NULL AS task_id'
            joined = f'''LEFT JOIN hl_news_dispatch d ON d.event_key=c.event_key
                        LEFT JOIN {pipeline_prefix}tasks t ON t.id=d.task_id
                        LEFT JOIN {pipeline_prefix}x_task_pipeline p ON p.task_id=t.id
                        LEFT JOIN {pipeline_prefix}prompt_template_versions v ON v.id=p.prompt_version_id''' if dispatch else ''
            rows = conn.execute(f'''SELECT c.payload,{columns} FROM {candidate_source} c {joined} WHERE {condition}
                        ORDER BY c.observed_ms DESC,c.event_key LIMIT ? OFFSET ?''',
                        (page_size+1 if view == 'scenarios' else 0, page*page_size)).fetchall()
        if timeline:
            scenarios = scenario_cards(dataset=dataset)
        else:
            visible = {scenario['key'] for scenario in scenario_cards(dataset=dataset)}
            selected_scenarios = [s for s in SCENARIOS if s[0] in visible]
            if indexed_live:
                scenario_counts = {row['scenario_key']: row['count'] for row in conn.execute(
                    'SELECT scenario_key,count(*) AS count FROM hl_news_candidate_scenarios GROUP BY scenario_key')}
            else:
                count_columns = ','.join(f'SUM(CASE WHEN {scenario_condition(s[0], dataset=dataset)} THEN 1 ELSE 0 END) AS "{s[0]}"' for s in selected_scenarios)
                scenario_counts = dict(conn.execute(f'SELECT {count_columns} FROM {candidate_source} c').fetchone())
            scenarios = scenario_cards({k: v or 0 for k, v in scenario_counts.items()}, dataset=dataset)
        events = []
        for row in rows[:page_size]:
            if timeline:
                addresses = json.loads(row['addresses'])
                events.append({
                    'key': row['event_key'], 'observed_ms': row['observed_ms'], 'addresses': addresses,
                    'coin': row['coin'], 'kinds': json.loads(row['kinds']),
                    'scenario_keys': json.loads(row['scenario_keys']),
                    'pipeline': {key: row[key] for key in row.keys()
                                 if key not in ('event_key', 'observed_ms', 'addresses', 'coin', 'kinds', 'scenario_keys')},
                    'source_url': 'https://hyperbot.network/trader/' + addresses[0],
                })
                continue
            item = json.loads(row['payload'])
            actions = (item.get('evidence') or {}).get('actions')
            if isinstance(actions, dict) and isinstance(actions.get('fill_keys'), list):
                actions['fill_count'] = len(actions.pop('fill_keys'))
            pipeline = {key: row[key] for key in row.keys() if key != 'payload'}
            if pipeline.get('search_result'):
                pipeline['search_result'] = json.loads(pipeline['search_result'])
            if pipeline.get('push_result'):
                pipeline['push_result'] = json.loads(pipeline['push_result'])
            item['pipeline'] = pipeline
            item['news_eligibility'] = news_eligibility(item)
            preview = conn.execute('SELECT payload FROM hl_news_metadata WHERE key=?',
                                   ('editorial_preview:' + item['key'],)).fetchone()
            if preview:
                item['editorial_preview'] = json.loads(preview['payload'])
            item['source_url'] = 'https://hyperbot.network/trader/' + item['addresses'][0]
            events.append(item)
        addresses = []
        address_clause = "instr(lower(a.address),lower(?))>0" if view == 'timeline' else "(instr(lower(a.address),lower(?))>0 OR instr(lower(COALESCE(json_extract(m.payload,'$.note'),'')),lower(?))>0)"
        address_params = (address_query.strip(),) if view == 'timeline' else (address_query.strip(), address_query.strip())
        name_table = 'hl_news_address_names' in tables
        name_columns = ("n.current_name,n.kind AS name_kind,n.coin AS name_coin,n.side AS name_side,n.status AS name_status,"
                        "n.manual_locked AS name_manual_locked,n.checked_ms AS name_checked_ms,n.next_check_ms AS name_next_check_ms") if name_table else "NULL AS current_name,NULL AS name_kind,NULL AS name_coin,NULL AS name_side,NULL AS name_status,0 AS name_manual_locked,0 AS name_checked_ms,0 AS name_next_check_ms"
        name_join = 'LEFT JOIN hl_news_address_names n ON n.address=a.address' if name_table else ''
        blocked_expr = "COALESCE(json_extract(b.payload,'$.blocked'),0)=1"
        has_fills_expr = "(EXISTS (SELECT 1 FROM hl_news_fills f WHERE f.address=a.address) OR EXISTS (SELECT 1 FROM hl_news_compact_history ch WHERE ch.address=a.address) OR COALESCE(json_extract(ns.history,'$.fills'),0)>0)"
        pos_count_expr = "COALESCE((SELECT count(*) FROM json_each(COALESCE(NULLIF(json_extract(ns.state,'$.positions'),'{}'), json_extract(s.state,'$.positions'), '{}'))), 0)"
        has_cands_expr = "EXISTS (SELECT 1 FROM hl_news_candidate_addresses ca WHERE ca.address_lower=a.address)"
        is_manual_expr = "(COALESCE(a.sources,'') LIKE '%\"manual\"%' OR COALESCE(a.sources,'') LIKE '%\"large_observed_trade\"%')"
        is_qualified_expr = "(COALESCE(ap.qualified,0)=1 AND COALESCE(ap.tier,'')!='non_perp')"
        is_checked_expr = "(COALESCE(ap.tier,'')='non_perp' OR (ns.address IS NOT NULL AND ap.address IS NOT NULL AND ap.qualified=0))"
        filtered_expr = f"(NOT ({blocked_expr}) AND {is_checked_expr} AND NOT ({is_manual_expr}) AND NOT ({is_qualified_expr}) AND NOT ({has_fills_expr}) AND ({pos_count_expr})=0 AND NOT ({has_cands_expr}))"
        active_expr = f"(NOT ({blocked_expr}) AND NOT ({filtered_expr}))"
        status_expr = (
            "1=1" if address_status == 'all' else
            blocked_expr if address_status == 'blocked' else
            filtered_expr if address_status == 'filtered' else
            active_expr
        )
        limit_val = 100 if view == 'addresses' else 1 if view == 'timeline' and address_query.strip() else 0
        if limit_val:
            for row in conn.execute(f'''SELECT a.address,a.first_seen_ms,a.last_seen_ms,a.last_polled_ms,a.error,
                m.payload AS note_payload,b.payload AS block_payload,{name_columns},
                {pos_count_expr} AS position_count,
                CASE WHEN {has_fills_expr} THEN 1 ELSE 0 END AS has_fills,
                CASE WHEN {is_qualified_expr} THEN 1 ELSE 0 END AS acct_qualified,
                COALESCE(ap.tier,'') AS acct_tier,
                CASE WHEN {is_manual_expr} THEN 1 ELSE 0 END AS is_manual,
                CASE WHEN {has_cands_expr} THEN 1 ELSE 0 END AS has_candidates,
                CASE WHEN {is_checked_expr} THEN 1 ELSE 0 END AS is_checked
                FROM hl_news_addresses a
                LEFT JOIN hl_news_metadata m ON m.key='address_note:' || a.address
                LEFT JOIN hl_news_metadata b ON b.key='address_block:' || a.address
                LEFT JOIN hl_news_normal_state ns ON ns.address=a.address
                LEFT JOIN hl_news_state s ON s.address=a.address
                LEFT JOIN hl_news_account_pool ap ON ap.address=a.address
                {name_join}
                WHERE {address_clause} AND ({status_expr})
                ORDER BY (lower(a.address)=lower(?)) DESC,
                         ({pos_count_expr}>0) DESC,
                         ({is_qualified_expr}) DESC,
                         ({has_fills_expr}) DESC,
                         a.last_polled_ms DESC,a.address LIMIT ?''',
                (*address_params, address_query.strip(), limit_val)):
                addresses.append(_shape_address_row(dict(row)))
        counts: dict[str, int] = {}
        if not timeline:
            addr_counts = conn.execute(f'''SELECT
                SUM(CASE WHEN {active_expr} THEN 1 ELSE 0 END) AS active_n,
                SUM(CASE WHEN {filtered_expr} THEN 1 ELSE 0 END) AS filtered_n,
                SUM(CASE WHEN {blocked_expr} THEN 1 ELSE 0 END) AS blocked_n,
                count(*) AS total_n
                FROM hl_news_addresses a
                LEFT JOIN hl_news_metadata b ON b.key='address_block:' || a.address
                LEFT JOIN hl_news_normal_state ns ON ns.address=a.address
                LEFT JOIN hl_news_state s ON s.address=a.address
                LEFT JOIN hl_news_account_pool ap ON ap.address=a.address''').fetchone()
            counts = {
                'addresses': int(addr_counts['active_n'] or 0),
                'addresses_filtered': int(addr_counts['filtered_n'] or 0),
                'addresses_blocked': int(addr_counts['blocked_n'] or 0),
                'addresses_total': int(addr_counts['total_n'] or 0),
                'events': conn.execute('SELECT count(*) FROM hl_news_candidates').fetchone()[0],
            }
            counts['test_cases'] = conn.execute('SELECT count(*) FROM hl_news_test_cases').fetchone()[0] if 'hl_news_test_cases' in tables else 0
            if dispatch:
                counts['submitted'] = conn.execute("SELECT count(*) FROM hl_news_dispatch WHERE task_id IS NOT NULL AND status!='duplicate'").fetchone()[0]
    return {'initialized': True, 'events': events, 'addresses': addresses, 'rules': asdict(Rules()), 'labels': KIND_LABELS,
            'last_pass': store.metadata('last_pass', {}) if not timeline else {}, 'has_next': len(rows) > page_size, 'page': page,
            'counts': counts, 'scenarios': scenarios, 'scenario': scenario, 'dataset': dataset, 'view': view, 'publication_mode': 'backend_only',
            'production': store.metadata('production_account_profit', None),
            'account_pool': store.account_pool_stats(int(time.time()*1000)) if not timeline and 'hl_news_account_pool' in tables else {},
            'flow': ['硬规则命中', '内部行为防重', '搜索者（跨来源查重）', '编写者1（独立Prompt）', '编写者2', '固定数据组装', '挂后台']}


def address_naming_details(store: NewsStore, address: str) -> dict[str, Any]:
    from .naming import naming_details
    return naming_details(store, address)


def _postgres_address_sql():
    blocked_expr = "COALESCE(b.payload->>'blocked','false')='true'"
    has_fills_expr = "(EXISTS (SELECT 1 FROM hl_news_fills f WHERE f.address=a.address) OR EXISTS (SELECT 1 FROM hl_news_compact_history ch WHERE ch.address=a.address) OR COALESCE((ns.history->>'fills')::bigint,0)>0)"
    pos_count_expr = "COALESCE((SELECT count(*) FROM jsonb_object_keys(CASE WHEN jsonb_typeof(ns.state->'positions')='object' AND ns.state->'positions'<>'{}'::jsonb THEN ns.state->'positions' WHEN jsonb_typeof(s.state->'positions')='object' THEN s.state->'positions' ELSE '{}'::jsonb END)), 0)"
    has_cands_expr = "EXISTS (SELECT 1 FROM hl_news_candidate_addresses ca WHERE ca.address_lower=a.address)"
    is_manual_expr = "EXISTS (SELECT 1 FROM jsonb_array_elements(a.sources) src WHERE src->>'kind' IN ('manual','large_observed_trade'))"
    is_qualified_expr = "(COALESCE(ap.qualified,false)=true AND COALESCE(ap.tier,'')<>'non_perp')"
    is_checked_expr = "(COALESCE(ap.tier,'')='non_perp' OR (ns.address IS NOT NULL AND ap.address IS NOT NULL AND ap.qualified=false))"
    filtered_expr = f"(NOT ({blocked_expr}) AND {is_checked_expr} AND NOT ({is_manual_expr}) AND NOT ({is_qualified_expr}) AND NOT ({has_fills_expr}) AND ({pos_count_expr})=0 AND NOT ({has_cands_expr}))"
    active_expr = f"(NOT ({blocked_expr}) AND NOT ({filtered_expr}))"
    return {
        'blocked': blocked_expr,
        'has_fills': has_fills_expr,
        'pos_count': pos_count_expr,
        'has_cands': has_cands_expr,
        'is_manual': is_manual_expr,
        'is_qualified': is_qualified_expr,
        'is_checked': is_checked_expr,
        'filtered': filtered_expr,
        'active': active_expr,
    }


def _postgres_address_counts(conn, exprs: dict[str, str]) -> dict[str, int]:
    row = conn.execute(
        f"""SELECT count(*) AS n,
            count(*) FILTER (WHERE {exprs['active']}) AS active_n,
            count(*) FILTER (WHERE {exprs['filtered']}) AS filtered_n,
            count(*) FILTER (WHERE {exprs['blocked']}) AS blocked_n
            FROM hl_news_addresses a
            LEFT JOIN hl_news_metadata b ON b.key='address_block:'||a.address
            LEFT JOIN hl_news_normal_state ns USING(address)
            LEFT JOIN hl_news_state s USING(address)
            LEFT JOIN hl_news_account_pool ap USING(address)"""
    ).fetchone() or {"n": 0}
    total_n = int(row["n"])
    return {
        "addresses": int(row["active_n"]) if "active_n" in row else total_n,
        "addresses_filtered": int(row.get("filtered_n", 0)),
        "addresses_blocked": int(row.get("blocked_n", 0)),
        "addresses_total": total_n,
    }


def _postgres_dashboard(store, *, page: int, page_size: int, scenario: str | None,
                        dataset: str, view: str, address_query: str, address_status: str) -> dict[str, Any]:
    """Bounded PostgreSQL dashboard read path; no SQLite SQL or ATTACH fallback."""
    import json
    with store._connect(autocommit=True) as conn:
        source = "hl_news_candidates" if dataset == "live" else "hl_news_test_cases"
        exists = conn.execute("SELECT to_regclass(%s) AS relation", (f"hyperliquid_news.{source}",)).fetchone()
        if not exists or exists["relation"] is None:
            return {"initialized": False, "events": [], "addresses": [], "rules": asdict(Rules()), "labels": KIND_LABELS,
                    "last_pass": {}, "has_next": False, "page": page, "counts": {}, "scenarios": scenario_cards(dataset=dataset),
                    "scenario": scenario, "dataset": dataset, "view": view}
        exprs = _postgres_address_sql()
        status_expr = (
            "true" if address_status == "all" else
            exprs["blocked"] if address_status == "blocked" else
            exprs["filtered"] if address_status == "filtered" else
            exprs["active"]
        )
        if view == "addresses":
            address_like = f"%{address_query.strip().lower()}%" if address_query.strip() else "%"
            address_rows = conn.execute(f"""SELECT a.address,a.first_seen_ms,a.last_seen_ms,a.last_polled_ms,a.error,
                m.payload AS note_payload,b.payload AS block_payload,n.current_name,n.kind AS name_kind,n.status AS name_status,
                n.manual_locked AS name_manual_locked,n.coin AS name_coin,n.side AS name_side,n.checked_ms AS name_checked_ms,n.next_check_ms AS name_next_check_ms,
                {exprs['pos_count']} AS position_count,
                {exprs['has_fills']} AS has_fills,
                {exprs['is_qualified']} AS acct_qualified,
                COALESCE(ap.tier,'') AS acct_tier,
                {exprs['is_manual']} AS is_manual,
                {exprs['has_cands']} AS has_candidates,
                {exprs['is_checked']} AS is_checked
                FROM hl_news_addresses a LEFT JOIN hl_news_metadata m ON m.key='address_note:'||a.address
                LEFT JOIN hl_news_metadata b ON b.key='address_block:'||a.address LEFT JOIN hl_news_address_names n USING(address)
                LEFT JOIN hl_news_normal_state ns USING(address) LEFT JOIN hl_news_state s USING(address)
                LEFT JOIN hl_news_account_pool ap USING(address)
                WHERE (a.address ILIKE %s OR COALESCE(m.payload->>'note','') ILIKE %s) AND ({status_expr})
                ORDER BY (lower(a.address)=lower(%s)) DESC,
                         ({exprs['pos_count']}>0) DESC,
                         ({exprs['is_qualified']}) DESC,
                         ({exprs['has_fills']}) DESC,
                         a.last_polled_ms DESC,a.address LIMIT 100""",
                (address_like,address_like,address_query.strip())).fetchall()
            addresses = [_shape_address_row(dict(row)) for row in address_rows]
            return {"initialized": True,"events": [],"addresses": addresses,"rules": asdict(Rules()),"labels": KIND_LABELS,
                "last_pass": {},"has_next": False,"page": page,"counts": _postgres_address_counts(conn, exprs),
                "scenarios": scenario_cards(dataset=dataset),"scenario": scenario,"dataset": dataset,"view": view,
                "publication_mode": "backend_only","account_pool": store.account_pool_stats(int(time.time()*1000))}
        if view == "rules":
            return {"initialized": True,"events": [],"addresses": [],"rules": asdict(Rules()),"labels": KIND_LABELS,
                "last_pass": {},"has_next": False,"page": page,"counts": {},"scenarios": scenario_cards(dataset=dataset),
                "scenario": scenario,"dataset": dataset,"view": view,"publication_mode": "backend_only"}
        scenario_filter = ""
        params: list[Any] = []
        if scenario:
            if dataset == "test":
                scenario_filter = " AND c.payload->'test_scenarios' ? %s"
            else:
                scenario_filter = " AND EXISTS (SELECT 1 FROM hl_news_candidate_scenarios cs WHERE cs.event_key=c.event_key AND cs.scenario_key=%s)"
            params.append(scenario)
        if view == "timeline":
            address_query_value = address_query.strip().lower()
            timeline_filter = ""
            timeline_params: list[Any] = []
            if address_query_value:
                exact = len(address_query_value) == 42 and address_query_value.startswith("0x")
                timeline_filter = "WHERE a.address_lower=%s" if exact else "WHERE a.address_lower LIKE %s"
                timeline_params.append(address_query_value if exact else address_query_value.replace("%", "\\%").replace("_", "\\_") + "%")
            rows = conn.execute(f"""SELECT e.event_key,e.observed_ms,e.coin,e.kinds,e.addresses,
                    COALESCE((SELECT jsonb_agg(cs.scenario_key ORDER BY cs.scenario_key) FROM hl_news_candidate_scenarios cs WHERE cs.event_key=e.event_key),'[]'::jsonb) AS scenario_keys,
                    d.status AS dispatch_status,d.task_id,t.status AS task_status,p.draft_title,p.draft_content,p.final_title,p.final_content,
                    p.write_completed_at,p.format_completed_at,p.publish_completed_at
                FROM hl_news_candidate_timeline e LEFT JOIN hl_news_candidate_addresses a ON a.event_key=e.event_key
                LEFT JOIN hl_news_dispatch d ON d.event_key=e.event_key LEFT JOIN main.tasks t ON t.id=d.task_id
                LEFT JOIN main.x_task_pipeline p ON p.task_id=t.id {timeline_filter}
                GROUP BY e.event_key,e.observed_ms,e.coin,e.kinds,e.addresses,d.status,d.task_id,t.status,p.draft_title,p.draft_content,p.final_title,p.final_content,p.write_completed_at,p.format_completed_at,p.publish_completed_at
                ORDER BY e.observed_ms DESC,e.event_key LIMIT %s OFFSET %s""",
                (*timeline_params,page_size+1,page*page_size)).fetchall()
        else:
            pipeline_join = "LEFT JOIN hl_news_dispatch d ON d.event_key=c.event_key LEFT JOIN main.tasks t ON t.id=d.task_id LEFT JOIN main.x_task_pipeline p ON p.task_id=t.id LEFT JOIN main.prompt_template_versions v ON v.id=p.prompt_version_id" if dataset == "live" else ""
            pipeline_columns = ",d.status AS dispatch_status,d.error AS dispatch_error,d.task_id,t.status AS task_status,p.draft_title,p.draft_content,p.final_title,p.final_content,p.writer_model,p.search_result,p.search_completed_at,p.prompt_version_id,v.version_number AS prompt_version_number,p.write_completed_at,p.format_completed_at,p.publish_completed_at,v.template_key AS prompt_template_key,p.push_result,p.last_error,p.publisher_reason_code" if dataset == "live" else ",NULL AS task_id"
            coin_column = "c.coin" if dataset == "live" else "COALESCE(c.payload->>'coin', '') AS coin"
            payload_projection = """CASE
                WHEN jsonb_typeof(c.payload #> '{evidence,actions,fill_keys}') = 'array' THEN
                    jsonb_set(c.payload #- '{evidence,actions,fill_keys}',
                        '{evidence,actions,fill_count}',
                        to_jsonb(jsonb_array_length(c.payload #> '{evidence,actions,fill_keys}')), true)
                ELSE c.payload #- '{evidence,actions,fill_keys}'
            END"""
            rows = conn.execute(f"SELECT c.event_key,c.observed_ms,{coin_column},{payload_projection} AS payload{pipeline_columns} FROM {source} c {pipeline_join} WHERE true{scenario_filter} ORDER BY c.observed_ms DESC,c.event_key LIMIT %s OFFSET %s",
                                (*params, page_size + 1, page * page_size)).fetchall()
        events = []
        for row in rows[:page_size]:
            if view == "timeline":
                addresses_for_event = row["addresses"] if isinstance(row["addresses"],list) else json.loads(row["addresses"])
                scenario_keys = row["scenario_keys"] if isinstance(row["scenario_keys"],list) else json.loads(row["scenario_keys"])
                events.append({"key":row["event_key"],"observed_ms":row["observed_ms"],"addresses":addresses_for_event,"coin":row["coin"],
                    "kinds":row["kinds"] if isinstance(row["kinds"],list) else json.loads(row["kinds"]),"scenario_keys":scenario_keys,
                    "pipeline":{key:row[key] for key in ("dispatch_status","task_id","task_status","draft_title","draft_content","final_title","final_content","write_completed_at","format_completed_at","publish_completed_at")},
                    "source_url":"https://hyperbot.network/trader/"+(addresses_for_event[0] if addresses_for_event else "")})
                continue
            payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
            pipeline = {key:row[key] for key in row.keys() if key not in ("event_key","observed_ms","coin","payload")}
            for key in ("search_result","push_result"):
                if isinstance(pipeline.get(key),str): pipeline[key]=json.loads(pipeline[key])
            payload["pipeline"] = pipeline
            payload["news_eligibility"] = news_eligibility(payload)
            preview = conn.execute("SELECT payload FROM hl_news_metadata WHERE key=%s", ("editorial_preview:" + row["event_key"],)).fetchone()
            if preview:
                payload["editorial_preview"] = preview["payload"] if isinstance(preview["payload"], dict) else json.loads(preview["payload"])
            payload["source_url"] = "https://hyperbot.network/trader/" + payload.get("addresses", [""])[0]
            events.append(payload)
        addresses = []
        address_limit = 1 if view == "timeline" and address_query.strip() else 0
        address_rows = conn.execute(f"""SELECT a.address,a.first_seen_ms,a.last_seen_ms,a.last_polled_ms,a.error,
            m.payload AS note_payload,b.payload AS block_payload,n.current_name,n.kind AS name_kind,n.status AS name_status,n.manual_locked AS name_manual_locked,
            {exprs['pos_count']} AS position_count,{exprs['has_fills']} AS has_fills,{exprs['is_qualified']} AS acct_qualified,
            COALESCE(ap.tier,'') AS acct_tier,{exprs['is_manual']} AS is_manual,{exprs['has_cands']} AS has_candidates,{exprs['is_checked']} AS is_checked
            FROM hl_news_addresses a LEFT JOIN hl_news_metadata m ON m.key='address_note:'||a.address
            LEFT JOIN hl_news_metadata b ON b.key='address_block:'||a.address LEFT JOIN hl_news_address_names n USING(address)
            LEFT JOIN hl_news_normal_state ns USING(address) LEFT JOIN hl_news_state s USING(address)
            LEFT JOIN hl_news_account_pool ap USING(address)
            WHERE a.address ILIKE %s AND ({status_expr})
            ORDER BY (lower(a.address)=lower(%s)) DESC,a.last_polled_ms DESC,a.address LIMIT %s""",
            (f"%{address_query.strip().lower()}%" if address_query.strip() else "%",address_query.strip(),address_limit)).fetchall() if address_limit else []
        addresses = [_shape_address_row(dict(row)) for row in address_rows]
        counts = {
            **_postgres_address_counts(conn, exprs),
            "events": int(conn.execute(f"SELECT count(*) AS n FROM {source}").fetchone()["n"]),
        }
        counts["test_cases"] = int(conn.execute("SELECT count(*) AS n FROM hl_news_test_cases").fetchone()["n"]) if dataset == "live" else counts["events"]
        visible = {card['key'] for card in scenario_cards(dataset=dataset)}
        selected = [item for item in SCENARIOS if item[0] in visible]
        scenario_counts = {item[0]: 0 for item in selected}
        if dataset == "live":
            for row in conn.execute("SELECT scenario_key,count(*) AS n FROM hl_news_candidate_scenarios WHERE scenario_key=ANY(%s) GROUP BY scenario_key",
                                    (list(visible),)).fetchall():
                scenario_counts[row["scenario_key"]] = int(row["n"])
        else:
            for item in selected:
                scenario_counts[item[0]] = int(conn.execute("SELECT count(*) AS n FROM hl_news_test_cases c WHERE payload->'test_scenarios' ? %s",
                                                            (item[0],)).fetchone()["n"])
    scenarios = scenario_cards(scenario_counts, dataset=dataset)
    return {"initialized": True, "events": events, "addresses": addresses, "rules": asdict(Rules()), "labels": KIND_LABELS,
            "last_pass": store.metadata("last_pass", {}) if view != "timeline" else {}, "has_next": len(rows) > page_size,
            "page": page, "counts": counts, "scenarios": scenarios, "scenario": scenario,
            "dataset": dataset, "view": view, "publication_mode": "backend_only", "production": store.metadata("production_account_profit", None),
            "account_pool": store.account_pool_stats(int(time.time()*1000)), "flow": ["硬规则命中", "内部行为防重", "搜索者（跨来源查重）", "编写者1（独立Prompt）", "编写者2", "固定数据组装", "挂后台"]}
