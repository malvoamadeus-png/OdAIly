import { useEffect, useState } from 'react';
import { Activity, ArrowRight, ChevronRight, Database, FileText, Layers, RefreshCw, ShieldCheck } from 'lucide-react';
import { getHyperliquidNewsDashboard, saveHyperliquidAddressNote, saveHyperliquidAddressBlock, type HyperliquidNewsDashboard, type HyperliquidNewsEvent } from './xCaptureStore';

function money(value: unknown): string {
  if (value === null || value === undefined) return '未取得';
  const number = Number(value);
  return Number.isFinite(number) ? `${(number / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 2 })} 万美元` : '未取得';
}
function time(value: unknown): string {
  if (!value) return '—';
  return new Date(typeof value === 'number' ? value : String(value)).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
}
const highlightNames: Record<string, string> = { asset_high_win_rate: '单资产高胜率', asset_high_profit: '单资产历史高盈利', account_high_profit: '账户历史高盈利', official_account_high_profit: '账户官方周期高收益', historical_loss: '账户总亏损高' };
const statuses: Record<string, string> = { judged: '待搜索者查重', deduping: '搜索者查重中', deduped: '待撰写', search_failed: '搜索者查重失败', writing: '编写者1处理中', written: '草稿完成',
  formatting: '编写者2处理中', publisher_pending: '待挂后台', publishing: '挂后台中', ready_review: '已挂后台',
  write_failed: '撰写失败', format_failed: '定稿失败', publisher_failed: '挂后台失败', duplicate: '重复内容或行为',
  no_change_evidence: '仅监测基线，无变化证据', queued: '已进入写作队列', enqueue_failed: '入队失败', created: '等待入队' };

