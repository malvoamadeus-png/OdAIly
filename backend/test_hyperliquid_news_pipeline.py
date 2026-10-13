from __future__ import annotations

import json
from datetime import UTC, datetime
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
    assert result.content.startswith('据Odaily监测，')
    assert 'Odaily星球日报讯' not in result.content
    assert A in result.content
    assert result.content.endswith('地址：'+A)
    assert '当前持仓：' not in result.content
    assert source_url(e) == f'https://hyperbot.network/trader/{A}'


def test_hyperliquid_bypasses_judge_search_and_forces_backend_even_if_profiles_enabled():
    e, _ = event()
    task = TaskRecord(id=77, source=HYPERLIQUID_SOURCE, source_item_id=e['key'], source_url=source_url(e),
        title=None, content='event', raw_payload=e, status='publisher_pending', published_at=datetime.now(UTC))
    assert LocalPipelineProcessor._write_flow_sequence(None, task) == ['write','format_publish','publish']
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
    assert task.status=='deduped' and task.metadata['writer_template_key']=='hyperliquid_writer'
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
        conn.commit()
    row=next(item for item in dashboard(store)['events'] if item['pipeline']['task_id']==task_id)
    assert row['pipeline']['prompt_version_number']==1


def test_api_dashboard_route_requires_console_auth():
    from packages.editor_plugin_api import EditorPluginApiHandler
    assert '/console/hyperliquid-news/get' in EditorPluginApiHandler.CONSOLE_PATHS


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


def test_footer_omits_unrelated_liquidation_and_unknown_empty_account_pnl():
    from packages.hyperliquid_news.copy import footer, money
    e,_=event()
    e['evidence']['liquidation_distance']='0.5'
    e['evidence']['history']={'fills':0,'fee_currency_complete':True,'account_closed_pnl_less_fees':'0'}
    text=footer(e)
    assert '距清算价' not in text and '账户已观察' not in text
    assert money('-0.01')=='0.00 万美元'


def test_reader_material_keeps_trigger_not_discovery_and_footer_only_address():
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
    assert 'STRK 交易轮数：10；胜率：70.00%' in footer(e)
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
    assert result['content'].startswith('据Odaily监测，') and result['content'].endswith('地址：'+A)
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
    assert facts['previous_floating_pnl_usd']=='0'
    assert len(facts['change_period'])==2
