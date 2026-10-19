import { useEffect, useState } from 'react';
import { newsflashOperations, type FactReviewDetail, type FactReviewStatus } from './newsflashOperations';

export const factReviewLabels: Record<FactReviewStatus, string> = {
  unreviewed: '未审查',
  pending: '待查', processing: '审查中', skipped: '无需审查', exempt: '豁免',
  passed: '未发现问题', flagged: '发现疑点', unverifiable: '无法确认', failed: '执行失败',
};

export function factReviewUrl(sourceId: string, checkId?: number | null): string {
  const params = new URLSearchParams({ view: 'newsflash', fact_review: sourceId });
  if (checkId) params.set('check_id', String(checkId));
  return `${window.location.pathname}?${params.toString()}`;
}

function evidenceUrl(value: string): string | undefined {
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined; }
  catch { return undefined; }
}

export default function FactReviewDetailPanel({ sourceId, checkId, refreshToken }: { sourceId: string; checkId?: number; refreshToken: number }) {
  const [data, setData] = useState<FactReviewDetail | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  async function load() {
    setLoading(true); setError('');
    try { setData(await newsflashOperations<FactReviewDetail>('fact_review', { source_item_id: sourceId, ...(checkId ? { check_id: checkId } : {}) })); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setLoading(false); }
  }
  useEffect(() => { setData(null); void load(); }, [sourceId, checkId, refreshToken]);
  const check = data?.check;
  const comparison = check?.result.stages?.comparison;
  const external = check?.result.stages?.external;
  const screening = check?.result.stages?.screening;
  return <section className="newsflashSection factReviewDetail">
    <div className="sectionCommandBar"><a href={`${window.location.pathname}?view=newsflash`}>← 返回快讯总览</a><button type="button" className="iconTextButton" disabled={loading} onClick={() => void load()}>{loading ? '加载中…' : '刷新审查结果'}</button></div>
    <h2>事实审查 · {sourceId}</h2>
    {error && <div className="notice error">{error}</div>}
    {data && <>
      <h3>{check?.title || data.published.title || '无标题'}</h3>
      <a href={`https://www.odaily.news/zh-CN/newsflash/${encodeURIComponent(sourceId)}`} target="_blank" rel="noreferrer">查看已发布快讯 ↗</a>
      {!check ? <div className="notice">未审查：尚无当前稿件版本的事实审查记录。自动扫描仅覆盖配置时间窗口内的快讯。</div> : <>
        {!data.is_current && <div className="notice">这是历史稿件版本的审查记录，当前发布稿件已发生变化。<a href={factReviewUrl(sourceId)}>查看当前版本</a></div>}
        <div className={`factReviewConclusion factReview-${check.status}`}><strong>{check.label}</strong><p>{check.summary || check.reason}</p>{check.result.incomplete && <p>部分核查尚无法完成，请结合下方各阶段结果判断。</p>}</div>
        <p className="factReviewMeta">模型：{check.model} · 更新时间：{new Date(check.updated_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })} · 尝试次数：{check.attempts}</p>
        <section className="factReviewBlock"><h3>为什么查 / 为什么不查</h3><p>{check.reason || screening?.result?.reason || '—'}</p>
          {check.result.exclusion_rules?.map((rule) => <p key={rule.rule_key}>{rule.name} · {rule.match_target === 'title' ? '匹配标题' : '匹配标题与正文'} · {rule.terms.join('、')}</p>)}
        </section>
        {check.last_error && <div className="notice error">执行失败：{check.last_error}</div>}
        <div className="factReviewColumns">
          <section className="factReviewBlock"><h3>本次审查的已发布稿件</h3><div className="factReviewCopy">{check.content}</div></section>
          <section className="factReviewBlock"><h3>原始材料</h3>{check.original_url && evidenceUrl(check.original_url) && <a href={evidenceUrl(check.original_url)} target="_blank" rel="noreferrer">打开原文 ↗</a>}<div className="factReviewCopy">{check.original_text || '本次未获取原文；未获取不代表审查通过。'}</div></section>
        </div>
        <section className="factReviewBlock"><h3>原文审查</h3><p>{comparison?.result?.summary || '本次未进入原文审查。'}</p>
          {comparison?.result?.issues?.map((issue, index) => <article className="factReviewIssue" key={index}><blockquote>{issue.original}</blockquote><p>{issue.reason}</p>{issue.evidence && <p>原文依据：{issue.evidence}</p>}</article>)}
        </section>
        <section className="factReviewBlock"><h3>外部事实审查</h3><p>{external?.result?.summary || '本次未进入外部事实审查。'}</p>
          {external?.result?.findings?.map((finding, index) => <article className="factReviewIssue" key={index}>
            <strong>{({ supported: '有证据支持', contradicted: '有证据冲突', unverified: '无法确认' } as Record<string, string>)[finding.verdict] || finding.verdict}</strong>
            <blockquote>{finding.claim}</blockquote><p>{finding.question}</p><p>{finding.reason}</p>
            {finding.evidence.map((item, evidenceIndex) => <div key={evidenceIndex}><blockquote>{item.quote}</blockquote>{evidenceUrl(item.url) && <a href={evidenceUrl(item.url)} target="_blank" rel="noreferrer">{item.url}</a>}</div>)}
          </article>)}
        </section>
        <details className="factReviewBlock"><summary>审查版本记录</summary>{data.history.map((item) => <p key={item.id}><a href={factReviewUrl(sourceId, item.id)}>#{item.id} · {factReviewLabels[item.status]} · {item.summary || item.updated_at}</a></p>)}</details>
        <details className="factReviewBlock"><summary>执行记录</summary>{data.runs?.map((item) => <p key={item.id}>#{item.id} · {factReviewLabels[item.status]} · {item.summary} · {new Date(item.completed_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}</p>)}</details>
      </>}
    </>}
  </section>;
}
