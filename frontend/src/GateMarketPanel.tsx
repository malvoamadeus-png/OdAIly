import type { GateMarketDashboard } from './xCaptureStore';

function timeLabel(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

function statusLabel(status: string | null): string {
  if (!status) return '未启动';
  if (status === 'open') return '交易中';
  if (status === 'closed') return '休市';
  return status;
}

export function GateMarketPanel({
  dashboard,
  loading,
}: {
  dashboard: GateMarketDashboard | null;
  loading: boolean;
}) {
  if (loading) {
    return <div className="emptyState">正在读取 Gate 行情播报状态…</div>;
  }
  if (!dashboard) {
    return <div className="emptyState">暂无 Gate 行情播报数据，请先在服务器初始化模块。</div>;
  }

  return (
    <div className="gateMarketLayout">
      <section className="statusGrid">
        <div>
          <span>运行模式</span>
          <strong>{dashboard.mode === 'live' ? '正式发布' : '后台生成'}</strong>
        </div>
        <div>
          <span>轮询周期</span>
          <strong>{dashboard.poll_interval_seconds} 秒</strong>
        </div>
        <div>
          <span>状态生成时间</span>
          <strong>{timeLabel(dashboard.generated_at)}</strong>
        </div>
      </section>

      <section className="section">
        <div className="sectionHeader">
          <h2>标的与阈值</h2>
          <span>只读 · 服务器 CLI 修改</span>
        </div>
        <div className="gateMarketSymbolGrid">
          {dashboard.symbols.map((item) => (
            <article className="gateMarketSymbolCard" key={item.symbol}>
              <header>
                <div>
                  <strong>{item.display_name}</strong>
                  <span>{item.symbol}</span>
                </div>
                <span className={item.market_status === 'open' ? 'statusPill' : 'statusPill warn'}>
                  {statusLabel(item.market_status)}
                </span>
              </header>
              <dl>
                <div>
                  <dt>播报步长</dt>
                  <dd>{item.threshold}{item.unit}</dd>
                </div>
                <div>
                  <dt>当前价格</dt>
                  <dd>{item.last_price ? `${item.last_price}${item.unit}` : '—'}</dd>
                </div>
                <div>
                  <dt>状态</dt>
                  <dd>{item.initialized ? '已初始化' : '等待首次报价'}</dd>
                </div>
                <div>
                  <dt>最后报价</dt>
                  <dd>{timeLabel(item.last_quote_at)}</dd>
                </div>
              </dl>
              {item.last_error && <p className="gateMarketError">{item.last_error}</p>}
            </article>
          ))}
        </div>
      </section>

      <section className="section">
        <div className="sectionHeader">
          <h2>固定文本模板</h2>
          <span>{dashboard.templates.length} 类</span>
        </div>
        <div className="gateMarketTemplateList">
          {dashboard.templates.map((template) => (
            <article key={template.template_key}>
              <strong>{template.label}</strong>
              <code>{template.title_template}</code>
              <p>{template.body_template}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="section">
        <div className="sectionHeader">
          <h2>最近触发结果</h2>
          <span>最多保留 100 条</span>
        </div>
        {dashboard.recent_events.length === 0 ? (
          <div className="emptyState compact">尚无触发记录。</div>
        ) : (
          <div className="gateMarketEventList">
            {dashboard.recent_events.slice(0, 20).map((event) => (
              <article key={event.id}>
                <div>
                  <strong>{event.symbol} · {event.trigger_level}</strong>
                  <span>{event.status} · {timeLabel(event.observed_at)}</span>
                </div>
                <p>{event.title || event.error || '该次触发未生成文本'}</p>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
