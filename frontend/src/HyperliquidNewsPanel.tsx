import { useEffect, useState } from 'react';
import { getHyperliquidNewsDashboard, type HyperliquidNewsDashboard, type HyperliquidNewsEvent } from './xCaptureStore';

function money(value: unknown): string {
  if (value === null || value === undefined) return '未取得';
  const number = Number(value);
  return Number.isFinite(number) ? `${(number / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 2 })} 万美元` : '未取得';
}
function time(value: unknown): string {
  if (!value) return '—';
  return new Date(typeof value === 'number' ? value : String(value)).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
}
const statuses: Record<string, string> = { deduped: '待撰写', writing: '编写者1处理中', written: '草稿完成',
  formatting: '编写者2处理中', publisher_pending: '待挂后台', publishing: '挂后台中', ready_review: '已挂后台',
  write_failed: '撰写失败', format_failed: '定稿失败', publisher_failed: '挂后台失败', duplicate: '重复行为',
  queued: '已进入写作队列', enqueue_failed: '入队失败', created: '等待入队' };

function Evidence({ event }: { event: HyperliquidNewsEvent }) {
  const e = event.evidence, p = e.position, history = e.history || {}, asset = history.assets?.[event.coin];
  const peak = e.episode_peak_unrealized_pnl;
  return <>
    <dl className="hlFacts">
      <div><dt>目标地址</dt><dd>{event.addresses.map(a => <a key={a} href={`https://hyperbot.network/trader/${a}`} target="_blank" rel="noreferrer">{a}<br /></a>)}</dd></div>
      <div><dt>合约</dt><dd>{event.coin}</dd></div>
      {p && <><div><dt>当前持仓</dt><dd>{Number(p.size) > 0 ? '多仓' : '空仓'} · {money(p.notional)}</dd></div>
        <div><dt>当前浮盈亏</dt><dd>{money(p.unrealized_pnl)}</dd></div></>}
      {e.market && <><div><dt>代币市值</dt><dd>{money(e.market.market_cap)} · {e.market.market_cap_source || '尚无来源'}</dd></div>
        <div><dt>持仓相当于市值</dt><dd>{e.ratios?.market_cap_share ? `${(Number(e.ratios.market_cap_share) * 100).toFixed(2)}%` : '未取得'}</dd></div>
        <div><dt>合约未平仓名义金额</dt><dd>{money(e.market.oi_notional)}</dd></div>
        <div><dt>占未平仓量</dt><dd>{e.ratios?.oi_share ? `${(Number(e.ratios.oi_share) * 100).toFixed(2)}%` : '未取得'}</dd></div></>}
      {peak !== undefined && <><div><dt>监测期间浮盈峰值</dt><dd>{money(peak)} · {time(e.episode_peak_ms)}</dd></div>
        <div><dt>回吐金额 / 比例</dt><dd>{money(Number(peak) - Number(p?.unrealized_pnl))} / {Number(peak) > 0 ? `${((Number(peak) - Number(p?.unrealized_pnl)) / Number(peak) * 100).toFixed(2)}%` : '—'}</dd></div></>}
      {asset && <><div><dt>单资产统计</dt><dd>{asset.rounds} 个完整交易轮 · 胜率 {asset.win_rate !== null ? `${(Number(asset.win_rate) * 100).toFixed(2)}%` : '未取得'}</dd></div>
        <div><dt>单资产已实现盈亏</dt><dd>{money(asset.closed_pnl_less_fees)}（不含资金费）</dd></div>
        <div><dt>实际成交样本范围</dt><dd>{time(history.first_observed_fill_ms)} 至 {time(history.last_observed_fill_ms)}</dd></div></>}
      {history.official_account_performance && Object.entries(history.official_account_performance.windows).map(([window, data]) => <div key={window}><dt>官方榜单 PnL · {window}</dt><dd>{money((data as any).pnl)} · 排名 {(data as any).rank}</dd></div>)}
      {e.actions && <><div><dt>本次取得开 / 平仓成交</dt><dd>{money(e.actions.open_notional)} / {money(e.actions.close_notional)}</dd></div><div><dt>本次成交已实现盈亏</dt><dd>{money(e.actions.closed_pnl)}</dd></div></>}
      {p?.liquidation && <div><dt>当前清算价</dt><dd>{Number(p.liquidation).toLocaleString('zh-CN', { maximumFractionDigits: 4 })} 美元</dd></div>}
      {e.liquidation_distance !== undefined && <div><dt>距清算价</dt><dd>{(Number(e.liquidation_distance) * 100).toFixed(2)}%（非清算预测）</dd></div>}
    </dl>
    {e.sides && <div className="hlFacts">{Object.entries(e.sides).map(([side, addresses]) => <p key={side}>{side === 'long' ? '多方' : '空方'}：{(addresses as string[]).map(a => `${a}（${money(e.positions?.[a]?.notional)}）`).join('；')}</p>)}</div>}
    <details><summary>完整证据与触发门槛</summary><pre>{JSON.stringify(e, null, 2)}</pre></details>
  </>;
}

