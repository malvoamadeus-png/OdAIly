import { useEffect, useState } from 'react';
import { Check, ChevronLeft, ChevronRight, ExternalLink, RefreshCcw, X, EyeOff } from 'lucide-react';
import {
  decideAITextSelfUpgradeItem,
  getAITextSelfUpgradeItem,
  listAITextSelfUpgradeItems,
  type AITextSelfUpgradeItem,
  type AITextSelfUpgradePage,
} from './xCaptureStore';
import './aiTextSelfUpgrade.css';

const PAGE_SIZE = 30;
const DECISION_LABELS: Record<string, string> = {
  pending: '待审', accepted: '已接受', rejected: '已驳回', ignored: '已忽略',
};
const VERDICT_LABELS: Record<string, string> = {
  writer1_error: '编写者1稿件存在问题',
  auditor_false_positive: '文本审核者意见不成立',
};
const EVIDENCE_LABELS: Record<string, string> = {
  audited_text: '已审核稿件', writer_draft: '编写者1草稿', source_material: '写作素材',
};

export default function AITextSelfUpgradePanel({ refreshToken = 0 }: { refreshToken?: number }) {
  const [page, setPage] = useState<AITextSelfUpgradePage | null>(null);
  const [selected, setSelected] = useState<AITextSelfUpgradeItem | null>(null);
  const [status, setStatus] = useState('pending');
  const [offset, setOffset] = useState(0);
  const [note, setNote] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  async function load() {
    setLoading(true);
    setError('');
    try {
      setPage(await listAITextSelfUpgradeItems({ status, offset, limit: PAGE_SIZE }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, [refreshToken, status, offset]);

  async function openItem(item: AITextSelfUpgradeItem) {
    try {
      const detail = await getAITextSelfUpgradeItem(item.case_key);
      setSelected(detail);
      setNote(detail.decision_note || '');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function decide(decision: 'accepted' | 'rejected' | 'ignored') {
    if (!selected) return;
    setSaving(true);
    setError('');
    try {
      setSelected(await decideAITextSelfUpgradeItem(selected.case_key, decision, note));
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  const latestRun = page?.latest_run;
  const auditedUrl = selected ? `https://www.odaily.news/zh-CN/newsflash/${encodeURIComponent(selected.source_item_id)}` : '';

  return (
    <section className="aiTextUpgrade">
      <div className="aiTextUpgradeToolbar">
        <label><span>人工决定</span><select value={status} onChange={(event) => { setStatus(event.target.value); setOffset(0); }}>
          <option value="pending">待审</option>
          <option value="accepted">已接受</option>
          <option value="rejected">已驳回</option>
          <option value="ignored">已忽略</option>
          <option value="all">全部</option>
        </select></label>
        <span>研究样本：{page?.accepted_count || 0} 条已接受</span>
        <span className="aiTextUpgradeRun">
          {latestRun
            ? `上次批次 ${formatDate(latestRun.started_at)} · 新增 ${latestRun.cases_found} · 完成 ${latestRun.cases_completed} · 待重试 ${latestRun.cases_failed} · 未关联 ${latestRun.cases_unlinked}`
            : '等待首次批次'}
        </span>
        <button className="iconButton" type="button" title="刷新案例" onClick={() => void load()}><RefreshCcw size={16} /></button>
      </div>
      {error && <div className="notice error" role="alert">{error}</div>}
      {latestRun?.error && <div className="notice error" role="status">最近批次失败：{latestRun.error}</div>}

      <div className="aiTextUpgradeColumns">
        <div className="aiTextUpgradeList" aria-busy={loading}>
          {page?.items.map((item) => (
            <button
              className={selected?.case_key === item.case_key ? 'aiTextUpgradeRow selected' : 'aiTextUpgradeRow'}
              type="button"
              key={item.case_key}
              onClick={() => void openItem(item)}
            >
              <span className="aiTextUpgradeRowTitle"><strong>{item.audited_title || '无标题'}</strong><span className={`aiTextUpgradeStatus ${item.human_decision}`}>{DECISION_LABELS[item.human_decision]}</span></span>
              <span className="aiTextUpgradeRowMeta">
                <span>{item.analysis_status === 'completed' ? VERDICT_LABELS[item.verdict || ''] || '已完成' : analysisLabel(item.analysis_status)}</span>
                <time>{formatDate(item.published_at || item.created_at)}</time>
              </span>
              <span className="aiTextUpgradeSummary">{item.audit_result.summary || issueSummary(item)}</span>
            </button>
          ))}
          {!loading && !page?.items.length && <div className="aiTextUpgradeEmpty">当前筛选没有案例。</div>}
          <div className="aiTextUpgradePagination">
            <button className="secondaryButton" type="button" disabled={offset === 0 || loading} onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}><ChevronLeft size={16} /> 上一页</button>
            <span>{page?.total || 0} 条案例</span>
            <button className="secondaryButton" type="button" disabled={!page || offset + PAGE_SIZE >= page.total || loading} onClick={() => setOffset((value) => value + PAGE_SIZE)}>下一页 <ChevronRight size={16} /></button>
          </div>
        </div>

        <aside className="aiTextUpgradeDetail">
          {!selected ? <div className="aiTextUpgradeEmpty">选择一条案例查看稿件和审核意见。</div> : <>
            <header className="aiTextUpgradeDetailHeader">
              <div><span>{formatDate(selected.published_at || selected.created_at)} · {DECISION_LABELS[selected.human_decision]}</span><h2>{selected.audited_title || '无标题'}</h2></div>
              <a href={auditedUrl} target="_blank" rel="noreferrer" title="打开已发布快讯"><ExternalLink size={16} /></a>
            </header>
            {selected.analysis_status === 'completed' ? (
              <section className="aiTextUpgradeVerdict">
                <div><strong>{VERDICT_LABELS[selected.verdict || ''] || '判断完成'}</strong><span>置信度：{confidenceLabel(selected.confidence)}</span><span>{selected.analysis_model || 'gpt-6-luna'}</span></div>
                <p>{selected.rationale}</p>
                {selected.evidence.map((item, index) => <blockquote key={`${item.location}-${index}`}>
                  <span>{EVIDENCE_LABELS[item.location] || item.location}</span>
                  <q>{item.quote}</q>
                  <p>{item.explanation}</p>
                </blockquote>)}
              </section>
            ) : (
              <div className={`aiTextUpgradeAnalysis ${selected.analysis_status}`}>
                {analysisLabel(selected.analysis_status)}{selected.analysis_error ? `：${selected.analysis_error}` : ''}
              </div>
            )}

            <section className="aiTextUpgradeSection">
              <h3>文本审核者意见</h3>
              <p>{selected.audit_result.summary || '未提供摘要'}</p>
              {(selected.audit_result.issues || []).map((issue, index) => (
                <article className="aiTextUpgradeIssue" key={index}>
                  <div><span>{String(issue.location || '') === 'title' ? '标题' : '正文'}</span><span>{String(issue.type || '问题')}</span></div>
                  <p><strong>原文</strong>{String(issue.original || '')}</p>
                  {issue.suggested ? <p><strong>建议</strong>{String(issue.suggested)}</p> : null}
                  <p><strong>理由</strong>{String(issue.reason || '')}</p>
                  {issue.evidence ? <p><strong>依据</strong>{String(issue.evidence)}</p> : null}
                </article>
              ))}
              <small>审核版本 {selected.audit_prompt_version || '未知'} · {selected.audit_model || '未知模型'}</small>
            </section>

            <TextBlock title="已审核稿件" titleText={selected.audited_title} content={selected.audited_content} />
            <details className="aiTextUpgradeSection aiTextUpgradeSource">
              <summary>编写者1写作素材</summary>
              <div>{selected.source_url && <a href={selected.source_url} target="_blank" rel="noreferrer">打开原始信源 <ExternalLink size={13} /></a>}
                <p className="aiTextUpgradeSourceTitle">{selected.source_title || '无素材标题'}</p>
                <pre>{selected.source_content || '无写作素材正文'}</pre>
              </div>
            </details>
            <TextBlock title="编写者1草稿" titleText={selected.draft_title} content={selected.draft_content} />
            <TextBlock title="格式化稿件" titleText={selected.final_title} content={selected.final_content} />

            {selected.analysis_status === 'completed' && <>
              <div className="aiTextUpgradeDecisionMeaning">
                接受将该 AI 判断计入研究样本；驳回和忽略均不计入。
              </div>
              <label className="aiTextUpgradeNote"><span>决定备注（可选）</span><textarea value={note} onChange={(event) => setNote(event.target.value)} rows={2} maxLength={2000} /></label>
              <div className="aiTextUpgradeActions">
                <button className="primaryButton" type="button" disabled={saving} onClick={() => void decide('accepted')}><Check size={16} /> 接受</button>
                <button className="secondaryButton" type="button" disabled={saving} onClick={() => void decide('rejected')}><X size={16} /> 驳回</button>
                <button className="secondaryButton" type="button" disabled={saving} onClick={() => void decide('ignored')}><EyeOff size={16} /> 忽略</button>
              </div>
            </>}
            {selected.decisions?.length ? <section className="aiTextUpgradeHistory"><h3>决定历史</h3>{selected.decisions.map((decision, index) => <article key={`${decision.decided_at}-${index}`}>
              <time>{formatDate(decision.decided_at)}</time><strong>{decision.actor} · {DECISION_LABELS[decision.decision] || decision.decision}</strong><p>{decision.note || '无备注'}</p>
            </article>)}</section> : null}
          </>}
        </aside>
      </div>
    </section>
  );
}

function TextBlock({ title, titleText, content }: { title: string; titleText: string | null; content: string | null }) {
  return <section className="aiTextUpgradeSection"><h3>{title}</h3>{titleText && <strong className="aiTextUpgradeTextTitle">{titleText}</strong>}<pre>{content || '无正文'}</pre></section>;
}

function issueSummary(item: AITextSelfUpgradeItem): string {
  const first = item.audit_result.issues?.[0];
  return String(first?.reason || first?.original || '文本审核者标记待复核问题');
}

function analysisLabel(status: string): string {
  return ({ pending: '等待自我审核', processing: '自我审核中', failed: '自我审核失败' } as Record<string, string>)[status] || status;
}

function confidenceLabel(value: string | null): string {
  return ({ high: '高', medium: '中', low: '低' } as Record<string, string>)[value || ''] || '未知';
}

function formatDate(value: string | null): string {
  if (!value) return '时间未知';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(date);
}
