import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { ArrowDownWideNarrow, ArrowUpWideNarrow, ChevronLeft, ChevronRight, ExternalLink, RefreshCcw, Search } from 'lucide-react';
import {
  getSystemInformationDetail,
  getSystemInformationRegistry,
  listSystemInformation,
  type SystemInformationDetail,
  type SystemInformationItem,
  type SystemInformationPage,
  type SystemInformationRegistry,
} from './xCaptureStore';
import './systemInformation.css';

const PAGE_SIZE = 50;

const STATUS_LABELS: Record<string, string> = {
  pending: '处理中', judged: '处理中', searched: '处理中', written: '处理中', formatted: '处理中',
  publisher_pending: '挂后台', ready_review: '挂后台', auto_published: '直发', published: '已发布',
  discarded: '正常停止', deduped: '重复停止', duplicate: '重复停止', failed: '失败',
  enabled: '已启用', disabled: '已停用', configured: '已配置',
};

const VIEW_BY_BUSINESS: Record<string, string> = {
  x: 'x', binance_square: 'binance_square', crypto_media: 'non_mainstream', ai_media: 'ai_source',
  mixed_media: 'mixed_source', jin10: 'jin10', business_news: 'business_news', hyperliquid_news: 'hyperliquid_news',
  whale: 'whale', meme: 'meme', gate_market: 'gate_market', stock_market: 'stock_market',
  x_agent: 'x_agent', post_publish_review: 'newsflash',
};

type Props = {
  refreshToken?: number;
  onOpenBusiness?: (view: string) => void;
  onOpenWorkflow?: (itemId: string) => void;
};

