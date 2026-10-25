from __future__ import annotations

import json
import pytest
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from packages.common.config import XProcessingSettings
from packages.common.storage import connect_sqlite
from packages.hyperliquid_news.copy import format_news, source_url
from packages.hyperliquid_news.dashboard import dashboard
from packages.hyperliquid_news.detector import evaluate_address
from packages.hyperliquid_news.dispatch import EventDispatcher
from packages.hyperliquid_news.models import Position, Rules
from packages.hyperliquid_news.store import NewsStore
from packages.local_pipeline.processor import LocalPipelineProcessor
from packages.publisher.push_client import PushResult
from packages.x_processing.models import DraftBrief, HYPERLIQUID_SOURCE, PipelineRecord, TaskRecord
from packages.x_processing.repository import InMemoryXProcessingRepository
from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository
from packages.x_processing.worker import XProcessingWorker

A = '0x'+'1'*40


def event(stamp=1000):
    events, state = evaluate_address(address=A, observed_ms=stamp, first_seen_ms=stamp,
        positions={'STRK': Position('STRK', D(100), D(10000000), D(100000), D(2000000), None)},
        previous={'observed_ms': stamp-1, 'positions': {'STRK': Position('STRK', D(50), D(5000000), D(100000), D(0), None).facts()}}, markets={'STRK': {'market_cap': '100000000', 'market_cap_source': 'coingecko', 'oi_notional': '50000000'}},
        history={}, new_fills=[], rules=Rules())
    from dataclasses import asdict
    return asdict(events[0]), state


class Pipeline:
    def __init__(self): self.jobs=[]
    def submit_job(self, **kwargs): self.jobs.append(kwargs)


class Feed:
    def upsert_newsflash(self, **kwargs): pass


class Push:
    def __init__(self): self.calls=[]
    def push(self, **kwargs):
        self.calls.append(kwargs)
        return PushResult(ok=True, status_code=200, response_text='ok')


def test_writer2_fixed_prefix_footer_source_without_model_numbers():
    e, _ = event()
    result = format_news(DraftBrief(title='STRK大额多仓当前浮盈200万美元',
        content='某地址当前在 Hyperliquid 持有 STRK 多仓，名义价值为 1000 万美元，目前浮盈为 200 万美元。'), e)
    assert result.content.startswith('Odaily星球日报讯 据Odaily监测，')
    assert A in result.content
    assert result.content.endswith('地址：'+A)
    assert '当前持仓：' not in result.content
    assert source_url(e) == f'https://hyperbot.network/trader/{A}'


def test_reader_copy_replaces_exchange_terminology():
    from packages.hyperliquid_news.copy import normalize_reader_terms
    text = ('开仓名义金额为约243万美元，平均执行价为81.1299美元，'
            '按78.711美元参考价计仓位估值约236万美元。')
    assert normalize_reader_terms(text) == (
        '开仓金额约243万美元，成交均价为81.13美元，持仓价值约236万美元。')
    assert normalize_reader_terms('按持仓估值参考价格计价值约869.91万美元') == '持仓价值约869.91万美元'
    assert normalize_reader_terms('按标记价 83.049 美元计算，名义价值约830.53万美元') == '持仓价值约830.53万美元'
    assert normalize_reader_terms('按83美元参考价计亏损') == '按83美元参考价计亏损'


def test_account_profit_copy_removes_disclaimer_adds_title_subject_and_uses_fresh_total_pnl():
    event = {
        'addresses': [A], 'coin': 'BTC', 'kinds': ['highlighted_address_action'],
        'observed_ms': 1792673211025,
        'evidence': {
            'highlights': ['official_account_high_profit'],
            'threshold_rules': {'account_profit': '20000000'},
            'history': {
                'official_account_total_pnl': {
                    'source': 'hyperliquid_portfolio',
                    'scope': 'perpAllTime_including_unrealized',
                    'observed_ms': 1792419600060,
                    'pnl': '26931373.1807200015',
                },
                'official_account_performance': {
                    'source': 'hyperliquid_portfolio',
                    'pnl_scope': 'official_portfolio_including_unrealized',
                    'windows': {'allTime': {
                        'scope': 'official_portfolio_window_change',
                        'observed_ms': 1792673210688,
                        'history_sample_ms': 1792673210688,
                        'pnl': '28669569.8838770017',
                    }},
                },
            },
        },
    }
    draft = DraftBrief(
        title='总盈利约2867万美元，平仓BTC空单后加仓约835万美元',
        content=('某地址合约账户累计收益约2867万美元（官方口径含浮盈亏）。'
                 '其在BTC上平仓约16万美元空单，随后加仓约835万美元空单；'
                 '截至监测时，BTC空单持仓价值约1425万美元。'),
    )

    result = format_news(draft, event)

    assert result.title == '总盈利约2,867万美元，某地址平仓BTC空单后加仓约835万美元'
    assert '官方口径含浮盈亏' not in result.content
    assert result.content.endswith(f'地址：{A}；总盈亏：盈利约2,867万美元')


