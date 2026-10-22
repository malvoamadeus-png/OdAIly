import { useMemo, useState } from 'react';
import { Search } from 'lucide-react';
import type { StockMarketDashboard } from './xCaptureStore';

const MARKET_LABELS: Record<string, string> = {
  us: '美股',
  cn: 'A股',
  hk: '港股',
  kr: '韩股',
  jp: '日股',
};

function timeLabel(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false });
}

function percentLabel(value: string | null): string {
  if (value === null) return '—';
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`;
}

function changeClass(value: string | null): string {
  const number = Number(value);
  return number > 0 ? 'stockMarketUp' : number < 0 ? 'stockMarketDown' : '';
}

function cycleStatusLabel(value: string | undefined): string {
  if (value === 'ok') return '运行正常';
  if (value === 'partial_error') return '部分异常';
  if (value === 'baseline') return '已建立基线';
  if (value === 'error') return '运行异常';
  return '等待数据';
}

function priceLockLabel(contract: StockMarketDashboard['contracts'][number], direction: 'up' | 'down') {
  const lock = contract.price_locks?.[direction];
  if (!lock) return '等待基线';
  if (!lock.tier) return `基线 ${contract.resume_price ?? lock.anchor_price} · 等待达标`;
  const target = Number(lock.anchor_price) * (direction === 'up' ? 1.05 : 0.95);
  return `已记录 ${lock.anchor_price} · 下一价${direction === 'up' ? '≥' : '≤'}${target.toFixed(contract.price_precision)}`;
}

function eventStatusLabel(status: string): string {
  if (status === 'published') return '已直发';
  if (status === 'backend_created') return '已挂后台';
  if (status === 'push_failed') return '推送失败';
  return '已消耗，结果待确认';
}

export function StockMarketPanel({
  dashboard,
  loading,
}: {
  dashboard: StockMarketDashboard | null;
  loading: boolean;
}) {
  const [tab, setTab] = useState<'overview' | 'contracts'>('overview');
  const [query, setQuery] = useState('');
  const [market, setMarket] = useState('all');
  const heartbeatFresh = Boolean(dashboard?.last_cycle_at && Date.now() - new Date(dashboard.last_cycle_at).getTime() <= 90000);

  const contracts = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return (dashboard?.contracts ?? []).filter((contract) => {
      const matchesQuery = !normalized || contract.symbol.toLowerCase().includes(normalized);
      return matchesQuery && (market === 'all' || contract.market === market);
    });
  }, [dashboard?.contracts, market, query]);

  return (
    <div className="stockMarketLayout">
      <section className="statusGrid stockMarketStatus">
        <div><span>合约数</span><strong>{dashboard?.contract_count ?? '—'}</strong></div>
        <div><span>监测周期</span><strong>{dashboard?.poll_interval_seconds ?? 30} 秒</strong></div>
        <div><span>最近轮询</span><strong>{timeLabel(dashboard?.last_cycle_at)}</strong></div>
        <div><span>运行状态</span><strong>{heartbeatFresh ? cycleStatusLabel(dashboard?.last_cycle_status) : '未检测到近期轮询'}</strong></div>
      </section>

      <div className="newsflashTabs" role="tablist" aria-label="股票行情播报子页">
        <button type="button" role="tab" aria-selected={tab === 'overview'} className={tab === 'overview' ? 'active' : ''} onClick={() => setTab('overview')}>播报概览</button>
        <button type="button" role="tab" aria-selected={tab === 'contracts'} className={tab === 'contracts' ? 'active' : ''} onClick={() => setTab('contracts')}>合约行情</button>
      </div>

      {tab === 'overview' && <>

      <div className="stockMarketNotice">
        首批候选等待3分钟，按市场和涨跌方向合并；后续每30分钟汇总。
        5%挂后台、10%直发，同一资产合并1H/24H信号。
        恢复后还需相对恢复基准价同向变化5%才能挂后台、10%才能直发；恢复前的涨跌不计入这道门槛。
        价格记录保留6小时，实际价格再走出5%才再次播报；后台升直发须创出本轮新高或新低。
      </div>

      {!!dashboard?.pending_groups?.length && (
        <section className="section">
          <div className="sectionHeader"><h2>待合并候选</h2></div>
          <div className="stockMarketEvents">
            {dashboard.pending_groups.map((group) => (
              <article key={`${group.market}:${group.direction}`}>
                <strong>{MARKET_LABELS[group.market]} · {group.direction === 'up' ? '上涨' : '下跌'} · {group.assets.length}只</strong>
                <p>{group.assets.join('、')}</p>
                <small>计划汇总：{timeLabel(group.due_at)}{!heartbeatFresh ? '（等待 worker 恢复，恢复时将静默清理候选）' : ''}</small>
              </article>
            ))}
          </div>
        </section>
      )}

      {(dashboard?.last_cycle_error || dashboard?.last_discovery_error) && (
        <div className="stockMarketNotice">
          {dashboard.last_cycle_error || dashboard.last_discovery_error}
        </div>
      )}

      <section className="section">
        <div className="sectionHeader"><h2>最近事件</h2><span>最多展示100条</span></div>
        {!dashboard?.recent_events.length ? (
          <div className="emptyState compact">尚无触发记录。</div>
        ) : (
          <div className="stockMarketEvents">
            {dashboard.recent_events.map((event) => (
              <article key={event.id}>
                <div className="stockMarketEventMeta">
                  <strong>{event.title}</strong>
                  <span>{MARKET_LABELS[event.market] ?? event.market} · {event.members?.length ? `${event.members.length}只资产` : event.window === '1h' ? '1小时' : '24小时'} · {event.direction === 'up' ? '上涨' : '下跌'} · {event.tier === 'live' ? '直发' : '挂后台'} · {eventStatusLabel(event.status)}</span>
                  <span>{timeLabel(event.created_at)}</span>
                  <b className={changeClass(event.change_percent)}>{percentLabel(event.change_percent)}</b>
                </div>
                <p>{event.content}</p>
                {event.error && <small className="stockMarketRowError">{event.error}</small>}
              </article>
            ))}
          </div>
        )}
      </section>
      </>}

      {tab === 'contracts' && <>
      <section className="section">
        <div className="sectionHeader stockMarketToolbar">
          <h2>合约行情</h2>
          <span>{contracts.length} / {dashboard?.contract_count ?? 0}</span>
          <label className="stockMarketSearch">
            <Search size={16} />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索资产名或合约代码" />
          </label>
          <select aria-label="按市场筛选" value={market} onChange={(event) => setMarket(event.target.value)}>
            <option value="all">全部市场</option>
            <option value="us">美股</option>
            <option value="cn">A股</option>
            <option value="hk">港股</option>
            <option value="kr">韩股</option>
            <option value="jp">日股</option>
          </select>
        </div>
        {loading && !dashboard ? (
          <div className="emptyState">正在读取股票行情…</div>
        ) : contracts.length === 0 ? (
          <div className="emptyState">{dashboard ? '没有匹配的合约。' : '暂无股票行情数据，请先启动股票行情播报 worker。'}</div>
        ) : (
          <div className="stockMarketTableWrap">
            <table className="stockMarketTable">
              <thead>
                <tr><th>资产</th><th>市场</th><th>现价 (USDT)</th><th>1H</th><th>24H</th><th>上涨价格记录</th><th>下跌价格记录</th><th>报价时间</th></tr>
              </thead>
              <tbody>
                {contracts.map((contract) => (
                  <tr key={contract.symbol}>
                    <td><strong title={contract.symbol}>{contract.asset_name ?? contract.symbol.replace(/USDT$/, '')}</strong>{contract.last_error && <small className="stockMarketRowError">{contract.last_error}</small>}</td>
                    <td>{MARKET_LABELS[contract.market] ?? contract.market}</td>
                    <td>{contract.last_price ?? '—'}</td>
                    <td className={changeClass(contract.change_1h)}>{percentLabel(contract.change_1h)}</td>
                    <td className={changeClass(contract.change_24h)}>{percentLabel(contract.change_24h)}</td>
                    <td>{priceLockLabel(contract, 'up')}</td>
                    <td>{priceLockLabel(contract, 'down')}</td>
                    <td>{timeLabel(contract.observed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      </>}
    </div>
  );
}
