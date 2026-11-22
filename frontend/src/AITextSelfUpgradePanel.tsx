import { useEffect, useRef, useState } from 'react';
import { Check, ChevronLeft, ChevronRight, ExternalLink, RefreshCcw, X, EyeOff, Trash2 } from 'lucide-react';
import {
  decideAITextSelfUpgradeItem,
  deleteAITextSelfUpgradeItem,
  getAITextSelfUpgradeItem,
  listAITextSelfUpgradeItems,
  type AITextSelfUpgradeItem,
  type AITextSelfUpgradeListItem,
  type AITextSelfUpgradePage,
} from './xCaptureStore';
import './aiTextSelfUpgrade.css';

const PAGE_SIZE = 30;
const DECISION_LABELS: Record<string, string> = {
  pending: '待审', accepted: '历史决定：接受AI判断', rejected: '历史决定：驳回AI判断', ignored: '已忽略',
  reviewer_correct: '审核者正确', writer_correct: '原文正确',
};
const VERDICT_LABELS: Record<string, string> = {
  writer1_error: '审核者意见成立，Writer1稿件有误',
  reviewer_correct_unattributed: '审核者意见成立，未归因 Writer1',
  auditor_false_positive: '审核者意见不成立',
  fact_auditor_error: '事实审查者意见不成立',
  inconclusive: '现有证据无法定论',
};
const EVIDENCE_LABELS: Record<string, string> = {
  published_text: '已发布稿件', writer_draft: '编写者1草稿',
  source_material: '原始素材', reviewer_record: '审核意见记录',
};
const REVIEWER_LABELS: Record<string, string> = {
  text_auditor: '文本审查者', fact_auditor: '事实审查者',
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
  const requestVersion = useRef(0);

  async function load() {
    const version = ++requestVersion.current;
    setLoading(true);
    setError('');
    try {
      const result = await listAITextSelfUpgradeItems({ scope: 'manual_recent', status, offset, limit: PAGE_SIZE });
      if (version === requestVersion.current) setPage(result);
    } catch (err) {
      if (version === requestVersion.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (version === requestVersion.current) setLoading(false);
    }
  }

  useEffect(() => { void load(); return () => { requestVersion.current += 1; }; }, [refreshToken, status, offset]);

  async function openItem(item: AITextSelfUpgradeListItem) {
    try {
      const detail = await getAITextSelfUpgradeItem(item.case_key);
      setSelected(detail);
      setNote(detail.decision_note || '');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function decide(decision: 'reviewer_correct' | 'writer_correct' | 'ignored') {
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

  async function removeSelected() {
    if (!selected || saving) return;
    setSaving(true);
    setError('');
    try {
      await deleteAITextSelfUpgradeItem(selected.case_key);
      setSelected(null);
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
          <option value="confirmed">已最终审核</option>
          <option value="ignored">已忽略</option>
          <option value="legacy">历史决定</option>
          <option value="all">全部</option>
        </select></label>
        <span>研究样本：{page?.confirmed_count || 0} 条人工确认</span>
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
              <span className="aiTextUpgradeRowTitle"><strong>{item.audited_title || '无标题'}</strong><span className={`aiTextUpgradeStatus ${item.human_resolution || item.human_decision}`}>{decisionLabel(item)}</span></span>
              <span className="aiTextUpgradeRowMeta">
                <span>#{item.id} · {item.analysis_status === 'completed' ? verdictLabel(item) : analysisLabel(item.analysis_status)}</span>
                <time>{formatDate(item.published_at || item.created_at)}</time>
              </span>
              <span className="aiTextUpgradeSummary">{item.audit_result.summary || '文本审查者标记待复核问题'}</span>
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
              <div><span>#{selected.id} · {REVIEWER_LABELS[selected.source_type]} · {formatDate(selected.published_at || selected.created_at)} · {decisionLabel(selected)}</span><h2>{selected.audited_title || '无标题'}</h2></div>
              <a href={auditedUrl} target="_blank" rel="noreferrer" title="打开已发布快讯"><ExternalLink size={16} /></a>
            </header>
            <button className="secondaryButton" type="button" disabled={saving} onClick={() => void removeSelected()}><Trash2 size={16} /> 删除此案例</button>
            {selected.analysis_status === 'completed' ? (
              <section className="aiTextUpgradeVerdict">
                <div><strong>{verdictLabel(selected) || '判断完成'}</strong><span>置信度：{confidenceLabel(selected.confidence)}</span><span>{selected.analysis_model || 'gpt-6-luna'} · {selected.analysis_prompt_version || '未知Prompt版本'}</span></div>
                {isManualPublication(selected) && selected.analysis_prompt_version !== 'ai_text_self_upgrade_v4' ? <>
                  <p>本案为人工稿，Writer1 归因不适用。以下保留旧版模型理由。</p>
                  <details className="aiTextUpgradeSection aiTextUpgradeSource"><summary>旧版模型理由</summary><p>{selected.rationale}</p></details>
                </> : <p>{selected.rationale}</p>}
                {uniqueEvidence(selected.evidence).map((item, index) => <blockquote key={`${item.location}-${index}`}>
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

            {selected.source_type === 'text_auditor' ? <section className="aiTextUpgradeSection">
              <h3>文本审查者意见</h3>
              <p>{selected.audit_result.summary || '文本审查者标记了待复核问题。'}</p>
              {(selected.audit_result.issues || []).map((issue, index) => (
                <article className="aiTextUpgradeIssue" key={index}>
                  <div><span>{String(issue.type || '文本问题')}</span><span>{String(issue.location || '正文')}</span></div>
                  <p><strong>原文</strong>{String(issue.original || '')}</p>
                  <p><strong>建议</strong>{String(issue.suggested || '')}</p>
                  <p><strong>理由</strong>{String(issue.reason || '')}</p>
                </article>
              ))}
              <small>文本审核版本 {selected.audit_prompt_version || '未知'} · {selected.audit_model || '未知模型'}</small>
            </section> : <section className="aiTextUpgradeSection">
              <h3>事实审查者意见</h3>
              <p>{selected.fact_summary || selected.fact_reason || '未提供摘要'}</p>
              {(selected.fact_result.stages?.comparison?.issues || []).map((issue, index) => (
                <article className="aiTextUpgradeIssue" key={index}>
                  <div><span>快讯原句</span><span>事实疑点</span></div>
                  <p><strong>原文</strong>{String(issue.original || '')}</p>
                  <p><strong>理由</strong>{String(issue.reason || '')}</p>
                  {issue.evidence ? <p><strong>原文依据</strong>{String(issue.evidence)}</p> : null}
                </article>
              ))}
              {(selected.fact_result.questions || selected.fact_result.stages?.comparison?.questions || []).map((question, index) => (
                <article className="aiTextUpgradeIssue" key={`question-${index}`}>
                  <div><span>待核问题</span><span>{String(question.kind || '事实疑点')}</span></div>
                  <p><strong>稿件主张</strong>{String(question.claim || '')}</p>
                  <p><strong>问题</strong>{String(question.question || '')}</p>
                  {question.source_evidence ? <p><strong>素材依据</strong>{String(question.source_evidence)}</p> : null}
                </article>
              ))}
              {(selected.fact_result.stages?.external?.findings || []).map((finding, index) => (
                <article className="aiTextUpgradeIssue" key={`finding-${index}`}>
                  <div><span>外部核查</span><span>{String(finding.verdict || 'unverified')}</span></div>
                  <p><strong>主张</strong>{String(finding.claim || '')}</p>
                  <p><strong>判断</strong>{String(finding.reason || '')}</p>
                  {Array.isArray(finding.evidence) ? finding.evidence.map((evidence, evidenceIndex) => {
                    const entry = evidence as Record<string, unknown>;
                    const href = safeExternalUrl(entry.url);
                    return <p key={`evidence-${evidenceIndex}`}><strong>证据</strong>{String(entry.quote || '')}{href ? <> · <a href={href} target="_blank" rel="noreferrer">来源</a></> : null}</p>;
                  }) : null}
                </article>
              ))}
              <small>事实审核版本 {selected.fact_policy_version || '未知'} · {selected.fact_model || '未知模型'} · {selected.fact_status || '未知状态'}</small>
            </section>}

            <TextBlock title="已审核稿件" titleText={selected.audited_title} content={selected.audited_content} />
            {selected.source_type === 'fact_auditor' ? <details className="aiTextUpgradeSection aiTextUpgradeSource">
              <summary>事实审查者核对的原文</summary>
              <div>{selected.fact_original_url && <a href={safeExternalUrl(selected.fact_original_url) || undefined} target="_blank" rel="noreferrer">打开事实核查原文 <ExternalLink size={13} /></a>}
                <pre>{selected.fact_original_text || '事实审核者未保存可用原文'}</pre>
              </div>
            </details> : null}
            <details className="aiTextUpgradeSection aiTextUpgradeSource">
              <summary>编写者1写作素材</summary>
              <div>{selected.source_url && <a href={safeExternalUrl(selected.source_url) || undefined} target="_blank" rel="noreferrer">打开原始信源 <ExternalLink size={13} /></a>}
                <p className="aiTextUpgradeSourceTitle">{selected.source_title || '无素材标题'}</p>
                <pre>{selected.source_content || '无写作素材正文'}</pre>
              </div>
            </details>
            <div className="aiTextUpgradeDecisionMeaning">
              人工确认审核意见正确或原文无误的案例计入研究样本。
            </div>
            <label className="aiTextUpgradeNote"><span>决定备注（可选）</span><textarea value={note} onChange={(event) => setNote(event.target.value)} rows={2} maxLength={2000} /></label>
            <div className="aiTextUpgradeActions">
              <button className="primaryButton" type="button" disabled={saving} onClick={() => void decide('reviewer_correct')}><Check size={16} /> 审核者正确</button>
              <button className="secondaryButton" type="button" disabled={saving} onClick={() => void decide('writer_correct')}><X size={16} /> 原文正确</button>
              <button className="secondaryButton" type="button" disabled={saving} onClick={() => void decide('ignored')}><EyeOff size={16} /> 忽略</button>
            </div>
            {selected.decisions?.length ? <section className="aiTextUpgradeHistory"><h3>决定历史</h3>{selected.decisions.map((decision, index) => <article key={`${decision.decided_at}-${index}`}>
              <time>{formatDate(decision.decided_at)}</time><strong>{decision.actor} · {decision.legacy && decision.decision === 'ignored' ? '历史决定：忽略' : DECISION_LABELS[decision.decision] || decision.decision}</strong><p>{decision.note || '无备注'}</p>
            </article>)}</section> : null}
          </>}
        </aside>
      </div>
    </section>
  );
}

function decisionLabel(item: AITextSelfUpgradeListItem): string {
  if (item.human_resolution) return DECISION_LABELS[item.human_resolution];
  if (item.human_decision === 'ignored') return '历史决定：忽略';
  return DECISION_LABELS[item.human_decision];
}

function verdictLabel(item: AITextSelfUpgradeListItem): string {
  if (item.verdict === 'reviewer_correct_unattributed') {
    const path = effectiveAuthoringPath(item);
    if (path === 'manual_publication') return '审核者意见成立，人工稿，Writer1不适用';
    if (path === 'unknown_publication') return '审核者意见成立，来源未知，未归因 Writer1';
  }
  return VERDICT_LABELS[item.verdict || ''] || (item.verdict ? '已完成' : '判断完成');
}

function effectiveAuthoringPath(item: AITextSelfUpgradeListItem): string | undefined {
  const rejection = item.audit_result.manual_rejection;
  if (rejection?.authoring_path === 'unknown_publication'
      && rejection.writer1_snapshot === 'unavailable'
      && rejection.authoring_basis !== 'explicit_non_manual_metadata'
      && rejection.authoring_basis !== 'ambiguous_writer1_mapping') {
    return 'manual_publication';
  }
  return rejection?.authoring_path;
}

function isManualPublication(item: AITextSelfUpgradeItem): boolean {
  return effectiveAuthoringPath(item) === 'manual_publication';
}

function uniqueEvidence(evidence: AITextSelfUpgradeItem['evidence']) {
  const seen = new Set<string>();
  return evidence.filter((item) => {
    if (item.location !== 'writer_draft' && item.location !== 'published_text') return true;
    const quote = item.location === 'published_text' ? item.quote.replace(/[（(][A-Za-z][A-Za-z0-9 ._-]{0,40}[）)]\s*[。.]?\s*$/, '') : item.quote;
    const key = quote.normalize('NFKC').replace(/\s+/g, '').replace(/[。.!！?？]+$/, '').toLowerCase();
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function TextBlock({ title, titleText, content }: { title: string; titleText: string | null; content: string | null }) {
  return <section className="aiTextUpgradeSection"><h3>{title}</h3>{titleText && <strong className="aiTextUpgradeTextTitle">{titleText}</strong>}<pre>{content || '无正文'}</pre></section>;
}

function analysisLabel(status: string): string {
  return ({ pending: '等待第三方审核', processing: '第三方审核中', failed: '第三方审核失败' } as Record<string, string>)[status] || status;
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

function safeExternalUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : null;
  } catch {
    return null;
  }
}