def test_hyperliquid_bypasses_judge_search_and_forces_backend_even_if_profiles_enabled():
    e, _ = event()
    task = TaskRecord(id=77, source=HYPERLIQUID_SOURCE, source_item_id=e['key'], source_url=source_url(e),
        title=None, content='event', raw_payload=e, status='publisher_pending', published_at=datetime.now(UTC))
    assert LocalPipelineProcessor._write_flow_sequence(None, task) == ['search','write','format_publish','publish']
    repo = InMemoryXProcessingRepository(); repo.add_task(task)
    repo.pipelines[77] = PipelineRecord(task_id=77, final_title='STRK大仓监测', final_content='据Odaily监测，某地址持有大额多仓。')
    push = Push()
    worker = XProcessingWorker(stage='publish', repository=repo, settings=XProcessingSettings(dry_run=False), push_client=push)
    worker.feed_writer = Feed()
    worker._run_publish(task)
    assert repo.tasks[77].status == 'ready_review'
    assert repo.pipelines[77].publisher_reason_code == 'hyperliquid_backend_only'
    assert push.calls[0]['is_publish'] is False and push.calls[0]['is_push'] is False
    assert push.calls[0]['source_url'] == source_url(e)


def test_dispatch_durable_task_dedup_enqueue_failure_and_dashboard(tmp_path):
    from dataclasses import replace
    path = tmp_path / 'test.sqlite'
    repo = SQLiteXProcessingRepository(path); repo.init_schema()
    store = NewsStore(path); store.initialize()
    e, state = event()
    from packages.hyperliquid_news.models import Candidate
    candidate = Candidate(**e)
    store.discover(A, source={'kind':'test'}, now_ms=1000)
    store.commit_observation(address=A, now_ms=1000, state=state, history={}, coverage={}, fills=[], candidates=[candidate])
    client=Pipeline(); dispatcher=EventDispatcher(store,pipeline_client=client)
    result=dispatcher.dispatch(limit=1)
    task_id=result[0]['task_id']
    assert result[0]['status']=='queued'
    task=repo.get_task(task_id)
    assert task.status=='judged' and task.metadata['writer_template_key']=='hyperliquid_position_writer'
    assert dispatcher.dispatch(limit=1)==[]
    # Replaying the same state evidence at another observation time is still the same fact.
    another=replace(candidate,key='repeat',observed_ms=2000)
    store.commit_observation(address=A, now_ms=2000, state=state, history={}, coverage={}, fills=[], candidates=[another])
    assert dispatcher.dispatch(limit=1)[0]['status']=='duplicate'
    assert len(client.jobs)==1
    data=dashboard(store)
    assert data['counts']['events']==2 and data['counts']['submitted']==1
    assert any(item['pipeline']['task_id']==task_id for item in data['events'])
    from packages.common.paths import get_paths
    repo.seed_prompt_templates(root_dir=get_paths().root_dir)
    prompt=repo.get_active_prompt('hyperliquid_writer')
    repo.ensure_pipeline(task_id)
    with connect_sqlite(path) as conn:
        conn.execute('UPDATE x_task_pipeline SET prompt_version_id=? WHERE task_id=?',(prompt.id,task_id))
        conn.execute("UPDATE x_task_pipeline SET final_title='账户高盈利地址操作',final_content='某地址本月收益明显增加。' WHERE task_id=?",(task_id,))
        conn.commit()
    row=next(item for item in dashboard(store)['events'] if item['pipeline']['task_id']==task_id)
    assert row['pipeline']['prompt_version_number']==1
    timeline_row=next(item for item in dashboard(store,view='timeline')['events'] if item['key']==candidate.key)
    assert timeline_row['pipeline']['final_title']=='账户高盈利地址操作'
    assert timeline_row['pipeline']['final_content']=='某地址本月收益明显增加。'


def test_api_dashboard_route_requires_console_auth():
    from packages.editor_plugin_api import EditorPluginApiHandler
    assert '/console/hyperliquid-news/get' in EditorPluginApiHandler.CONSOLE_PATHS
    assert '/console/hyperliquid-news/address-add' in EditorPluginApiHandler.CONSOLE_PATHS


def test_dedicated_prompt_seed_and_writer_input(tmp_path):
    from packages.common.paths import get_paths
    from packages.x_processing.worker import build_structured_writer_prompt
    repo=SQLiteXProcessingRepository(tmp_path/'test.sqlite'); repo.init_schema()
    repo.seed_prompt_templates(root_dir=get_paths().root_dir)
    prompt=repo.get_active_prompt('hyperliquid_writer')
    assert '不需要重新判断新闻价值' in prompt.content
    e,_=event()
    task=TaskRecord(id=1,source=HYPERLIQUID_SOURCE,source_item_id='1',source_url=None,title=None,content='event',raw_payload=e)
    built=build_structured_writer_prompt(task=task,prompt=prompt,known_subjects=[])
    assert '结构化事件证据' in built and 'STRK' in built
    assert 'X平台' not in built