function Evidence({ event }: { event: HyperliquidNewsEvent }) {
  const e = event.evidence, p = e.position, history = e.history || {}, asset = history.assets?.[event.coin];
  const peak = e.episode_peak_unrealized_pnl;
  return <>
    <dl className="hlFacts">
      <div><dt>目标地址</dt><dd>{event.addresses.map(a => <a key={a} href={`https://hyperbot.network/trader/${a}`} target="_blank" rel="noreferrer">{a}<br /></a>)}</dd></div>
      <div><dt>合约</dt><dd>{event.coin}</dd></div>
      {e.previous_position && <><div><dt>此前持仓</dt><dd>{Number(e.previous_position.size) > 0 ? '多仓' : '空仓'} · {money(e.previous_position.notional)}</dd></div><div><dt>此前浮盈亏</dt><dd>{money(e.previous_position.unrealized_pnl)}</dd></div></>}
      {e.net_position_change && <div><dt>数量变化对应金额</dt><dd>{e.net_position_change.direction} · {money(e.net_position_change.estimated_value)}</dd></div>}
      {e.previous_reported_floating_pnl !== undefined && <div><dt>上次播报浮盈亏</dt><dd>{money(e.previous_reported_floating_pnl)} · {time(e.previous_reported_floating_pnl_ms)}</dd></div>}
      {p && <><div><dt>当前持仓</dt><dd>{Number(p.size) > 0 ? '多仓' : '空仓'} · {money(p.notional)}</dd></div>
        <div><dt>当前浮盈亏</dt><dd>{money(p.unrealized_pnl)}</dd></div></>}
      {e.market && <><div><dt>代币市值</dt><dd>{money(e.market.market_cap)} · {e.market.market_cap_source || '尚无来源'}</dd></div>
        <div><dt>持仓相当于市值</dt><dd>{e.ratios?.market_cap_share ? `${(Number(e.ratios.market_cap_share) * 100).toFixed(2)}%` : '未取得'}</dd></div>
        <div><dt>合约未平仓名义金额</dt><dd>{money(e.market.oi_notional)}</dd></div>
        <div><dt>占未平仓量</dt><dd>{e.ratios?.oi_share ? `${(Number(e.ratios.oi_share) * 100).toFixed(2)}%` : '未取得'}</dd></div></>}
      {peak !== undefined && <><div><dt>监测期间浮盈峰值</dt><dd>{money(peak)} · {time(e.episode_peak_ms)}</dd></div>
        <div><dt>回吐金额 / 比例</dt><dd>{money(Number(peak) - Number(p?.unrealized_pnl))} / {Number(peak) > 0 ? `${((Number(peak) - Number(p?.unrealized_pnl)) / Number(peak) * 100).toFixed(2)}%` : '—'}</dd></div></>}
      {asset && <><div><dt>单资产统计</dt><dd>{asset.rounds} 个完整交易轮 · 胜率 {asset.win_rate != null ? `${(Number(asset.win_rate) * 100).toFixed(2)}%` : '未取得'}</dd></div>
        <div><dt>单资产已实现盈亏</dt><dd>{money(asset.closed_pnl_less_fees)}（不含资金费）</dd></div>
        <div><dt>实际成交样本范围</dt><dd>{time(history.first_observed_fill_ms)} 至 {time(history.last_observed_fill_ms)}</dd></div></>}
      {history.official_account_loss?.windows?.allTime && <div><dt>合约账户总亏损（含浮盈亏）</dt><dd>{money(-Number(history.official_account_loss.windows.allTime.pnl))} · {time(history.official_account_loss.windows.allTime.observed_ms)}</dd></div>}
      {history.account_closed_pnl_less_fees != null && <div><dt>账户历史已实现盈亏</dt><dd>{money(history.account_closed_pnl_less_fees)}</dd></div>}
      {history.official_account_performance && Object.entries(history.official_account_performance.windows).map(([window, data]) => <div key={window}><dt>官方周期 PnL · {{ day: '近1天', week: '近7天', month: '近30天', allTime: '全部时间' }[window] || window}</dt><dd>{money((data as any).pnl)}{(data as any).rank != null ? ` · 排名 ${(data as any).rank}` : ''} · {time((data as any).observed_ms)}</dd></div>)}
      {e.actions?.liquidated_notional && Number(e.actions.liquidated_notional) > 0 && <div><dt>实际清算成交</dt><dd>{money(e.actions.liquidated_notional)}</dd></div>}
      {e.actions && <><div><dt>本次取得开 / 平仓成交</dt><dd>{money(e.actions.open_notional)} / {money(e.actions.close_notional)}</dd></div><div><dt>本次成交已实现盈亏</dt><dd>{money(e.actions.closed_pnl)}</dd></div></>}
      {p?.liquidation && <div><dt>当前清算价</dt><dd>{Number(p.liquidation).toLocaleString('zh-CN', { maximumFractionDigits: 4 })} 美元</dd></div>}
      {e.liquidation_distance !== undefined && <div><dt>距清算价</dt><dd>{(Number(e.liquidation_distance) * 100).toFixed(2)}%</dd></div>}
    </dl>
    {e.sides && <div className="hlSides">{Object.entries(e.sides).map(([side, addresses]) => <section key={side}><h4>{side === 'long' ? '多方持仓' : '空方持仓'}</h4>{(addresses as string[]).map(a => <div key={a}><a href={`https://hyperbot.network/trader/${a}`} target="_blank" rel="noreferrer">{a}</a><strong>{money(e.positions?.[a]?.notional)}</strong></div>)}</section>)}</div>}
    <details><summary>完整证据与触发门槛</summary><pre>{JSON.stringify(e, null, 2)}</pre></details>
  </>;
}

const ruleNames: Record<string, string> = {
  large_position: '大仓金额', action: '重要操作金额', win_rate_action: '高胜率地址操作金额', market_cap_share: '相当于市值占比', oi_share: '未平仓量占比',
  major_pnl: 'BTC / ETH 盈亏', other_pnl: '其他资产盈亏', win_rate: '单资产胜率', min_rounds: '最低完整交易轮',
  asset_profit: 'BTC / ETH 单资产盈利', other_asset_profit: '其他资产单资产盈利', asset_profit_action: '单资产高盈利地址操作金额', account_profit: '账户盈利', historical_loss: '账户总亏损门槛',
  liquidation_distance: '距清算价', giveback_fraction: '浮盈回吐比例', escalation: '再触发档位倍数', new_address_window_ms: '新地址建仓窗口',
};
function ruleValue(key: string, value: unknown) {
  if (['oi_share', 'market_cap_share', 'win_rate', 'liquidation_distance', 'giveback_fraction'].includes(key)) return `${Number(value) * 100}%`;
  if (key === 'new_address_window_ms') return `${Number(value) / 60000} 分钟`;
  if (['min_rounds'].includes(key)) return `${value} 轮`;
  if (key === 'escalation') return `${value} 倍`;
  return money(value);
}
const shortAddress = (value: string) => `${value.slice(0, 8)}…${value.slice(-6)}`;
function eventStatus(event: HyperliquidNewsEvent) {
  if (event.evidence.test_replay) return '测试回放';
  if (event.news_eligibility?.eligible === false) return '监测基线';
  return statuses[event.pipeline.task_status || event.pipeline.dispatch_status || ''] || '待投递';
}
function eventChange(event: HyperliquidNewsEvent) {
  const e = event.evidence;
  if (event.news_eligibility?.eligible === false) return '监测快照 · 无变化证据';
  if (e.sides) return `多方 ${e.sides.long?.length || 0} 个地址 / 空方 ${e.sides.short?.length || 0} 个地址`;
  if (e.net_position_change) return `${e.net_position_change.direction} · ${money(e.net_position_change.estimated_value)}`;
  if (e.episode_peak_unrealized_pnl !== undefined) return `浮盈峰值 ${money(e.episode_peak_unrealized_pnl)} → 当前 ${money(e.position?.unrealized_pnl)}`;
  if (event.kinds.includes('large_reversal')) return `${Number(e.previous_position?.size) > 0 ? '多单' : '空单'} → ${Number(e.position?.size) > 0 ? '多单' : '空单'} · ${money(e.position?.notional)}`;
  if (e.actions) return `开仓 ${money(e.actions.open_notional)} / 平仓 ${money(e.actions.close_notional)}`;
  if (e.previous_position && e.position) return `浮盈亏 ${money(e.previous_position.unrealized_pnl)} → ${money(e.position.unrealized_pnl)}`;
  return '查看触发证据';
}

