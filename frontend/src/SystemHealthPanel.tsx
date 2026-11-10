import { useEffect, useState } from 'react';
import { ExternalLink, RefreshCcw } from 'lucide-react';
import { getSystemHealth, type SystemHealthSnapshot } from './xCaptureStore';
import './systemInformation.css';

export default function SystemHealthPanel({ mode = 'all', refreshToken = 0, onOpenInformation }: { mode?: 'all' | 'briefs'; refreshToken?: number; onOpenInformation?: () => void }) {
  const [snapshot, setSnapshot] = useState<SystemHealthSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  async function load() {
    setLoading(true);
    setError('');
    try {
      setSnapshot(await getSystemHealth());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, [refreshToken]);

  const projection = snapshot?.coverage.projection;
  const storage = snapshot?.storage;
  const estimatedGrowth = storage?.estimated_growth_next_30d_bytes;
  return (
    <section className="systemInformationLayout systemHealthLayout">
      {error && <div className="notice error" role="alert">{error}</div>}
      <div className="systemInformationMeta">
        <span>{loading ? '正在读取' : snapshot ? `快照 ${formatTime(snapshot.generated_at_ms)}` : '读取失败'}</span>
        <span>{loading ? '索引状态读取中' : projection?.last_success_ms ? `索引更新 ${formatTime(projection.last_success_ms)}` : projection ? '尚无成功索引记录' : '索引状态未知'}</span>
        <span>{loading ? '积压状态读取中' : projection ? projection.lag ? `待索引 ${projection.lag} 条` : '主流程索引已追平' : '积压状态未知'}</span>
        <button className="iconButton" type="button" title="刷新运行状态" onClick={() => void load()}><RefreshCcw size={16} /></button>
      </div>

      {projection?.last_error && <div className="notice error" role="status">信息流投影失败：{projection.last_error}</div>}
      {snapshot?.coverage.worker_read_error && <div className="notice error" role="status">读取 worker 心跳失败：{snapshot.coverage.worker_read_error}</div>}

      <section className="systemHealthSection">
        <header><h2>磁盘与证据留存</h2><span>扩容门槛：至少预留 10 GiB 或文件系统容量的 15%</span></header>
        <div className="systemStorageSummary">
          <div><span>可用空间</span><strong className={storage ? storage.space_ok ? 'healthState good' : 'healthState bad' : ''}>{storage ? `${formatBytes(storage.filesystem_free_bytes)} / ${formatBytes(storage.filesystem_total_bytes)}` : loading ? '读取中' : '读取失败'}</strong></div>
          <div><span>最低余量</span><strong>{storage ? formatBytes(storage.required_free_bytes) : loading ? '读取中' : '未知'}</strong></div>
          <div><span>信息流数据库</span><strong>{storage ? formatBytes(storage.database_bytes) : loading ? '读取中' : '未知'}</strong></div>
          <div><span>压缩证据</span><strong>{storage ? formatBytes(storage.evidence_bytes) : loading ? '读取中' : '未知'}</strong></div>
          <div><span>预计未来 30 天增长</span><strong>{!storage ? loading ? '读取中' : '未知' : estimatedGrowth == null ? '样本不足' : formatBytes(estimatedGrowth)}</strong></div>
          <div><span>增长样本</span><strong>{storage ? `${storage.samples} 天` : loading ? '读取中' : '未知'}</strong></div>
        </div>
        {storage && !storage.space_ok && <div className="notice error" role="status">可用空间低于当前安全余量门槛；请扩容后再执行大型回填或索引维护。</div>}
      </section>

      {mode === 'all' && <section className="systemHealthSection">
        <header><h2>外部组件历史错误统计</h2><span>北京时间跨日后只保留分类次数</span></header>
        <div className="systemHealthRows">
          {snapshot?.external_error_daily?.map((row) => <article className="systemHealthAdapter" key={`${row.error_date}:${row.source}:${row.stage}:${row.category}`}>
            <strong>{row.error_date} · {row.source} · {row.stage}</strong>
            <span>{({ upstream_timeout: '上游超时', upstream_http: '上游接口错误', upstream_auth: '权限或额度', upstream_network: '上游网络错误' } as Record<string, string>)[row.category] || row.category}</span>
            <span>{row.occurrences} 次</span>
          </article>)}
          {!loading && !snapshot?.external_error_daily?.length && <div className="emptyState">尚无已汇总的历史外部错误。</div>}
        </div>
      </section>}

      {mode === 'all' && <section className="systemHealthSection">
        <header><h2>Worker 心跳</h2><span>{snapshot?.workers.length || 0} 个已登记运行实例</span></header>
        {snapshot?.review_worker && <article className="systemHealthAdapter">
          <strong>系统巡检 worker</strong>
          <span>{!snapshot.review_worker.enabled ? '已停用' : snapshot.review_worker.last_seen_at ? `最近活动 ${formatTime(Date.parse(snapshot.review_worker.last_seen_at))}` : '等待首次运行'}</span>
          {snapshot.review_worker.enabled && snapshot.review_worker.last_error && <span className="healthState bad">{snapshot.review_worker.last_error}</span>}
        </article>}
        <div className="systemHealthRows">
          {snapshot?.workers.map((worker) => <article className="systemHealthRow" key={`${worker.component}:${worker.worker_id}`}>
            <strong>{worker.component}</strong><span>{worker.worker_id}</span><span className={worker.status === 'ok' ? 'healthState good' : 'healthState bad'}>{worker.status}</span>
            <time>最近活动 {worker.last_seen_at ? formatTime(Date.parse(worker.last_seen_at)) : '未知'}</time>
            <time>最近成功 {worker.last_success_at ? formatTime(Date.parse(worker.last_success_at)) : '未知'}</time>
            {worker.last_error && <p>{worker.last_error}</p>}
          </article>)}
          {!loading && !snapshot?.workers.length && <div className="emptyState">当前没有已登记的 worker 心跳。</div>}
        </div>
      </section>}

      {mode === 'all' && Boolean(snapshot?.inspection_runs.length) && <section className="systemHealthSection">
        <header><h2>巡检任务</h2><span>每日任务与渠道检查</span></header>
        <div className="systemHealthRows">
          {snapshot?.inspection_runs.slice(0, 10).map((run) => <article className="systemHealthAdapter" key={run.run_key}>
            <strong>{run.task_kind === 'daily_quality' ? '每日质量检查' : run.task_kind === 'storage_retention' ? '证据留存清理' : '渠道检查'}</strong>
            <span className={run.status === 'completed' ? 'healthState good' : 'healthState bad'}>{run.status}</span>
            <time>{formatTime(Date.parse(run.started_at))}</time>
            <span>{run.items_created} 项</span>
            {run.error && <span className="healthState bad">{run.error}</span>}
          </article>)}
        </div>
      </section>}

      {mode === 'all' && <section className="systemHealthSection">
        <header><h2>业务数据接入</h2><span>未配置和停用来源不会标为采集故障</span></header>
        <div className="systemHealthRows">
          {snapshot?.coverage.adapters.map((adapter) => <article className="systemHealthAdapter" key={adapter.name}>
            <strong>{adapter.name.replaceAll('_', ' ')}</strong>
            <span className={adapter.status === 'ready' ? 'healthState good' : adapter.status === 'error' ? 'healthState bad' : 'healthState'}>
              {adapter.status}{adapter.audit_tables != null ? ` · 事务记录表 ${adapter.audit_tables} 张` : ''}{adapter.audit_skipped ? ` · 未覆盖 ${adapter.audit_skipped} 张` : ''}
            </span>
          </article>)}
          {!loading && !snapshot?.coverage.adapters.length && <div className="emptyState">尚无适配器同步记录。</div>}
        </div>
        {snapshot?.coverage.unmapped_sources?.length ? <p className="systemInformationNotice">未登记业务来源：{snapshot.coverage.unmapped_sources.join('、')}</p> : null}
      </section>}

      <section className="systemHealthSection">
        <header><h2>定时行情简报</h2><span>使用现有调度配置</span></header>
        <div className="systemScheduleRows">
          {snapshot?.scheduled_tasks.map((task) => <article className="systemScheduleRow" key={task.task_id}>
            <div className="systemScheduleTitle"><strong>{briefName(task.task_id)}</strong><span>{task.config || '环境配置'}</span></div>
            <div className="systemScheduleTimes">{task.schedules.map((schedule) => <span key={schedule.job_id}><b>{schedule.kind}</b>{schedule.description}</span>)}</div>
          </article>)}
          {!loading && !snapshot?.scheduled_tasks.length && <div className="emptyState">暂时无法读取既有简报调度。</div>}
        </div>
        <div className="systemHealthBriefs">
          <strong>最近执行结果与稿件</strong>
          {snapshot?.recent_briefs.map((brief) => <article className="systemHealthBrief" key={brief.item_id}>
            <span>{brief.title || brief.source_id || brief.item_id}</span><span>{brief.result}</span><time>{formatTime(brief.updated_at_ms || brief.created_at_ms || undefined)}</time>
          </article>)}
          {!loading && !snapshot?.recent_briefs.length && <div className="emptyState">信息流中还没有已登记的简报记录。</div>}
        </div>
        {onOpenInformation && <button className="secondaryButton" type="button" onClick={onOpenInformation}>打开全量信息流 <ExternalLink size={15} /></button>}
      </section>
    </section>
  );
}

function briefName(taskId: string): string {
  return ({ 'gate-tradfi': 'Gate 传统市场', 'us-market': '美国市场', 'okx-stock-brief': 'OKX 美股周报' } as Record<string, string>)[taskId] || taskId;
}

function formatTime(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return '未知';
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(value);
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return '未知';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}