def test_dispatch_enqueue_failure_reuses_task_on_retry(tmp_path):
    from packages.hyperliquid_news.models import Candidate
    repo=SQLiteXProcessingRepository(tmp_path/'db.sqlite'); repo.init_schema()
    store=NewsStore(repo.path); store.initialize()
    e,state=event(); store.discover(A,source={'kind':'test'},now_ms=1000)
    store.commit_observation(address=A,now_ms=1000,state=state,history={},coverage={},fills=[],candidates=[Candidate(**e)])
    class Bad:
        def submit_job(self,**kwargs): raise RuntimeError('offline')
    first=EventDispatcher(store,pipeline_client=Bad()).dispatch(limit=1)[0]
    client=Pipeline(); second=EventDispatcher(store,pipeline_client=client).dispatch(limit=1)[0]
    assert first['status']=='enqueue_failed' and second['status']=='queued'
    assert first['task_id']==second['task_id']


def test_footer_omits_unrelated_liquidation_and_unknown_account_pnl():
    from packages.hyperliquid_news.copy import footer, money
    e,_=event()
    e['evidence']['liquidation_distance']='0.5'
    e['evidence']['history']={'fills':0,'fee_currency_complete':True,'account_closed_pnl_less_fees':'0'}
    text=footer(e)
    assert '距清算价' not in text and '账户已观察' not in text
    assert money('-0.01')=='约0 万美元'


def test_reader_material_keeps_trigger_not_discovery_and_footer_omits_interval_pnl():
    from packages.hyperliquid_news.copy import footer, writer_material
    e,_=event()
    e['kinds']=['large_position']
    e['evidence']['history']={'fills':0,'account_closed_pnl_less_fees':'0',
        'official_account_performance':{'windows':{'week':{'pnl':'-11854548','rank':2}}},
        'assets':{'STRK':{'closed_pnl_less_fees':'-2263209','rounds':0,'win_rate':None}}}
    e['evidence']['highlights']=['historical_loss']
    material=writer_material(e)
    text=json.dumps(material,ensure_ascii=False,default=str)
    assert 'official_account_performance' not in text and '-11854548' not in text
    assert '-2263209' not in text and 'first_observation' not in text
    assert 'floating_pnl_usd' not in text
    assert footer(e)=='地址：'+A


def test_empty_trade_history_is_unknown_not_zero():
    from packages.hyperliquid_news.history import summarize
    assert summarize([],start_ms=0,end_ms=1000)['account_closed_pnl_less_fees'] is None


def test_prompt_full_activation_preserves_history_and_is_idempotent(tmp_path):
    from packages.common.paths import get_paths
    from packages.hyperliquid_news.commands import activate_writer_prompt
    repo=SQLiteXProcessingRepository(tmp_path/'db.sqlite'); repo.init_schema()
    repo.seed_prompt_templates(root_dir=get_paths().root_dir)
    before=repo.get_active_prompt('hyperliquid_writer')
    with connect_sqlite(repo.path) as conn:
        conn.execute('UPDATE prompt_template_versions SET content=? WHERE id=?', ('old conflicting copy',before.id))
        conn.commit()
    result=activate_writer_prompt(repo.path,get_paths().root_dir)
    assert result=={'version_number':2,'changed':True}
    after=repo.get_active_prompt('hyperliquid_writer')
    assert after.id!=before.id and 'old conflicting copy' not in after.content
    with connect_sqlite(repo.path) as conn:
        assert conn.execute('SELECT content FROM prompt_template_versions WHERE id=?',(before.id,)).fetchone()[0]=='old conflicting copy'
    assert activate_writer_prompt(repo.path,get_paths().root_dir)=={'version_number':2,'changed':False}


def test_prompt_activation_supports_scene_specific_templates(tmp_path):
    from packages.common.paths import get_paths
    from packages.hyperliquid_news.commands import activate_writer_prompt
    repo=SQLiteXProcessingRepository(tmp_path/'db.sqlite'); repo.init_schema()
    repo.seed_prompt_templates(root_dir=get_paths().root_dir)
    before=repo.get_active_prompt('hyperliquid_asset_performance_writer')
    with connect_sqlite(repo.path) as conn:
        conn.execute('UPDATE prompt_template_versions SET content=? WHERE id=?', ('outdated scene prompt',before.id))
        conn.commit()
    result=activate_writer_prompt(repo.path,get_paths().root_dir,
                                 template_key='hyperliquid_asset_performance_writer')
    assert result=={'version_number':2,'changed':True}
    active=repo.get_active_prompt('hyperliquid_asset_performance_writer')
    expected=(get_paths().root_dir/'docs/OdAIly代码与整体设计/Hyperliquid资产表现Prompt.txt').read_text(encoding='utf-8')
    assert active.content==expected