function EventDetail({ event, labels }: { event: HyperliquidNewsEvent; labels: Record<string, string> }) {
  const p = event.pipeline, preview = event.editorial_preview;
  const stages = event.evidence.test_replay ? [
    ['规则核验', '已取得测试证据'], ['编辑预览', preview ? '已生成' : '待生成'], ['正式投递', '测试不投递'],
  ] : [
    ['硬规则', '已命中'], ['行为防重', p.dispatch_status ? statuses[p.dispatch_status] || p.dispatch_status : '待处理'],
    ['搜索者查重', p.search_completed_at ? p.search_result?.is_duplicate ? '重复' : '已完成' : '待处理'],
    ['编写者1', p.write_completed_at ? '已完成' : '待处理'], ['编写者2', p.format_completed_at ? '已完成' : '待处理'],
    ['挂后台', p.publish_completed_at ? '已完成' : '待处理'],
  ];
  return <article className="hlDetail">
    <header className="hlDetailHeader"><div><div className="hlEyebrow">触发依据</div><h3>{event.coin} · {event.kinds.map(k => labels[k] || k).join(' / ')}</h3><p className="hlMuted">{time(event.observed_ms)}{p.task_id ? ` · 任务 #${p.task_id}` : ''}</p></div><span className="autoNewsflashStatus">{eventStatus(event)}</span></header>
    <div className={`hlReason ${event.news_eligibility?.eligible === false ? 'baseline' : ''}`}><Activity size={17} /><div><strong>{eventChange(event)}</strong><p>{event.news_eligibility?.eligible === false ? '这条记录只建立监测基线，不符合当前变化性准入。下方旧稿保留供对照。' : '以下展示事件对应的事实，可对照场景门槛理解为什么触发。'}</p></div></div>
    {event.evidence.highlights?.length > 0 && <div className="hlRuleChips">{event.evidence.highlights.map((key: string) => <span key={key}>{highlightNames[key] || key}</span>)}</div>}
    {event.evidence.test_replay && <p className="notice" role="note">上线前测试案例 · {time(event.evidence.test_replay.start_ms)} 至 {time(event.evidence.test_replay.end_ms)}。{event.evidence.test_replay.valuation_basis === 'quantity_times_window_last_execution_price' ? '仓位金额按该窗口末笔实际成交价折算；历史浮盈亏与清算价未补造。' : '重建口径及来源见完整证据。'} {event.evidence.test_replay.simulated_monitor_start && ' 本案例把历史首次空仓成交时点设为虚拟监测启动时点，用于测试快速建仓规则；不代表钱包刚创建或当时已被我们发现。'} 不作为当前动态投递。</p>}
    <Evidence event={event} />
    <section className="hlDetailSection"><h4><ShieldCheck size={16} />处理流程</h4><div className="hlStages">{stages.map(([label, result], index) => <div key={label}><small>{String(index + 1).padStart(2, '0')} · {label}</small><strong>{result}</strong></div>)}</div>
      <details><summary>搜索者跨来源查重记录</summary><p className="hlMuted">与已收集的 X 动态及已有快讯查重，包括 Lookonchain 等来源。</p>{p.search_result && Object.keys(p.search_result).length ? <><p>{p.search_result.is_duplicate ? '重复，停止后续写稿' : p.search_completed_at ? '查重通过' : '待完成'} · {time(p.search_completed_at)}</p><pre>{JSON.stringify(p.search_result, null, 2)}</pre></> : <p className="hlMuted">暂无查重记录</p>}</details>
      {(p.last_error || p.dispatch_error) && <p className="notice error" role="alert">{p.last_error || p.dispatch_error}</p>}
    </section>
<section className="hlDetailSection"><h4><FileText size={16} />快讯文本<span className="hlMuted">{event.evidence.test_replay ? '管理页预览 · 未投递' : '仅挂后台'}</span></h4>
      {preview && <p className="hlMuted">{event.evidence.test_replay ? '测试稿预览' : event.news_eligibility?.eligible === false ? '历史文本试稿' : '修订预览'} · 沿用原观察时点事实 · 未重新挂后台</p>}
      <div className="hlNewsCopy"><h3>{preview?.title || p.final_title || p.draft_title || '暂未生成快讯'}</h3><p className="hlCopy">{preview?.content || p.final_content || p.draft_content || '后续文本将在这里展示。'}</p></div>
      <p className="hlMuted">模型：{preview?.writer_model || p.writer_model || '—'} · Prompt：{preview?.prompt_source === 'repository_template' ? '仓库模板（未激活）' : preview?.prompt_version_number || p.prompt_version_number || '—'}</p>
      <a className="hlSourceLink" href={event.source_url} target="_blank" rel="noreferrer">查看 Hyperbot 地址 <ArrowRight size={14} /></a>
      <details><summary>原稿与时间记录</summary><h3>{p.final_title || p.draft_title || '暂未生成'}</h3><p className="hlCopy">{p.final_content || p.draft_content || '暂未生成'}</p><p className="hlMuted">编写：{time(p.write_completed_at)} · 定稿：{time(p.format_completed_at)} · 挂后台：{time(p.publish_completed_at)}</p>{preview && <p className="hlMuted">预览生成：{time(preview.generated_at)} · 原稿 Prompt 版本：{p.prompt_version_number || '—'}</p>}{p.push_result && <pre>{JSON.stringify(p.push_result, null, 2)}</pre>}</details>
    </section>
  </article>;
}

