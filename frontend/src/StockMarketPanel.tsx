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

function triggerLabel(contract: StockMarketDashboard['contracts'][number], window: '1h' | '24h') {
  const state = contract.trigger_state[window];
  if (!state?.initialized) return '等待基线';
  const pending: string[] = [];
  if (!state.armed_5_up) pending.push('涨5%');
  if (!state.armed_10_up) pending.push('涨10%');
  if (!state.armed_5_down) pending.push('跌5%');
  if (!state.armed_10_down) pending.push('跌10%');
  return pending.length ? `${pending.join('/')}未武装` : '全部已武装';
}

export function StockMarketPanel({
  dashboard,
  loading,
}: {
  dashboard: StockMarketDashboard | null;
  loading: boolean;
}) {
  const [query, setQuery] = useState('');
  const [market, setMarket] = useState('all');

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
        <div><span>运行状态</span><strong>{cycleStatusLabel(dashboard?.last_cycle_status)}</strong></div>
      </section>

      {(dashboard?.last_cycle_error || dashboard?.last_discovery_error) && (
        <div className="stockMarketNotice">
          {dashboard.last_cycle_error || dashboard.last_discovery_error}
        </div>
      )}

      <section className="section">
        <div className="sectionHeader stockMarketToolbar">
          <h2>合约行情</h2>
          <span>{contracts.length} / {dashboard?.contract_count ?? 0}</span>
          <label className="stockMarketSearch">
            <Search size={16} />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索合约代码" />
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
                <tr><th>合约</th><th>市场</th><th>现价 (USDT)</th><th>1H</th><th>24H</th><th>1H触发</th><th>24H触发</th><th>报价时间</th></tr>
              </thead>
              <tbody>
                {contracts.map((contract) => (
                  <tr key={contract.symbol}>
                    <td><strong>{contract.symbol}</strong>{contract.last_error && <small className="stockMarketRowError">{contract.last_error}</small>}</td>
                    <td>{MARKET_LABELS[contract.market] ?? contract.market}</td>
                    <td>{contract.last_price ?? '—'}</td>
                    <td className={changeClass(contract.change_1h)}>{percentLabel(contract.change_1h)}</td>
                    <td className={changeClass(contract.change_24h)}>{percentLabel(contract.change_24h)}</td>
                    <td>{triggerLabel(contract, '1h')}</td>
                    <td>{triggerLabel(contract, '24h')}</td>
                    <td>{timeLabel(contract.observed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="section">
        <div className="sectionHeader"><h2>最近事件</h2><span>最多展示100条</span></div>
        {!dashboard?.recent_events.length ? (
          <div className="emptyState compact">尚无触发记录。</div>
        ) : (
          <div className="stockMarketEvents">
            {dashboard.recent_events.map((event) => (
              <article key={event.id}>
                <div className="stockMarketEventMeta">
                  <strong>{event.symbol}</strong>
                  <span>{MARKET_LABELS[event.market] ?? event.market} · {event.window === '1h' ? '1小时' : '24小时'} · {event.direction === 'up' ? '上涨' : '下跌'} · {event.tier === 'live' ? '直发' : '挂后台'}</span>
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
    </div>
  );
}