def test_liquidation_reader_fact_uses_actual_price_and_adverse_direction():
    from packages.hyperliquid_news.copy import writer_material
    e,_=event(); e['kinds']=['large_loss']
    e['evidence']['position'].update(size='-100005',mark='83.049',liquidation='132.4500168714')
    facts=writer_material(e)['facts']
    assert facts['liquidation_price_usd']=='132.4500168714'
    assert facts['price_move_to_liquidation']=='上涨'
    assert abs(D(facts['price_move_to_liquidation_percent'])-D('59.48418'))<D('0.00001')
    assert 'mark' not in facts['position']


def test_single_asset_win_rate_supplement_only_for_highlight_action():
    from packages.hyperliquid_news.copy import footer,writer_material
    e,_=event();e['kinds']=['highlighted_address_action']
    e['evidence']['highlights']=['asset_high_win_rate']
    e['evidence']['history']={'assets':{'STRK':{'rounds':10,'win_rate':'0.7'}},
                            'first_observed_fill_ms':1000,'last_observed_fill_ms':2000}
    assert '交易轮数' not in footer(e) and '统计时间' not in footer(e)
    assert writer_material(e)['facts']['historical_highlights']['asset_win_rate']=='0.7'
    e['kinds']=['large_position']
    assert footer(e)=='地址：'+A


def test_editorial_preview_uses_real_writer_format_and_preserves_original(tmp_path):
    from packages.common.paths import get_paths
    from packages.hyperliquid_news.preview import generate_preview
    repo=SQLiteXProcessingRepository(tmp_path/'db.sqlite');repo.init_schema()
    repo.seed_prompt_templates(root_dir=get_paths().root_dir)
    e,_=event()
    task=TaskRecord(id=1,source=HYPERLIQUID_SOURCE,source_item_id=e['key'],source_url=source_url(e),
                    title=None,content='event',raw_payload=e,status='ready_review')
    class AI:
        def generate_text(self,**kwargs):
            assert 'trigger_rules' in kwargs['prompt']
            return json.dumps({'title':'某地址持有1000万美元STRK多单',
                'content':'某地址当前持有1000万美元STRK多单，浮盈200万美元。',
                'title_strategy':'plain','title_strategy_reason':'持仓命中',
                'matched_title_rules':['plain_direct'],'feature_mode_applied':False,'feature_mode_reason':''})
    result=generate_preview(task=task,prompt=repo.get_active_prompt('hyperliquid_writer'),
                            settings=XProcessingSettings(),ai_client=AI())
    assert result['mode']=='preview_only' and result['prompt_version_number']==1
    assert result['content'].startswith('Odaily星球日报讯 据Odaily监测，') and result['content'].endswith('地址：'+A)
    assert task.status=='ready_review'
    with connect_sqlite(repo.path) as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==0


def test_legacy_first_observation_is_not_dispatchable_news(tmp_path):
    from packages.hyperliquid_news.models import Candidate
    repo=SQLiteXProcessingRepository(tmp_path/'db.sqlite');repo.init_schema()
    store=NewsStore(repo.path);store.initialize()
    e,state=event();e['evidence'].pop('news_change',None)
    e['evidence']['first_observation']=True;e['evidence']['previous_position']=None
    store.discover(A,source={'kind':'test'},now_ms=1000)
    store.commit_observation(address=A,now_ms=1000,state=state,history={},coverage={},fills=[],candidates=[Candidate(**e)])
    client=Pipeline()
    result=EventDispatcher(store,pipeline_client=client).dispatch(limit=1)
    assert result[0]['status']=='no_change_evidence' and client.jobs==[]
    assert dashboard(store)['events'][0]['news_eligibility']['eligible'] is False
    # An old enqueue retry must also be blocked, rather than returned early.
    with connect_sqlite(repo.path) as conn:
        conn.execute("UPDATE hl_news_dispatch SET status='enqueue_failed'")
        conn.commit()
    assert EventDispatcher(store,pipeline_client=client).dispatch(limit=1)[0]['status']=='no_change_evidence'
    assert client.jobs==[]
    with connect_sqlite(repo.path) as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==0


def test_change_material_includes_before_after_not_only_current_state():
    from packages.hyperliquid_news.copy import writer_material
    e,_=event()
    facts=writer_material(e)['facts']
    assert facts['previous_position']['position_value_usd']=='5000000'
    assert 'previous_floating_pnl_usd' not in facts
    assert len(facts['change_period'])==2