export default function SystemInformationPanel({ refreshToken = 0, onOpenBusiness, onOpenWorkflow }: Props) {
  const [page, setPage] = useState<SystemInformationPage | null>(null);
  const [registry, setRegistry] = useState<SystemInformationRegistry | null>(null);
  const [detail, setDetail] = useState<SystemInformationDetail | null>(null);
  const [query, setQuery] = useState('');
  const [business, setBusiness] = useState('all');
  const [source, setSource] = useState('all');
  const [stage, setStage] = useState('all');
  const [status, setStatus] = useState('all');
  const [result, setResult] = useState('all');
  const [failure, setFailure] = useState('all');
  const [since, setSince] = useState('');
  const [until, setUntil] = useState('');
  const [sort, setSort] = useState<'created' | 'activity'>('created');
  const [cursor, setCursor] = useState<{ time: number; id: string } | null>(null);
  const [previousCursors, setPreviousCursors] = useState<Array<{ time: number; id: string } | null>>([]);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState('');

  const businessLabels = useMemo(() => new Map((registry?.registry || []).map((item) => [item.key, item.label])), [registry]);
  const sourceOptions = useMemo(() => {
    if (!registry) return [];
    if (business === 'all') return registry.sources;
    return registry.sources.filter((item) => (registry.registry.find((entry) => entry.key === item.business_key)?.key || '') === business);
  }, [business, registry]);

  async function loadPage(nextCursor: { time: number; id: string } | null, nextSort = sort) {
    setLoading(true);
    setError('');
    try {
      const data = await listSystemInformation({
        q: query.trim(), business, source, stage, status, result, failure_category: failure,
        since: since ? new Date(since).toISOString() : '', until: until ? new Date(until).toISOString() : '',
        sort: nextSort, cursor: nextCursor, limit: PAGE_SIZE,
      });
      setPage(data);
      setCursor(nextCursor);
      setDetail(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void getSystemInformationRegistry().then(setRegistry).catch((err: Error) => setError(err.message));
  }, []);

  useEffect(() => {
    setPreviousCursors([]);
    void loadPage(null);
  }, [refreshToken]);

  async function openDetail(item: SystemInformationItem) {
    setDetailLoading(true);
    setError('');
    try {
      setDetail(await getSystemInformationDetail(item.item_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDetailLoading(false);
    }
  }

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPreviousCursors([]);
    void loadPage(null);
  }

  function nextPage() {
    if (!page?.next_cursor) return;
    setPreviousCursors((current) => [...current, cursor]);
    void loadPage(page.next_cursor);
  }

  function previousPage() {
    if (!previousCursors.length) return;
    const next = previousCursors[previousCursors.length - 1];
    setPreviousCursors((current) => current.slice(0, -1));
    void loadPage(next);
  }

  function changeSort(next: 'created' | 'activity') {
    setSort(next);
    setPreviousCursors([]);
    void loadPage(null, next);
  }

  return (
    <section className="systemInformationLayout">
      <form className="systemInformationFilters" onSubmit={applyFilters}>
        <label className="systemInformationSearch">
          <span>关键词</span>
          <div><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="标题、稿件、来源、ID、URL" /></div>
        </label>
        <label><span>业务</span><select value={business} onChange={(event) => { setBusiness(event.target.value); setSource('all'); }}>
          <option value="all">全部业务</option>
          {(registry?.registry || []).map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}
          <option value="unmapped_source">未登记来源</option>
        </select></label>
        <label><span>来源</span><select value={source} onChange={(event) => setSource(event.target.value)}>
          <option value="all">全部来源</option>
          {sourceOptions.map((item) => <option key={item.source} value={item.source}>{item.source} ({item.items})</option>)}
        </select></label>
        <label><span>环节</span><select value={stage} onChange={(event) => setStage(event.target.value)}>
          <option value="all">全部环节</option><option>已收集</option><option>判断</option><option>查重</option><option>写作</option><option>稿件整理</option><option>投递</option><option>业务停止</option>
        </select></label>
        <label><span>状态</span><select value={status} onChange={(event) => setStatus(event.target.value)}>
          <option value="all">全部状态</option><option value="processing">处理中</option><option value="backoffice">挂后台</option><option value="direct_publish">直发</option><option value="normal_stop">正常停止</option><option value="failed">失败</option>
        </select></label>
        <label><span>结果</span><select value={result} onChange={(event) => setResult(event.target.value)}>
          <option value="all">全部结果</option><option value="processing">处理中</option><option value="backoffice">挂后台</option><option value="direct_publish">直发</option><option value="stopped">正常停止</option><option value="failed">失败</option>
        </select></label>
        <label><span>故障类别</span><select value={failure} onChange={(event) => setFailure(event.target.value)}>
          <option value="all">全部</option><option value="program_or_unknown">程序或未知</option>
        </select></label>
        <label><span>开始时间</span><input type="datetime-local" value={since} onChange={(event) => setSince(event.target.value)} /></label>
        <label><span>结束时间</span><input type="datetime-local" value={until} onChange={(event) => setUntil(event.target.value)} /></label>
        <button className="primaryButton" type="submit"><Search size={16} /> 查询</button>
      </form>

      {error && <div className="notice error" role="alert">{error}</div>}
      <div className="systemInformationMeta">
        <span>{loading ? '正在查询' : `${page?.total ?? 0} 条记录`}</span>
        <span>索引 {page?.index.generated_at_ms ? fmtDate(page.index.generated_at_ms) : '初始化中'}</span>
        <span>{page?.index.lag ? `主流程积压 ${page.index.lag} 条` : '主流程索引已追平'}</span>
        {!page?.search_index_ready && <span>关键词索引初始化中</span>}
        <span>{page?.search_scope}</span>
        <div className="systemInformationSort" role="group" aria-label="排序方式">
          <button type="button" className={sort === 'created' ? 'active' : ''} onClick={() => changeSort('created')}><ArrowDownWideNarrow size={15} /> 创建时间</button>
          <button type="button" className={sort === 'activity' ? 'active' : ''} onClick={() => changeSort('activity')}><ArrowUpWideNarrow size={15} /> 最近活动</button>
        </div>
        <button className="iconButton" type="button" title="刷新列表" onClick={() => { setPreviousCursors([]); void loadPage(null); }}><RefreshCcw size={16} /></button>
      </div>

      <div className="systemInformationColumns">
        <div className="systemInformationList" aria-busy={loading}>
          {page?.items.map((item) => (
            <button className={detail?.item_id === item.item_id ? 'systemInformationRow selected' : 'systemInformationRow'} type="button" key={item.item_id} onClick={() => void openDetail(item)}>
              <span className="systemInformationRowTop">
                <strong>{item.title || item.source_id || item.item_id}</strong>
                <span className={`systemInformationResult result-${item.result}`}>{resultLabel(item.result)}</span>
              </span>
              <span className="systemInformationRowMeta">
                <span>{businessLabels.get(item.business_key) || item.business_key}</span><span>{item.source}</span><span>{item.stage}</span>
                <span>{STATUS_LABELS[item.status] || item.status}</span><time>{fmtDate(item.created_at_ms)}</time>
              </span>
              <span className="systemInformationSummary">{item.summary || '当时未保存原因'}</span>
              {Boolean(item.legacy_history) && <span className="systemInformationLegacy">仅有当前状态</span>}
            </button>
          ))}
          {!loading && !page?.items.length && <div className="emptyState">没有匹配的处理记录。</div>}
          <div className="systemInformationPagination">
            <button className="secondaryButton" type="button" disabled={!previousCursors.length || loading} onClick={previousPage}><ChevronLeft size={16} /> 上一页</button>
            <span>{page?.items.length || 0} / {page?.total || 0}</span>
            <button className="secondaryButton" type="button" disabled={!page?.next_cursor || loading} onClick={nextPage}>下一页 <ChevronRight size={16} /></button>
          </div>
        </div>

        <aside className="systemInformationDetail">
          {detailLoading ? <div className="emptyState">正在读取详情…</div> : detail ? (
            <>
              <div className="systemInformationDetailHeader">
                <div><span>{businessLabels.get(detail.business_key) || detail.business_key} · {detail.source}</span><h2>{detail.title || detail.source_id}</h2></div>
                {detail.source_url && <a className="iconButton" href={detail.source_url} target="_blank" rel="noreferrer" title="打开来源"><ExternalLink size={16} /></a>}
              </div>
              <div className="systemInformationOutcome"><strong>{resultLabel(detail.result)}</strong><span>{detail.stage} · {STATUS_LABELS[detail.status] || detail.status}</span><p>{detail.summary || '当时未保存原因'}</p></div>
              {Boolean(detail.legacy_history) && <p className="systemInformationNotice">此历史记录只保留当前状态；早期尝试、当时未保存的理由和规则不能补造。</p>}
              <section><h3>处理时间线</h3>
                {detail.timeline.map((event) => <article className="systemInformationEvent" key={event.event_id}>
                  <time>{fmtDate(event.happened_at_ms || event.observed_at_ms)}</time><strong>{event.stage} · {event.summary}</strong>
                  {event.reason && <p>{event.reason}</p>}
                  {event.error_code && <span>错误码：{event.error_code}</span>}
                  {Boolean(event.details.history_note) && <p>{String(event.details.history_note)}</p>}
                  {typeof event.evidence?.raw_model_response === 'string' && <EvidenceText label="模型原始返回" value={event.evidence.raw_model_response} />}
                  {typeof event.evidence?.raw_model_response_excerpt === 'string' && <EvidenceText label="模型返回摘录" value={event.evidence.raw_model_response_excerpt} />}
                  {typeof event.details.response_archive_error === 'string' && <p className="systemInformationNotice">原始返回归档失败：{event.details.response_archive_error}</p>}
                  {Boolean(event.details.input || event.details.rules) && <details className="systemInformationTechnical"><summary>本次输入与生效规则</summary><pre>{JSON.stringify({ input: event.details.input, rules: event.details.rules }, null, 2)}</pre></details>}
                </article>)}
              </section>
              {detail.evidence?.task && <section><h3>来源材料与稿件</h3>
                <EvidenceText label="采集原文" value={detail.evidence.task.content} />
                <EvidenceText label="定稿标题" value={detail.evidence.pipeline?.final_title} />
                <EvidenceText label="定稿正文" value={detail.evidence.pipeline?.final_content} />
                <EvidenceText label="写作输出" value={detail.evidence.pipeline?.writer_output} />
                <EvidenceText label="实际投递回执" value={detail.evidence.pipeline?.push_result} />
              </section>}
              <details className="systemInformationTechnical"><summary>技术记录</summary><pre>{JSON.stringify(detail.evidence, null, 2)}</pre></details>
              {onOpenWorkflow && <button className="secondaryButton" type="button" onClick={() => onOpenWorkflow(detail.item_id)}>查看实际路径</button>}
              {onOpenBusiness && VIEW_BY_BUSINESS[detail.business_key] && <button className="secondaryButton" type="button" onClick={() => onOpenBusiness(VIEW_BY_BUSINESS[detail.business_key])}>打开业务工作区 <ExternalLink size={15} /></button>}
            </>
          ) : <div className="emptyState">选择一条记录查看来源、处理原因和证据。</div>}
        </aside>
      </div>
    </section>
  );
}

function EvidenceText({ label, value }: { label: string; value: unknown }) {
  const text = typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value, null, 2);
  if (!text) return null;
  return <details className="systemInformationEvidence"><summary>{label}</summary><pre>{text}</pre></details>;
}

function resultLabel(result: string): string {
  const labels: Record<string, string> = {
    processing: '处理中', backoffice: '挂后台', direct_publish: '已直发', stopped: '正常停止', failed: '失败', unknown: '结果未知', configuration_change: '配置生效变更',
  };
  return labels[result] || result;
}

function fmtDate(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(value);
}
