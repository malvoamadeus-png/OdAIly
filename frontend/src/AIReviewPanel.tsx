import { useEffect, useState } from 'react';
import { Check, ChevronLeft, ChevronRight, CircleX, Pause, RefreshCcw, Search, UserRoundSearch } from 'lucide-react';
import {
  decideAIReviewItem,
  listAIReviewItems,
  type AIReviewItem,
  type AIReviewPage,
  type AIReviewStatus,
  type AIReviewType,
} from './xCaptureStore';

const STATUS_LABELS: Record<AIReviewStatus, string> = {
  pending: '待审阅',
  accepted: '已认可',
  rejected: '已驳回',
  deferred: '暂缓',
};
const CATEGORY_LABELS: Record<string, string> = {
  competitor_first_ai: '竞品先发 · Odaily AI 发布',
  multiple_competitors_no_odaily: '多家竞品发布 · Odaily 未发',
  odaily_only: '仅 Odaily 发布',
  market_sentiment: '市场情绪账号',
  project_promotion: '项目推介账号',
};
const CATEGORY_OPTIONS: Record<AIReviewType, string[]> = {
  event: ['competitor_first_ai', 'multiple_competitors_no_odaily', 'odaily_only'],
  account: ['market_sentiment', 'project_promotion'],
};

export default function AIReviewPanel({ refreshToken = 0 }: { refreshToken?: number }) {
  const [page, setPage] = useState<AIReviewPage | null>(null);
  const [selected, setSelected] = useState<AIReviewItem | null>(null);
  const [type, setType] = useState<'all' | AIReviewType>('all');
  const [category, setCategory] = useState('all');
  const [status, setStatus] = useState<'all' | AIReviewStatus>('pending');
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  async function loadPage(nextOffset = offset) {
    setLoading(true);
    setError('');
    try {
      const data = await listAIReviewItems({ type, status, category, offset: nextOffset, limit: 30 });
      setPage(data);
      setSelected((current) => data.items.find((item) => item.itemKey === current?.itemKey) || data.items[0] || null);
      setOffset(nextOffset);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadPage(0);
  }, [type, category, status, refreshToken]);

  async function decide(decision: 'accepted' | 'rejected' | 'deferred') {
    if (!selected) return;
    setSaving(true);
    setError('');
    setMessage('');
    try {
      await decideAIReviewItem(selected.itemKey, decision);
      setMessage('已标记为' + STATUS_LABELS[decision]);
      await loadPage(offset);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  const pageCount = page ? Math.max(1, Math.ceil(page.total / page.limit)) : 1;
  const currentPage = page ? Math.floor(page.offset / page.limit) + 1 : 1;

  return (
    <section className="aiReviewPanel">
      <div className="aiReviewToolbar">
        <div className="aiReviewFilters" role="group" aria-label="审阅项目类型">
          {([['all', '全部'], ['event', '事件'], ['account', '账号']] as const).map(([value, label]) => (
            <button className={type === value ? 'active' : ''} key={value} type="button" onClick={() => { setType(value); setCategory('all'); }}>{label}</button>
          ))}
        </div>
        <label className="aiReviewSelect aiReviewCategoryFilter">
          <span>类别</span>
          <select value={category} disabled={type === 'all'} onChange={(event) => setCategory(event.target.value)}>
            <option value="all">全部类别</option>
            {type !== 'all' && CATEGORY_OPTIONS[type].map((value) => (
              <option key={value} value={value}>{CATEGORY_LABELS[value]}</option>
            ))}
          </select>
        </label>
        <div className="aiReviewFilters" role="group" aria-label="审阅状态">
          {([['pending', '待审阅'], ['accepted', '已采纳'], ['rejected', '已驳回'], ['deferred', '暂缓'], ['all', '全部']] as const).map(([value, label]) => (
            <button className={status === value ? 'active' : ''} key={value} type="button" onClick={() => setStatus(value)}>{label}</button>
          ))}
        </div>
        <button className="aiReviewIconButton" type="button" onClick={() => void loadPage(offset)} disabled={loading} title="刷新" aria-label="刷新">
          <RefreshCcw size={16} />
        </button>
      </div>

      <div className="aiReviewUsage">
        <span>账号挖掘 {formatTokens(page?.usage.account.usedTokens || 0)} / 5,000 万</span>
        <span>事件研究 {formatTokens(page?.usage.event.usedTokens || 0)} / 5,000 万</span>
        <span>当前筛选 {page?.total ?? 0} 条</span>
        {page?.weekly.candidates ? (
          <span>
            近 3 天 {page.weekly.candidates} 条 · 认可 {formatRate(page.weekly.acceptanceRate)} ·
            驳回 {formatRate(page.weekly.rejectionRate)} · 实际 {formatTokens(page.weekly.inputTokens + page.weekly.outputTokens)} tokens
          </span>
        ) : <span>近 3 天暂无建议</span>}
      </div>
      {error && <div className="aiReviewNotice error">{error}</div>}
      {message && <div className="aiReviewNotice success">{message}</div>}

      <div className="aiReviewWorkspace">
        <section className="aiReviewQueue" aria-label="审阅队列">
          <div className="aiReviewQueueHead">
            <strong>审阅队列</strong>
            <span>{loading ? '加载中' : (page?.total ?? 0) + ' 条'}</span>
          </div>
          <div className="aiReviewRows">
            {!loading && page?.items.length === 0 && <p className="aiReviewEmpty">暂无审阅项目</p>}
            {page?.items.map((item) => (
              <button
                className={selected?.itemKey === item.itemKey ? 'aiReviewRow active' : 'aiReviewRow'}
                key={item.itemKey}
                type="button"
                onClick={() => setSelected(item)}
              >
                <span className={'aiReviewTypeIcon ' + item.type} aria-hidden="true">
                  {item.type === 'event' ? <Search size={15} /> : <UserRoundSearch size={15} />}
                </span>
                <span className="aiReviewRowText">
                  <strong>{item.title}</strong>
                  <small>{CATEGORY_LABELS[item.category] || item.category} · {STATUS_LABELS[item.status]}</small>
                </span>
                <span className={'aiReviewRecommendation ' + item.recommendation}>{item.recommendationLabel}</span>
              </button>
            ))}
          </div>
          <div className="aiReviewPagination">
            <button type="button" title="上一页" aria-label="上一页" disabled={currentPage <= 1 || loading} onClick={() => void loadPage(Math.max(0, offset - 30))}><ChevronLeft size={17} /></button>
            <span>{currentPage} / {pageCount}</span>
            <button type="button" title="下一页" aria-label="下一页" disabled={currentPage >= pageCount || loading} onClick={() => void loadPage(offset + 30)}><ChevronRight size={17} /></button>
          </div>
        </section>

        <section className="aiReviewDetail" aria-label="建议详情">
          {!selected ? <p className="aiReviewEmpty">选择一条建议查看依据</p> : (
            <>
              <div className="aiReviewDetailHeader">
                <div>
                  <small>{CATEGORY_LABELS[selected.category] || selected.category}</small>
                  <h2>{selected.title}</h2>
                </div>
                <span className={'aiReviewRecommendation ' + selected.recommendation}>{selected.recommendationLabel}</span>
              </div>
              {selected.analysisStatus === 'completed' ? (
                <p className="aiReviewReason">{selected.reason || 'AI 未提供补充理由'}</p>
              ) : (
                <div className={'aiReviewAnalysisState ' + selected.analysisStatus}>
                  {selected.analysisStatus === 'queued' ? '等待 AI 分析' : 'AI 分析失败：' + (selected.analysisError || '请稍后重试')}
                </div>
              )}
              <div className="aiReviewMeta">
                <span>{selected.analysisModel || 'gpt-6-luna'}</span>
                <span>{formatTokens(selected.inputTokens + selected.outputTokens)} tokens</span>
                <span>{formatDate(selected.sourceUpdatedAt || selected.createdAt)}</span>
              </div>

              {selected.type === 'account' && (selected.clues.xAccounts?.length || selected.clues.domains?.length) ? (
                <section className="aiReviewClues">
                  <h3>来源线索</h3>
                  {selected.clues.xAccounts?.length ? <p><strong>X 账号</strong>{selected.clues.xAccounts.map((handle) => <a key={handle} href={'https://x.com/' + handle} target="_blank" rel="noreferrer">@{handle}</a>)}</p> : null}
                  {selected.clues.domains?.length ? <p><strong>网站域名</strong>{selected.clues.domains.map((domain) => <span key={domain}>{domain} <small>仅线索</small></span>)}</p> : null}
                </section>
              ) : null}

              <section className="aiReviewEvidenceList">
                <h3>{selected.type === 'event' ? '来源原文' : '证据原文'}</h3>
                {selected.evidence.map((evidence, index) => (
                  <article key={(evidence.tweetId || evidence.url || evidence.source || 'evidence') + '-' + index} className="aiReviewEvidence">
                    <div className="aiReviewEvidenceMeta">
                      {evidence.sourceKind && <span className={'aiReviewSourceKind ' + evidence.sourceKind}>{evidence.sourceKind === 'odaily' ? 'Odaily' : '竞品'}</span>}
                      <strong>{evidence.source || 'X'}</strong>
                      {evidence.sourceAccount && <a href={'https://x.com/' + evidence.sourceAccount} target="_blank" rel="noreferrer">@{evidence.sourceAccount}</a>}
                      {evidence.sourceDomain && <span>{evidence.sourceDomain}</span>}
                      {evidence.sourceKind === 'competitor' && evidence.tracked != null && (
                        <small className={evidence.tracked ? 'aiReviewTrackingState tracked' : 'aiReviewTrackingState'}>
                          {evidence.tracked ? '已追踪' : '可考虑追踪'}
                        </small>
                      )}
                      <time>{formatDate(evidence.postedAt || evidence.publishedAt)}</time>
                      {evidence.url && <a href={evidence.url} target="_blank" rel="noreferrer">打开原文</a>}
                    </div>
                    {evidence.title && <h4>{evidence.title}</h4>}
                    <p>{evidence.text || '无正文'}</p>
                    {(evidence.matchedAssets?.length || evidence.matchedProjects?.length) ? (
                      <small>命中：{[...(evidence.matchedAssets || []), ...(evidence.matchedProjects || [])].join('、')}</small>
                    ) : null}
                  </article>
                ))}
              </section>

              {selected.type === 'event' && selected.status === 'pending' && (
                <div className="aiReviewActions">
                  <button type="button" disabled={saving} onClick={() => void decide('accepted')}><Check size={15} /> 认可</button>
                  <button type="button" disabled={saving} onClick={() => void decide('deferred')}><Pause size={15} /> 暂缓</button>
                  <button type="button" disabled={saving} onClick={() => void decide('rejected')}><CircleX size={15} /> 驳回</button>
                </div>
              )}
              {selected.type === 'account' && selected.status === 'pending' && (
                <div className="aiReviewActions">
                  <button type="button" disabled={saving} onClick={() => void decide('accepted')}><Check size={15} /> 认可</button>
                  <button type="button" disabled={saving} onClick={() => void decide('rejected')}><CircleX size={15} /> 驳回账号建议</button>
                </div>
              )}
            </>
          )}
        </section>
      </div>
    </section>
  );
}

function formatTokens(value: number): string {
  return value >= 1000000 ? (value / 1000000).toFixed(1) + 'M' : value.toLocaleString();
}

function formatRate(value: number): string {
  return (value * 100).toFixed(0) + '%';
}

function formatDate(value?: string | null): string {
  if (!value) return '时间未知';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}