@pytest.mark.parametrize('reference_type', ['odaily_reference','candidate','recent_processed'])
@pytest.mark.parametrize('same_event', [True,False])
def test_shared_searcher_reviews_lookonchain_fact_and_releases_later_actions(tmp_path,monkeypatch,reference_type,same_event):
    import math
    from packages.x_processing.searcher import SearchDocument
    import packages.x_processing.worker as worker_module
    monkeypatch.setattr(worker_module,'_search_cache_path_for_repository',lambda _:tmp_path/'search.sqlite')
    repo=SQLiteXProcessingRepository(tmp_path/'db.sqlite');repo.init_schema()
    store=NewsStore(repo.path);store.initialize()
    now=datetime.now(UTC)
    e,state=event(int(now.timestamp()*1000))
    from packages.hyperliquid_news.models import Candidate
    store.discover(A,source={'kind':'test'},now_ms=e['observed_ms'])
    store.commit_observation(address=A,now_ms=e['observed_ms'],state=state,history={},coverage={},fills=[],candidates=[Candidate(**e)])
    result=EventDispatcher(store,pipeline_client=Pipeline()).dispatch(limit=1)[0]
    task=repo.get_task(result['task_id'])
    class Embeddings:
        cache=None
        def embed_one(self,**kwargs):
            assert 'STRK' in kwargs['text'] and 'threshold_rules' not in kwargs['text']
            return [1.,0.]
        def embed_documents(self,documents):return [(d,[.99,math.sqrt(1-.99**2)]) for d in documents]
    class AI:
        calls=[]
        def generate_text(self,**kwargs):
            self.calls.append(kwargs['prompt'])
            assert 'Lookonchain' in kwargs['prompt']
            return json.dumps({'is_duplicate':same_event,'duplicate_target_type':
                {'odaily_reference':'odaily_published','candidate':'inflight_candidate','recent_processed':'recent_processed'}[reference_type] if same_event else 'none',
                'duplicate_target_id':'reference' if same_event else '',
                'reason':'same_event' if same_event else 'update_of_existing_event'})
    ai=AI()
    worker=XProcessingWorker(stage='search',repository=repo,settings=XProcessingSettings(),
        search_embedding_service=Embeddings(),search_ai_client=ai)
    document=SearchDocument(doc_type=reference_type,doc_id='reference',title='Lookonchain：某地址加仓STRK',
        content='Lookonchain报道：'+('该地址刚加仓至1000万美元STRK多单。' if same_event else '该地址昨日持有500万美元STRK多单。'),
        source='x',source_url=source_url(e),published_at=now,
        metadata={'hyperliquid_event_key':'older'} if reference_type!='odaily_reference' else {},
        task_id=999,expires_at=now+timedelta(hours=1),status='active')
    worker._load_odaily_reference_documents=lambda **_: [document] if reference_type=='odaily_reference' else []
    worker._load_active_candidate_documents=lambda **_: [document] if reference_type=='candidate' else []
    worker._load_recent_processed_documents=lambda **_: [document] if reference_type=='recent_processed' else []
    worker._run_search(task)
    assert len(ai.calls)==1  # .99 similarity must not auto-block later actions.
    assert repo.get_task(task.id).status==('duplicate' if same_event else 'deduped')
    search=repo.get_pipeline(task.id)
    assert search.search_completed_at is not None
    assert search.search_result['is_duplicate'] is same_event


def test_same_hyperbot_profile_or_generic_heading_is_not_exact_duplicate():
    from packages.x_processing.searcher import SearchDocument,exact_duplicate_decision
    query=SearchDocument(doc_type='task',doc_id='new',title='ETH 大仓重要加减仓',
        content='今日加仓至1亿美元',source='hyperliquid_news',source_url='https://hyperbot.network/trader/'+A)
    old=SearchDocument(doc_type='candidate',doc_id='old',title=query.title,
        content='昨日减仓至5000万美元',source='x',source_url=query.source_url)
    assert exact_duplicate_decision(query=query,documents=[old],target_type='inflight_candidate') is None


def test_dashboard_fixed_scenarios_without_data(tmp_path):
    from packages.hyperliquid_news.scenarios import PRODUCTION_SCENARIOS, scenario_cards
    store = NewsStore(tmp_path / 'empty.sqlite')
    data = dashboard(store, scenario='giveback')
    assert not data['initialized'] and len(data['scenarios']) == 10
    assert {s['key'] for s in data['scenarios']} == set(PRODUCTION_SCENARIOS)
    assert [s['key'] for s in data['scenarios']] == [s['key'] for s in scenario_cards(dataset='live')]
    assert all(s['count'] == 0 for s in data['scenarios'])
    assert not store.path.exists()  # Reading an empty dashboard must not create state.
    with pytest.raises(ValueError, match='Unknown Hyperliquid scenario'):
        dashboard(store, scenario="x' OR 1=1")


