import { useEffect, useState } from 'react';
import { Check, ChevronLeft, ChevronRight, Clock3, RefreshCcw, X } from 'lucide-react';
import {
  decideSystemReviewItem,
  getSystemReviewItem,
  listSystemReviewItems,
  type SystemReviewItem,
  type SystemReviewPage,
} from './xCaptureStore';
import './systemInformation.css';

const TASK_LABELS: Record<string, string> = {
  channel_outage: '渠道中断', missed_coverage: '漏闻复盘', output_audit: '产出与审核复盘',
  rule_conflict: '规则冲突与执行', failure_recovery: '持续故障',
};

const CATEGORY_LABELS: Record<string, string> = {
  persistent_internal_failure: '内部可修故障',
};

export default function SystemReviewPanel({ refreshToken = 0 }: { refreshToken?: number }) {
  const [page, setPage] = useState<SystemReviewPage | null>(null);
  const [selected, setSelected] = useState<SystemReviewItem | null>(null);
  const [status, setStatus] = useState('pending');
  const [taskKind, setTaskKind] = useState('all');
  const [offset, setOffset] = useState(0);
  const [note, setNote] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  async function load() {
    setLoading(true);
    setError('');
    try {
      const result = await listSystemReviewItems({ status, task_kind: taskKind, offset, limit: 30 });
      setPage(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, [refreshToken, status, taskKind, offset]);

  async function openItem(item: SystemReviewItem) {
    try {
      setSelected(await getSystemReviewItem(item.item_key));
      setNote(item.decision_note || '');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function decide(decision: 'accepted' | 'rejected' | 'deferred') {
    if (!selected) return;
    setSaving(true);
    setError('');
    try {
      setSelected(await decideSystemReviewItem(selected.item_key, decision, note));
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="systemInformationLayout">
      <div className="systemReviewToolbar">
        <label><span>问题状态</span><select value={status} onChange={(event) => { setStatus(event.target.value); setOffset(0); }}>
          <option value="pending">待审</option><option value="accepted">已认可</option><option value="rejected">已驳回</option><option value="deferred">已暂缓</option><option value="all">全部</option>
        </select></label>
        <label><span>巡检类别</span><select value={taskKind} onChange={(event) => { setTaskKind(event.target.value); setOffset(0); }}>
          <option value="all">全部巡检</option>{Object.entries(TASK_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
        <span>今日巡检用量：{page?.usage.calls || 0} / 100 次 · {page?.usage.input_tokens || 0} / 1,000,000 输入 tokens</span>
        {Boolean(page?.analyses_incomplete.length) && <span className="systemReviewPending">{page?.analyses_incomplete.length} 条分析等待、预算暂停或失败</span>}
        <button className="iconButton" type="button" title="刷新问题" onClick={() => void load()}><RefreshCcw size={16} /></button>
      </div>
      {error && <div className="notice error" role="alert">{error}</div>}
      <div className="systemInformationColumns">
        <div className="systemInformationList" aria-busy={loading}>
          {page?.items.map((item) => <button
            className={selected?.item_key === item.item_key ? 'systemInformationRow selected' : 'systemInformationRow'}
            type="button" key={item.item_key} onClick={() => void openItem(item)}>
            <span className="systemInformationRowTop"><strong>{item.title}</strong><span className={`systemInformationResult result-${item.severity}`}>{severityLabel(item.severity)}</span></span>
            <span className="systemInformationRowMeta"><span>{TASK_LABELS[item.task_kind] || item.task_kind}</span><span>{CATEGORY_LABELS[item.category] || item.category}</span><span>{item.recovered_at ? '运行已恢复' : item.task_kind === 'failure_recovery' ? '持续发生' : '仍在观察'}</span><time>{formatDate(item.last_seen_at)}</time></span>
            <span className="systemInformationSummary">{item.summary || analysisLabel(item.analysis_status)}</span>
          </button>)}
          {!loading && !page?.items.length && <div className="emptyState">当前筛选没有巡检问题。</div>}
          <div className="systemInformationPagination">
            <button className="secondaryButton" type="button" disabled={offset === 0 || loading} onClick={() => setOffset((value) => Math.max(0, value - 30))}><ChevronLeft size={16} /> 上一页</button>
            <span>{page?.total || 0} 条问题</span>
            <button className="secondaryButton" type="button" disabled={!page || offset + 30 >= page.total || loading} onClick={() => setOffset((value) => value + 30)}>下一页 <ChevronRight size={16} /></button>
          </div>
        </div>
        <aside className="systemInformationDetail">
          {selected ? <>
            <header className="systemInformationDetailHeader"><div><span>{TASK_LABELS[selected.task_kind] || selected.task_kind} · {selected.analysis_status === 'completed' ? 'AI已复核' : analysisLabel(selected.analysis_status)}</span><h2>{selected.title}</h2></div></header>
            <div className="systemInformationOutcome"><strong>{severityLabel(selected.severity)} · 置信度{certaintyLabel(selected.certainty)}</strong><p>{selected.summary || 'AI分析等待执行'}</p></div>
            <section className="systemReviewFacts"><h3>预期与实际</h3><p><strong>预期</strong>{selected.expected || '未记录'}</p><p><strong>实际</strong>{selected.actual || '未记录'}</p><p><strong>影响范围</strong>{JSON.stringify(selected.scope)} · {selected.affected_count} 项</p></section>
            <section><h3>建议与验证</h3><p>{selected.suggestion || '等待AI复核建议'}</p><p>{selected.validation || '等待可执行验证方法'}</p></section>
            <EvidenceBlock label="正向证据" value={selected.evidence} />
            <EvidenceBlock label="反向证据" value={selected.counter_evidence} />
            {selected.analysis_history?.length ? <details className="systemInformationTechnical">
              <summary>模型调用记录 · {selected.analysis_history.length} 次</summary>
              {selected.analysis_history.map((attempt) => <article className="systemInformationEvent" key={attempt.attempt_no}>
                <strong>第 {attempt.attempt_no} 次 · {attempt.status} · {attempt.input_tokens} 输入 tokens</strong>
                <time>{formatDate(attempt.started_at)}{attempt.finished_at ? ` 至 ${formatDate(attempt.finished_at)}` : ''}</time>
                {attempt.request_id && <span>请求 ID：{attempt.request_id}</span>}
                {attempt.error && <p>{attempt.error}</p>}
                {attempt.raw_response && <details className="systemInformationEvidence"><summary>原始模型返回</summary><pre>{attempt.raw_response}</pre></details>}
              </article>)}
            </details> : null}
            <div className="systemReviewTimes"><span><Clock3 size={14} /> 首次 {formatDate(selected.first_seen_at)}</span><span>最近 {formatDate(selected.last_seen_at)}</span><span>{selected.recovered_at ? `恢复 ${formatDate(selected.recovered_at)}` : '尚未确认恢复'}</span></div>
            {selected.analysis_error && <p className="systemInformationNotice">分析状态：{selected.analysis_status} · {selected.analysis_error}</p>}
            <label className="systemReviewNote"><span>人工决定理由</span><textarea value={note} onChange={(event) => setNote(event.target.value)} rows={3} maxLength={2000} /></label>
            <div className="systemReviewDecisions">
              <button className="primaryButton" type="button" disabled={saving} onClick={() => void decide('accepted')}><Check size={16} /> 认可</button>
              <button className="secondaryButton" type="button" disabled={saving} onClick={() => void decide('rejected')}><X size={16} /> 驳回</button>
              <button className="secondaryButton" type="button" disabled={saving} onClick={() => void decide('deferred')}><Clock3 size={16} /> 暂缓</button>
            </div>
            {selected.decisions?.length ? <section><h3>决定历史</h3>{selected.decisions.map((decision, index) => <article className="systemInformationEvent" key={`${decision.decided_at}-${index}`}><time>{formatDate(decision.decided_at)}</time><strong>{decision.actor} · {decision.decision}</strong><p>{decision.note || '未填写理由'}</p></article>)}</section> : null}
          </> : <div className="emptyState">选择一张问题卡查看证据并作出人工决定。</div>}
        </aside>
      </div>
    </section>
  );
}

function EvidenceBlock({ label, value }: { label: string; value: unknown }) {
  return <details className="systemInformationEvidence"><summary>{label}</summary><pre>{JSON.stringify(value || [], null, 2)}</pre></details>;
}

function severityLabel(value: string): string {
  return ({ critical: '严重', high: '高', medium: '中', low: '低', unknown: '待评估' } as Record<string, string>)[value] || value;
}

function certaintyLabel(value: string): string {
  return ({ high: '高', medium: '中', low: '低', unknown: '未知' } as Record<string, string>)[value] || value;
}

function analysisLabel(value: string): string {
  return ({ queued: '等待分析', failed: '分析失败', budget_paused: '预算暂停', evidence_insufficient: '证据不足' } as Record<string, string>)[value] || value;
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date);
}