export function HyperliquidNewsPanel() {
  const [tab, setTab] = useState('events'), [page, setPage] = useState(0);
  const [data, setData] = useState<HyperliquidNewsDashboard | null>(null), [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  async function load() { setLoading(true); try { setData(await getHyperliquidNewsDashboard(page)); setError(''); } catch(e) { setError(String(e)); } finally { setLoading(false); } }
  useEffect(() => { let live = true; setLoading(true); getHyperliquidNewsDashboard(page).then(d => { if (live) { setData(d); setError(''); } }).catch(e => { if(live) setError(String(e)); }).finally(() => { if(live) setLoading(false); }); return () => { live = false; }; }, [page]);
  return <section className="section hlMonitor">
    <div className="sectionHeader"><h2>Hyperliquid监测</h2><button type="button" onClick={() => void load()} disabled={loading}>刷新</button></div>
    <p>硬规则命中 → 行为重复控制 → 编写者1 → 编写者2 → 固定数据组装 → 挂后台</p>
    <div className="whaleTabs">{[['events','事件与快讯'],['addresses','地址监测'],['rules','规则与运行']].map(([key,label]) => <button type="button" key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}>{label}</button>)}</div>
    {error && <p role="alert">{error}</p>}
    {loading && <p>正在读取…</p>}
    {data && !data.initialized && <p>尚未运行采集测试；页面仅展示实际监测数据。</p>}
    {data && tab === 'events' && <>
      <p>{data.counts.events || 0} 条事件 · {data.counts.submitted || 0} 条已投递 · 强制挂后台</p>
      {data.events.map(event => <article className="hlEvent" key={event.key}>
        <header><strong>{event.editorial_preview?.title || event.pipeline.final_title || event.pipeline.draft_title || `${event.coin} · ${event.kinds.map(k => data.labels[k] || k).join(' / ')}`}</strong><span>{time(event.observed_ms)}</span></header>
        <p>{event.kinds.map(k => data.labels[k] || k).join(' · ')} · {statuses[event.pipeline.task_status || event.pipeline.dispatch_status || ''] || '硬规则已命中，待投递'}{event.pipeline.task_id ? ` · 任务 #${event.pipeline.task_id}` : ''}</p>
        {(event.pipeline.last_error || event.pipeline.dispatch_error) && <p role="alert">{event.pipeline.last_error || event.pipeline.dispatch_error}</p>}
        <details><summary>为什么触发：地址、持仓与变化</summary><Evidence event={event} /></details>
        {event.editorial_preview && <section>
          <p>修订预览 · 沿用 {time(event.observed_ms)} 的事实 · 未重新挂后台</p>
          <h3>{event.editorial_preview.title}</h3><p className="hlCopy">{event.editorial_preview.content}</p>
          <p>模型：{event.editorial_preview.writer_model} · Prompt版本：{event.editorial_preview.prompt_version_number} · 生成时间：{time(event.editorial_preview.generated_at)}</p>
        </section>}
        <details open={!event.editorial_preview && Boolean(event.pipeline.final_content)}><summary>{event.editorial_preview ? '原稿与流程记录' : '快讯与流程记录'}</summary>
          <h3>{event.pipeline.final_title || event.pipeline.draft_title || '等待编写'}</h3><p className="hlCopy">{event.pipeline.final_content || event.pipeline.draft_content || '暂未生成'}</p>
          <p>编写完成：{time(event.pipeline.write_completed_at)} · 定稿：{time(event.pipeline.format_completed_at)} · 挂后台：{time(event.pipeline.publish_completed_at)}</p>
          <p>模型：{event.pipeline.writer_model || '—'} · Prompt版本：{event.pipeline.prompt_version_number || '—'}</p>
          <a href={event.source_url} target="_blank" rel="noreferrer">原文链接（Hyperbot目标地址）</a>
          {event.pipeline.push_result && <pre>{JSON.stringify(event.pipeline.push_result, null, 2)}</pre>}
        </details>
      </article>)}
      <div className="sectionHeader"><button disabled={page === 0} onClick={() => setPage(page-1)}>上一页</button><span>第 {page+1} 页</span><button disabled={!data.has_next} onClick={() => setPage(page+1)}>下一页</button></div>
    </>}
    {data && tab === 'addresses' && <>{data.addresses.map(a => <article className="hlEvent" key={a.address}><a href={`https://hyperbot.network/trader/${a.address}`} target="_blank" rel="noreferrer">{a.address}</a><p>首次观察：{time(a.first_seen_ms)} · 最近轮询：{time(a.last_polled_ms)}</p><p>属性查询依据：{a.history?.enrichment_reasons?.join(' / ') || '未取得'}</p>{a.error && <p role="alert">{a.error}</p>}<details><summary>发现来源、仓位与单资产属性</summary><pre>{JSON.stringify({ sources: a.sources, positions: a.state?.positions, history: a.history }, null, 2)}</pre></details></article>)}</>}
    {data && tab === 'rules' && <><p>默认工程门槛；每条事件展开可查看当时实际门槛。当前没有新闻价值筛选。</p><div className="hlFacts">{Object.entries(data.rules).map(([key,value]) => <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>)}</div><h3>最近采集结果与覆盖</h3><pre>{JSON.stringify(data.last_pass, null, 2)}</pre><p>榜单和大额成交有界发现，非全网全历史；单资产胜率只统计完整交易轮。市值缺失不当成零。</p></>}
  </section>;
}