def test_dashboard_scenario_counts_and_pagination_are_not_global(tmp_path):
    from packages.hyperliquid_news.store import encode
    path = tmp_path / 'scenarios.sqlite'
    SQLiteXProcessingRepository(path).init_schema()
    store = NewsStore(path)
    store.initialize()
    e, _ = event()
    # Newer records from other directions must not hide older matching records.
    samples = [
        ('win-old', 1, ['highlighted_address_action'], ['asset_high_win_rate']),
        ('win-new', 2, ['highlighted_address_action'], ['asset_high_win_rate', 'asset_high_profit']),
        ('account', 3, ['highlighted_address_action'], ['official_account_high_profit']),
        ('loss', 4, ['highlighted_address_action'], ['historical_loss']),
        ('position', 5, ['large_position', 'oi_share', 'large_position_action'], []),
        ('giveback', 6, ['profit_giveback', 'profit_to_loss'], []),
    ]
    with connect_sqlite(path) as conn:
        for key, stamp, kinds, highlights in samples:
            item = {**e, 'key': key, 'observed_ms': stamp, 'kinds': kinds,
                    'evidence': {**e['evidence'], 'highlights': highlights}}
            conn.execute('INSERT INTO hl_news_candidates VALUES(?,?,?,?)', (key, stamp, e['coin'], encode(item)))
    first = dashboard(store, scenario='asset_win_rate', page_size=1)
    assert [e['key'] for e in first['events']] == ['win-new'] and first['has_next']
    counts = {s['key']: s['count'] for s in first['scenarios']}
    assert counts == dict(large_pnl=0, asset_win_rate=2, account_profit=1,
                         historical_loss=1, reversal=0, loss_addition=0, realized=0,
                         asset_profit=1, giveback=1, liquidation=0)
    second = dashboard(store, scenario='asset_win_rate', page_size=1, page=1)
    assert [e['key'] for e in second['events']] == ['win-old'] and not second['has_next']
    assert dashboard(store, scenario='divergence')['events'] == []
    assert first['counts']['events'] == 6  # One record can belong to several directions.


def test_scenario_dashboard_does_not_transfer_hidden_address_history(tmp_path):
    path = tmp_path/'offline.sqlite'
    repo = SQLiteXProcessingRepository(path); repo.init_schema()
    store = NewsStore(path); store.initialize()
    store.discover(A, source={'kind': 'test'}, now_ms=1000)
    e, state = event()
    store.commit_observation(address=A, now_ms=1000, state=state,
        history={'assets': {'ETH': {'completed_rounds': ['history'*1000]*100}}},
        coverage={}, fills=[], candidates=[])
    data = dashboard(store, scenario='account_profit')
    assert data['addresses'] == []
    assert len(json.dumps(data, default=str).encode()) < 20000
    addresses = dashboard(store, view='addresses')
    assert 'sources' not in addresses['addresses'][0]
    assert 'state' not in addresses['addresses'][0] and 'history' not in addresses['addresses'][0]
    assert addresses['events'] == []
    assert dashboard(store, view='rules')['addresses'] == []
    with pytest.raises(ValueError): dashboard(store, view='unknown')


def test_hyperliquid_http_compresses_large_responses_and_caches_preflight():
    import gzip
    from io import BytesIO
    from http import HTTPStatus
    from types import SimpleNamespace
    from packages.editor_plugin_api import EditorPluginApiHandler
    class Handler:
        path = '/console/hyperliquid-news/get'
        def __init__(self, encoding):
            self.headers = {'Accept-Encoding': encoding}
            self.wfile = BytesIO(); self.sent = {}
            self.server = SimpleNamespace(service=SimpleNamespace(api_settings=SimpleNamespace(cors_allow_origin='*')))
        def send_response(self, status): self.status = status
        def send_header(self, name, value): self.sent[name] = value
        def end_headers(self): pass
        def _json_default(self, value): return str(value)
        def _send_common_headers(self): EditorPluginApiHandler._send_common_headers(self)
    payload = {'data': '重复地址证据'*10000}
    h = Handler('br, gzip')
    EditorPluginApiHandler._send_json(h, HTTPStatus.OK, payload)
    assert h.sent['Content-Encoding'] == 'gzip'
    assert len(h.wfile.getvalue()) < 2000
    assert json.loads(gzip.decompress(h.wfile.getvalue())) == payload
    for encoding in ('', 'gzip;q=0', 'gzip;q=invalid'):
        plain = Handler(encoding)
        EditorPluginApiHandler._send_json(plain, HTTPStatus.OK, payload)
        assert 'Content-Encoding' not in plain.sent
        assert json.loads(plain.wfile.getvalue()) == payload
    EditorPluginApiHandler.do_OPTIONS(h)
    assert h.sent['Access-Control-Max-Age'] == '600'
    other = Handler('gzip');other.path='/console/other'
    EditorPluginApiHandler._send_json(other, HTTPStatus.OK, payload)
    assert 'Content-Encoding' not in other.sent