function AddressRow({ address: a, onChanged }: { address: Record<string, any>; onChanged: () => void }) {
  const [note, setNote] = useState(a.note || ''), [saved, setSaved] = useState(a.note || '');
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  async function block() {
    setBusy(true); setError('');
    try { await saveHyperliquidAddressBlock(a.address, !a.blocked); onChanged(); }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  async function save() {
    setBusy(true); setError('');
    try { const result = await saveHyperliquidAddressNote(a.address, note); setSaved(result.note); setNote(result.note); }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  return <article className="hlAddress">
    <div className="hlAddressTop"><a href={`https://hyperbot.network/trader/${a.address}`} target="_blank" rel="noreferrer">{a.address}</a><small>{a.blocked ? '已拉黑 · 停止监测与出稿' : `最近检查 ${time(a.last_polled_ms)}`}</small><button className="hlButton" disabled={busy} onClick={block}>{a.blocked ? '解除拉黑' : '拉黑'}</button></div>
    <div className="hlAddressNote"><input aria-label={`${a.address} 地址备注`} placeholder="地址备注，例如 Wintermute做市地址" maxLength={80} value={note} onChange={e => setNote(e.target.value)} /><button className="hlButton" disabled={busy || note === saved} onClick={save}>{busy ? '保存中' : '保存备注'}</button></div>
    {(error || a.error) && <p className="notice error">{error || a.error}</p>}
    <details><summary>发现来源与监测证据</summary><pre>{JSON.stringify({ sources: a.sources, positions: a.state?.positions, history: a.history }, null, 2)}</pre></details>
  </article>;
}

export function HyperliquidNewsPanel() {
  const [addressInput, setAddressInput] = useState(''), [addressQuery, setAddressQuery] = useState(''), [addressStatus, setAddressStatus] = useState('all');
  useEffect(() => { const timer = setTimeout(() => setAddressQuery(addressInput.trim()), 300); return () => clearTimeout(timer); }, [addressInput]);
  const [dataset, setDataset] = useState<'live' | 'test'>('live');
  const [tab, setTab] = useState<'scenarios' | 'addresses' | 'rules'>('scenarios'), [page, setPage] = useState(0);
  const [scenarioKey, setScenarioKey] = useState('account_profit'), [selected, setSelected] = useState<string | null>(null);
  const [data, setData] = useState<HyperliquidNewsDashboard | null>(null), [error, setError] = useState('');
  const [loading, setLoading] = useState(true), [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let live = true;
    setLoading(true); setError('');
    getHyperliquidNewsDashboard(page, scenarioKey, dataset, tab, addressQuery, addressStatus).then(d => {
      if (!live) return;
      setData(d); setSelected(current => d.events.some(e => e.key === current) ? current : (dataset === 'test' ? d.events.find(e => e.editorial_preview)?.key : null) || d.events.find(e => e.key === new URLSearchParams(window.location.search).get('event_key'))?.key || d.events[0]?.key || null);
    }).catch(e => { if (live) setError(String(e)); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [page, scenarioKey, refresh, dataset, tab, addressQuery, addressStatus]);
  const scenario = data?.scenarios.find(s => s.key === scenarioKey);
  useEffect(() => {
    if (data?.scenarios.length && !data.scenarios.some(s => s.key === scenarioKey)) {
      setScenarioKey(data.scenarios[0].key);
      setPage(0);
      setSelected(null);
    }
  }, [data?.scenarios, scenarioKey]);
  const current = data?.events.find(e => e.key === selected);
  const ready = data?.view === tab && data?.scenario === scenarioKey && data.page === page && data.dataset === dataset;
  return <section className="section hlMonitor">
    <div className="sectionHeader"><div><h2>Hyperliquid监测</h2><p className="hlMuted">按场景查看触发依据、关键数据与快讯处理进度</p></div><button className="hlButton" onClick={() => setRefresh(n => n + 1)} disabled={loading}><RefreshCw size={14} />{loading ? '读取中' : '刷新'}</button></div>
    {dataset === 'live' && data?.production?.enabled && <p className="notice" role="note">当前正式上线：{data.scenarios.filter(s => data.production?.enabled_scenarios.includes(s.key)).map(s => s.name).join('、')}。</p>}
    <div className="autoNewsflashSummary">
      {[[Layers, '固定监测场景', data?.scenarios.length ?? '—'], [Activity, dataset === 'test' ? '测试案例' : '监测记录', (dataset === 'test' ? data?.counts.test_cases : data?.counts.events) ?? '—'], [Database, '已发现地址', data?.counts.addresses ?? '—'], [FileText, dataset === 'test' ? '已有案例方向' : '已投递任务', dataset === 'test' ? data?.scenarios.filter(s => s.count > 0).length ?? '—' : data?.counts.submitted ?? '—']].map(([Icon, label, value]) => { const MetricIcon = Icon as typeof Layers; return <div className="autoNewsflashMetric" key={String(label)}><span><MetricIcon size={14} />{String(label)}</span><strong>{String(value)}</strong></div>; })}
    </div>
    <div className="hlToolbar"><div className="segmentedControl">{([['scenarios', '方向与场景'], ['addresses', '地址监测'], ['rules', '规则与运行']] as const).map(([key, label]) => <button key={key} className={tab === key ? 'active' : ''} aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</button>)}</div><span className="autoNewsflashStatus active">仅挂后台 · 不公开发布</span></div>
    {tab === 'scenarios' && <div className="hlToolbar"><div className="segmentedControl">{([['live', '正常监测'], ['test', '上线前测试案例']] as const).map(([key, label]) => <button key={key} aria-pressed={dataset === key} className={dataset === key ? 'active' : ''} onClick={() => { setDataset(key); setScenarioKey(key === 'live' ? 'large_pnl' : 'large_position'); setPage(0); setSelected(null); }}>{label}</button>)}</div><span className="hlMuted">{dataset === 'test' ? '真实历史证据回放 · 不进入正式投递 · 不改变监测基线' : '正常监测候选与流程记录'}</span></div>}
    {error && <p className="notice error" role="alert">{error}</p>}
    {loading && <p className="hlMuted" role="status">正在读取监测记录…</p>}
    {data && tab === 'scenarios' && <div className="hlWorkspace" aria-busy={loading}>
      <nav className="hlScenarioNav" aria-label="监测场景"><div className="hlNavHeading">监测方向<span>记录数</span></div>{data.scenarios.map((s, index) => <button key={s.key} className={`hlScenarioRow ${scenarioKey === s.key ? 'active' : ''}`} aria-pressed={scenarioKey === s.key} onClick={() => { setScenarioKey(s.key); setPage(0); setSelected(null); }}><span className="hlScenarioNumber">{String(index + 1).padStart(2, '0')}</span><strong>{s.name}</strong><span className={`hlCount ${s.count ? 'hasRecords' : ''}`}>{s.count}</span><ChevronRight size={14} /></button>)}<p className="hlNavNote">{dataset === 'live' ? '仅列出当前正式启用的七个方向。' : '仅保留尚未完成研究验收的方向和案例。'}</p></nav>
      <div className="hlScenarioContent">
        <header className="hlScenarioHeader"><div className="hlEyebrow">监测场景 · {String(data.scenarios.findIndex(s => s.key === scenarioKey) + 1).padStart(2, '0')}</div><h3>{scenario?.name}</h3><p>{scenario?.description}</p><div className="hlRuleChips">{scenario?.rule_keys.map(key => <span key={key}>{ruleNames[key] || key}<strong>{ruleValue(key, data.rules[key])}</strong></span>)}</div><p className="hlMuted">以上为当前工程默认门槛；实际触发值以记录保存的证据为准。</p></header>
        {ready && !loading && !error && <>
          {!data.events.length ? <div className="hlEmpty"><Layers size={28} /><h4>{data.initialized ? '该场景暂无监测记录' : '尚未开始采集'}</h4><p>场景已保留，符合条件的记录将在这里展示。</p></div> : <>
            <div className="hlRecordsHeader"><strong>场景记录</strong><span className="hlMuted">按观察时间倒序 · 先看变化，再看文本</span></div>
            <div className="hlRecordList">{data.events.map(event => <button key={event.key} className={`hlRecordRow ${selected === event.key ? 'active' : ''}`} aria-pressed={selected === event.key} onClick={() => setSelected(event.key)}><div><strong>{event.coin} <span>{event.addresses.length > 1 ? `${event.addresses.length} 个地址` : shortAddress(event.addresses[0])}</span></strong><p>{eventChange(event)}</p></div><div><span className="autoNewsflashStatus">{eventStatus(event)}</span><small>{time(event.observed_ms)}</small></div><ChevronRight size={15} /></button>)}</div>
            {current && <EventDetail key={current.key} event={current} labels={data.labels} />}
          </>}
          {(page > 0 || data.has_next) && <div className="hlPagination"><button className="hlButton" disabled={page === 0} onClick={() => setPage(n => n - 1)}>上一页</button><span>第 {page + 1} 页</span><button className="hlButton" disabled={!data.has_next} onClick={() => setPage(n => n + 1)}>下一页</button></div>}
        </>}
      </div>
    </div>}
    {data && tab === 'addresses' && <div className="hlSecondary"><div className="sectionHeader"><h3>地址监测</h3><span>展示最多 100 条匹配结果</span></div><div className="hlAddressNote"><input aria-label="搜索监测地址或备注" placeholder="搜索地址或备注" value={addressInput} onChange={e => setAddressInput(e.target.value)} /><select aria-label="地址状态筛选" value={addressStatus} onChange={e => setAddressStatus(e.target.value)}><option value="all">全部地址</option><option value="active">未拉黑</option><option value="blocked">已拉黑</option></select></div>{!data.addresses.length && <div className="hlEmpty"><Database size={28} /><h4>暂无匹配地址</h4><p>可调整搜索词或地址状态筛选。</p></div>}{data.addresses.map(a => <AddressRow key={a.address + ':' + (a.note || '')} address={a} onChanged={() => setRefresh(n => n + 1)} />)}</div>}
    {data && tab === 'rules' && <div className="hlSecondary"><h3>规则与运行</h3><p className="hlMuted">硬规则命中后进入查重与写作，不经过新闻价值筛选。</p><div className="hlFlow">{(data.flow || []).map((step, i) => <span key={step}>{i > 0 && <ChevronRight size={14} />}{step}</span>)}</div><dl className="hlFacts">{Object.entries(data.rules).map(([key, value]) => <div key={key}><dt>{ruleNames[key] || key}</dt><dd>{ruleValue(key, value)}</dd></div>)}</dl><details><summary>最近采集结果与覆盖</summary><pre>{JSON.stringify({ production: data.production, account_pool: data.account_pool, last_pass: data.last_pass }, null, 2)}</pre></details></div>}
  </section>;
}
