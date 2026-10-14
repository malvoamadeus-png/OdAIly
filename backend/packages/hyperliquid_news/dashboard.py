from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any

from packages.common.storage import connect_sqlite
from .models import Rules
from .store import NewsStore
from .eligibility import news_eligibility
from .scenarios import SCENARIOS, scenario_cards, scenario_condition

KIND_LABELS = {
    'large_position': '巨大持仓', 'oi_share': '未平仓量占比突出', 'market_cap_share': '相当于市值占比突出',
    'large_profit': '巨额浮盈', 'large_loss': '巨额浮亏', 'highlighted_address_action': '有历史亮点地址的重要操作',
    'large_position_action': '大仓重要加减仓', 'large_reversal': '大仓反手', 'profit_giveback': '浮盈回吐',
    'profit_to_loss': '由盈转亏', 'loss_addition': '亏损后继续加仓', 'large_realized_result': '大额平仓结果',
    'near_liquidation': '接近清算价', 'actual_liquidation': '实际清算', 'address_divergence': '多空分歧',
    'newly_observed_address_position': '新观察地址快速建仓',
}


def dashboard(store: NewsStore, *, page: int = 0, page_size: int = 30, scenario: str | None = None, dataset: str = 'live', view: str = 'scenarios', address_query: str = '', address_status: str = 'all') -> dict[str, Any]:
    if page < 0 or not 1 <= page_size <= 100:
        raise ValueError('Invalid page')
    if dataset not in ('live', 'test'):
        raise ValueError('Invalid dataset')
    if view not in ('scenarios', 'addresses', 'rules'):
        raise ValueError('Invalid view')
    if not isinstance(address_query, str) or len(address_query) > 100 or address_status not in ('all', 'active', 'blocked'):
        raise ValueError('Invalid address search')
    condition = scenario_condition(scenario) if scenario else '1=1'
    if not store.path.exists():
        return {'initialized': False, 'events': [], 'addresses': [], 'rules': asdict(Rules()), 'labels': KIND_LABELS,
                'last_pass': {}, 'has_next': False, 'page': page, 'counts': {}, 'scenarios': scenario_cards(), 'scenario': scenario, 'dataset': dataset, 'view': view}
    with connect_sqlite(store.path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'hl_news_candidates' not in tables:
            return {'initialized': False, 'events': [], 'addresses': [], 'rules': asdict(Rules()), 'labels': KIND_LABELS,
                    'last_pass': {}, 'has_next': False, 'page': page, 'counts': {}, 'scenarios': scenario_cards(), 'scenario': scenario, 'dataset': dataset, 'view': view}
        dispatch = dataset == 'live' and 'hl_news_dispatch' in tables
        candidate_source = 'hl_news_candidates' if dataset == 'live' else ('hl_news_test_cases' if 'hl_news_test_cases' in tables else '(SELECT NULL AS payload, NULL AS observed_ms, NULL AS event_key WHERE 0)')
        joined = '''LEFT JOIN hl_news_dispatch d ON d.event_key=c.event_key
                    LEFT JOIN tasks t ON t.id=d.task_id LEFT JOIN x_task_pipeline p ON p.task_id=t.id
                    LEFT JOIN prompt_template_versions v ON v.id=p.prompt_version_id''' if dispatch else ''
        columns = '''d.status AS dispatch_status,d.error AS dispatch_error,d.task_id,t.status AS task_status,
                     p.draft_title,p.draft_content,p.final_title,p.final_content,p.writer_model,
                     p.search_result,p.search_completed_at,p.prompt_version_id,v.version_number AS prompt_version_number,p.write_completed_at,p.format_completed_at,p.publish_completed_at,
                     p.push_result,p.last_error,p.publisher_reason_code''' if dispatch else 'NULL AS task_id'
        rows = conn.execute(f'''SELECT c.payload,{columns} FROM {candidate_source} c {joined} WHERE {condition}
                    ORDER BY c.observed_ms DESC,c.event_key LIMIT ? OFFSET ?''', (page_size+1 if view == 'scenarios' else 0, page*page_size)).fetchall()
        count_columns = ','.join(f'SUM(CASE WHEN {scenario_condition(s[0])} THEN 1 ELSE 0 END) AS "{s[0]}"' for s in SCENARIOS)
        scenario_counts = dict(conn.execute(f'SELECT {count_columns} FROM {candidate_source} c').fetchone())
        scenarios = scenario_cards({k: v or 0 for k, v in scenario_counts.items()})
        events = []
        for row in rows[:page_size]:
            item = json.loads(row['payload'])
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
        for row in conn.execute('''SELECT a.*,s.state,s.history,m.payload AS note_payload,b.payload AS block_payload FROM hl_news_addresses a
            LEFT JOIN hl_news_state s ON s.address=a.address
            LEFT JOIN hl_news_metadata m ON m.key='address_note:' || a.address
            LEFT JOIN hl_news_metadata b ON b.key='address_block:' || a.address
            WHERE (instr(lower(a.address),lower(?))>0 OR instr(lower(COALESCE(json_extract(m.payload,'$.note'),'')),lower(?))>0)
            AND (?='all' OR (?='blocked' AND COALESCE(json_extract(b.payload,'$.blocked'),0)=1)
                 OR (?='active' AND COALESCE(json_extract(b.payload,'$.blocked'),0)=0))
            ORDER BY a.last_polled_ms DESC LIMIT ?''', (address_query.strip(), address_query.strip(), address_status, address_status, address_status, 100 if view == 'addresses' else 0)):

            item = dict(row)
            for field in ('sources', 'state', 'history'):
                item[field] = json.loads(item[field]) if item.get(field) else ([] if field == 'sources' else {})
            item['note'] = json.loads(item.pop('note_payload') or '{}').get('note', '')
            item['blocked'] = bool(json.loads(item.pop('block_payload') or '{}').get('blocked'))
            addresses.append(item)
        counts = {name: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                  for name, table in [('addresses', 'hl_news_addresses'), ('events', 'hl_news_candidates')]}
        counts['test_cases'] = conn.execute('SELECT count(*) FROM hl_news_test_cases').fetchone()[0] if 'hl_news_test_cases' in tables else 0
        if dispatch:
            counts['submitted'] = conn.execute("SELECT count(*) FROM hl_news_dispatch WHERE task_id IS NOT NULL AND status!='duplicate'").fetchone()[0]
    return {'initialized': True, 'events': events, 'addresses': addresses, 'rules': asdict(Rules()), 'labels': KIND_LABELS,
            'last_pass': store.metadata('last_pass', {}), 'has_next': len(rows) > page_size, 'page': page,
            'counts': counts, 'scenarios': scenarios, 'scenario': scenario, 'dataset': dataset, 'view': view, 'publication_mode': 'backend_only',
            'production': store.metadata('production_account_profit', None),
            'account_pool': store.account_pool_stats(int(time.time()*1000)) if 'hl_news_account_pool' in tables else {},
            'flow': ['硬规则命中', '内部行为防重', '搜索者（跨来源查重）', '编写者1（独立Prompt）', '编写者2', '固定数据组装', '挂后台']}