def test_account_copy_corrects_model_wording_with_alltime_evidence():
    e, _ = event()
    e['kinds'] = ['highlighted_address_action']
    e['evidence']['highlights'] = ['official_account_high_profit']
    e['evidence']['history'] = {'official_account_performance': {'windows': {
        'allTime': {'pnl': '173488802.47'}}}}
    draft = DraftBrief(title='合约账户累计收益超1.73亿美元的地址加仓约459万美元BTC空单',
        content='该地址合约账户累计收益超过1.73亿美元。该地址随后加仓BTC空单。')
    result = format_news(draft, e)
    assert result.title == '总盈利超1.73亿美元地址加仓约459万美元BTC空单'
    assert result.content.startswith('Odaily星球日报讯 据Odaily监测，某地址合约账户')
    assert '。其随后' in result.content
    assert format_news(result, e).title == result.title
    e['evidence']['history']['official_account_performance']['windows'] = {'month': {'pnl': '21000000'}}
    finite = format_news(DraftBrief(title='近30天盈利超2000万美元地址加仓BTC空单', content='该地址近30天收益超过2000万美元。'), e)
    assert finite.title.startswith('近30天盈利')
    assert finite.content.startswith('Odaily星球日报讯 据Odaily监测，某地址')


def test_address_notes_are_persistent_literal_and_inserted_once(tmp_path):
    path = tmp_path/'offline.sqlite'
    SQLiteXProcessingRepository(path).init_schema()
    store = NewsStore(path);store.initialize()
    store.discover(A, source={'kind': 'manual'}, now_ms=1000)
    store.save_address_note(A, 'Wintermute做市地址', updated_by='editor@example.test')
    e, _ = event();e['kinds']=['highlighted_address_action'];e['evidence']['highlights']=['official_account_high_profit']
    e['evidence']['history']={'official_account_performance':{'windows':{'allTime':{'pnl':'173000000'}}}}
    e=store.with_address_notes(e)
    result=format_news(DraftBrief(title='总盈利超1亿美元地址加仓BTC空单', content='某地址合约账户累计收益超过1亿美元。其加仓BTC空单。'),e)
    assert result.content.startswith('Odaily星球日报讯 据Odaily监测，某地址合约账户（Wintermute做市地址）累计收益')
    assert format_news(result,e).content.count('Wintermute做市地址')==1
    assert dashboard(store,view='addresses')['addresses'][0]['note']=='Wintermute做市地址'
    for invalid in ('a\nb','名字（嵌套）','x'*81):
        with pytest.raises(ValueError):store.save_address_note(A,invalid,updated_by='editor')
    store.save_address_note(A,'',updated_by='editor')
    assert store.with_address_notes(e)['evidence']['address_notes'][A]==''
    assert 'Wintermute' not in format_news(result,store.with_address_notes(e)).content


def test_address_search_covers_full_pool_and_notes(tmp_path):
    path = tmp_path/'search.sqlite'
    SQLiteXProcessingRepository(path).init_schema()
    store = NewsStore(path); store.initialize()
    store.discover(A,source={'kind':'test'},now_ms=1000)
    store.save_address_note(A,'Wintermute做市地址',updated_by='test')
    for i in range(101):
        address = '0x'+f'{i:040x}'
        store.discover(address,source={'kind':'test'},now_ms=2000)
        with connect_sqlite(path) as conn:
            conn.execute('UPDATE hl_news_addresses SET last_polled_ms=2000 WHERE address=?',(address,))
    assert A not in [a['address'] for a in dashboard(store,view='addresses')['addresses']]
    assert dashboard(store,view='addresses',address_query='winterMUTE')['addresses'][0]['address']==A
    assert dashboard(store,view='addresses',address_query=A.upper())['addresses'][0]['address']==A
    store.save_address_block(A,True,updated_by='test')
    assert dashboard(store,view='addresses',address_status='blocked')['addresses'][0]['address']==A
    assert not dashboard(store,view='addresses',address_query=A,address_status='active')['addresses']


def test_hyperliquid_timeline_defaults_to_recent_ten_and_uses_address_index(tmp_path):
    from packages.hyperliquid_news.models import Candidate
    path = tmp_path / 'timeline.sqlite'
    SQLiteXProcessingRepository(path).init_schema()
    store = NewsStore(path); store.initialize()
    for stamp in range(1, 13):
        payload, _ = event(stamp * 1000)
        payload['key'] = f'timeline-{stamp:02d}'
        if stamp == 12:
            payload['kinds'] = ['highlighted_address_action']
            payload['evidence']['highlights'] = ['official_account_high_profit']
        store.save_global(f'global-{stamp}', {}, [Candidate(**payload)])

    recent = dashboard(store, view='timeline')
    assert len(recent['events']) == 10
    assert [item['key'] for item in recent['events']] == [f'timeline-{stamp:02d}' for stamp in range(12, 2, -1)]
    assert recent['has_next'] is True
    assert 'evidence' not in recent['events'][0]
    assert recent['events'][0]['scenario_keys'] == ['account_profit']

    searched = dashboard(store, view='timeline', address_query=A)
    assert len(searched['events']) == 10
    prefix = dashboard(store, view='timeline', address_query=A[:8])
    assert len(prefix['events']) == 10
    with connect_sqlite(path) as conn:
        plan = conn.execute('''EXPLAIN QUERY PLAN
            SELECT e.event_key,e.observed_ms,e.coin,e.kinds,e.addresses
            FROM hl_news_candidate_addresses a
            JOIN hl_news_candidate_timeline e ON e.event_key=a.event_key
            WHERE a.address_lower=? ORDER BY a.observed_ms DESC,a.event_key ASC LIMIT 11''', (A,)).fetchall()
    assert any('hl_news_candidate_addresses_address_time' in row['detail'] for row in plan)
    assert not any('TEMP B-TREE' in row['detail'] for row in plan)


