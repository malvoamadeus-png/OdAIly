import { useEffect, useState } from 'react';
import { RefreshCcw } from 'lucide-react';
import {
  getMacroNewsflashDashboard,
  type MacroNewsflashDashboard,
  type MacroNewsflashEvent,
} from './xCaptureStore';

const METRIC_LABELS: Record<string, string> = {
  retail_sales_mom: '零售销售月率',
  initial_claims: '当周初请失业金人数',
  fomc_rate: '利率决议',
  pce_yoy: 'PCE物价指数年率',
  nonfarm_payrolls: '季调后非农就业人口',
  unemployment_rate: '月失业率',
  cpi_yoy_nsa: '未季调CPI年率',
  cpi_mom_nsa: '未季调CPI月率',
  cpi_mom_sa: '季调后CPI月率',
  core_cpi_mom_sa: '季调后核心CPI月率',
};

const DELIVERY_LABELS: Record<string, string> = {
  pending: '待挂后台',
  publishing: '挂后台中',
  published: '已挂后台',
  uncertain: '结果待确认',
};

function timeLabel(value: string | null | undefined, timeZone = 'Asia/Shanghai'): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

function eventStatus(event: MacroNewsflashEvent): string {
  return DELIVERY_LABELS[event.status] ?? event.status;
}

export function MacroNewsflashPanel({ refreshToken = 0 }: { refreshToken?: number }) {
  const [dashboard, setDashboard] = useState<MacroNewsflashDashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  async function load() {
    setLoading(true);
    setError('');
    try {
      setDashboard(await getMacroNewsflashDashboard());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, [refreshToken]);

  const latestEvents = new Map<string, MacroNewsflashEvent>();
  for (const event of dashboard?.events ?? []) {
    if (!latestEvents.has(event.metric_id)) latestEvents.set(event.metric_id, event);
  }

  return (
    <div className="macroNewsflashLayout">
      <div className="macroNewsflashToolbar">
        <span>数据检查 {timeLabel(dashboard?.source_checked_at)} · 日历刷新 {timeLabel(dashboard?.schedule_refreshed_at)}</span>
        <button className="iconButton" type="button" title="刷新美国数据状态" aria-label="刷新美国数据状态" onClick={() => void load()} disabled={loading}>
          <RefreshCcw size={17} />
        </button>
      </div>

      {error && <div className="notice error">{error}</div>}
      {!loading && dashboard && !dashboard.available && (
        <div className="macroNewsflashNotice warning">宏观数据 worker 尚未初始化，暂时没有线上排程或采集状态。</div>
      )}

      <section className="statusGrid macroNewsflashStatus">
        <div><span>未来排程</span><strong>{dashboard?.scheduled_releases.length ?? '—'}</strong></div>
        <div><span>已观测指标</span><strong>{dashboard?.metrics.length ?? '—'}</strong></div>
        <div><span>数据源异常</span><strong>{Object.keys(dashboard?.source_errors ?? {}).length}</strong></div>
        <div><span>日历异常</span><strong>{Object.keys(dashboard?.schedule_errors ?? {}).length}</strong></div>
      </section>

      {!!Object.keys(dashboard?.source_errors ?? {}).length && (
        <section className="macroNewsflashNotice warning">
          <strong>指标数据源异常</strong>
          <ul>
            {Object.entries(dashboard?.source_errors ?? {}).map(([source, message]) => (
              <li key={source}><span>{source}</span>：{message}</li>
            ))}
          </ul>
        </section>
      )}

      {!!Object.keys(dashboard?.schedule_errors ?? {}).length && (
        <section className="macroNewsflashNotice warning">
          <strong>日历来源异常</strong>
          <ul>
            {Object.entries(dashboard?.schedule_errors ?? {}).map(([source, message]) => (
              <li key={source}><span>{source}</span>：{message}</li>
            ))}
          </ul>
        </section>
      )}

      <section className="section">
        <div className="sectionHeader"><h2>未来发布排程</h2><span>北京时间 · 美东时间</span></div>
        {loading ? <div className="emptyState">正在读取美国数据排程…</div> : !dashboard?.scheduled_releases.length ? (
          <div className="emptyState">暂无已解析的未来排程；请查看上方日历来源状态。</div>
        ) : (
          <div className="macroNewsflashReleaseList">
            {dashboard.scheduled_releases.map((release) => (
              <article key={release.release_id}>
                <div className="macroNewsflashReleaseTime">
                  <strong>{timeLabel(release.scheduled_at_beijing)}</strong>
                  <span>{timeLabel(release.scheduled_at_eastern, 'America/New_York')} 美东</span>
                </div>
                <div className="macroNewsflashReleaseInfo">
                  <strong>{release.metric_ids.map((id) => METRIC_LABELS[id] ?? id).join('、')}</strong>
                  <span>{release.source_name} · {release.is_estimate ? '规律估算' : '已公布排程'}</span>
                  <small>{release.title}</small>
                </div>
                <a href={release.source_url} target="_blank" rel="noreferrer">来源</a>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="section">
        <div className="sectionHeader"><h2>最近数据观测</h2><span>统计期、来源与最近快讯</span></div>
        {!dashboard?.metrics.length ? (
          <div className="emptyState">暂无已采集的指标统计期。</div>
        ) : (
          <div className="macroNewsflashMetricList">
            {dashboard.metrics.map((metric) => {
              const event = latestEvents.get(metric.metric_id);
              return (
                <article key={metric.metric_id}>
                  <div>
                    <strong>{METRIC_LABELS[metric.metric_id] ?? metric.metric_id}</strong>
                    <span>{metric.last_reference_period} · {metric.source_name}</span>
                  </div>
                  <div className="macroNewsflashMetricValue">
                    {metric.actual || event ? <><strong>{metric.actual ?? event?.actual}</strong><span>前值 {metric.previous ?? event?.previous}</span></> : <span>已采集 · 尚无快讯记录</span>}
                  </div>
                  <div className="macroNewsflashMetricStatus">
                    <span>{event ? eventStatus(event) : '基线已建立'}</span>
                    <small>{timeLabel(metric.last_observed_at)}</small>
                  </div>
                  <a href={metric.source_url} target="_blank" rel="noreferrer">数据源</a>
                </article>
              );
            })}
          </div>
        )}
      </section>

      <section className="section">
        <div className="sectionHeader"><h2>最近快讯</h2><span>最多50条</span></div>
        {!dashboard?.events.length ? (
          <div className="emptyState">暂无宏观数据快讯。</div>
        ) : (
          <div className="macroNewsflashEventList">
            {dashboard.events.slice(0, 20).map((event) => (
              <article key={event.event_key}>
                <div><strong>{event.title}</strong><span>{METRIC_LABELS[event.metric_id] ?? event.metric_id} · {event.reference_period} · {eventStatus(event)}</span></div>
                <time>{timeLabel(event.published_at ?? event.created_at)}</time>
                {event.error && <small>{event.error}</small>}
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