def test_hyperliquid_address_list_omits_raw_monitoring_evidence(tmp_path):
    path = tmp_path / 'compact-addresses.sqlite'
    SQLiteXProcessingRepository(path).init_schema()
    store = NewsStore(path); store.initialize()
    store.discover(A, source={'kind': 'test'}, now_ms=1000)
    with connect_sqlite(path) as conn:
        conn.execute('UPDATE hl_news_addresses SET sources=? WHERE address=?',
                     (json.dumps([{'kind': 'private-shaped-evidence', 'payload': 'x' * 5000}]), A))
    address = dashboard(store, view='addresses')['addresses'][0]
    assert 'sources' not in address and 'state' not in address and 'history' not in address


def test_manual_hyperliquid_address_is_added_to_monitored_pool(tmp_path):
    path = tmp_path / 'manual-address.sqlite'
    SQLiteXProcessingRepository(path).init_schema()
    store = NewsStore(path); store.initialize()
    added = store.add_manual_address(A, now_ms=1234, updated_by='operator@example.test')
    assert added['created'] is True
    assert store.addresses(10)[0]['address'] == A
    assert json.loads(store.addresses(10)[0]['sources'])[0]['kind'] == 'manual'
    assert store.account_due(1234, 10)[0]['address'] == A
    assert store.add_manual_address(A, now_ms=2000, updated_by='operator@example.test')['created'] is False


def test_blocked_address_cannot_rediscover_monitor_dispatch_or_publish(tmp_path, monkeypatch):
    from packages.hyperliquid_news.models import Candidate
    path = tmp_path/'block.sqlite'
    repo=SQLiteXProcessingRepository(path);repo.init_schema()
    store=NewsStore(path);store.initialize()
    store.discover(A,source={'kind':'test'},now_ms=1000)
    store.schedule_account(A,now_ms=1000,leaderboard_qualified=True)
    e,state=event()
    store.commit_observation(address=A,now_ms=1000,state=state,history={},coverage={},fills=[],candidates=[Candidate(**e)])
    client=Pipeline(); dispatcher=EventDispatcher(store,pipeline_client=client)
    result=dispatcher.dispatch(limit=1)[0]
    task=repo.get_task(result['task_id'])
    repo.complete_format_publish(task.id,final_title='测试',final_content='测试正文')
    store.save_address_block(A,True,updated_by='test')
    assert repo.get_task(task.id).status=='discarded'
    assert store.blocked(A)
    assert not store.discover(A,source={'kind':'leaderboard'},now_ms=2000)
    store.schedule_account(A,now_ms=2000,leaderboard_qualified=True)
    assert not store.account_due(3000,10) and not store.addresses(100)
    later,later_state=event(2000)
    store.commit_observation(address=A,now_ms=2000,state=later_state,history={},coverage={},fills=[],candidates=[Candidate(**later)])
    store.save_global('test',{},[Candidate(**later)])
    assert len(store.candidates())==1
    assert dispatcher._create_task(later)['status']=='address_blocked'
    push=Push()
    monkeypatch.setattr('packages.x_processing.worker.LocalEditorPluginFeedWriter', Feed)
    worker=XProcessingWorker(stage='publish',repository=repo,settings=XProcessingSettings(),push_client=push)
    worker._process_task(task)
    # The direct pre-push path must also stop a task already picked up by a worker.
    worker._run_publish(task)
    assert not push.calls
    store.save_address_block(A,False,updated_by='test')
    assert not store.blocked(A) and store.load(A) is None
    assert store.event_blocked(e)  # Never replay retired events after unblocking.
    stamp=store.metadata('address_block:'+A,{})['last_blocked_ms']+1
    fresh,_=event(stamp)
    assert not store.event_blocked(fresh)
    assert store.discover(A,source={'kind':'leaderboard'},now_ms=stamp)
    store.schedule_account(A,now_ms=stamp)
    assert store.account_due(stamp,10)[0]['address']==A
