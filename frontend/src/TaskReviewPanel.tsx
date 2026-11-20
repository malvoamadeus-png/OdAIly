import { useEffect, useState } from 'react';
import { ExternalLink, RefreshCcw } from 'lucide-react';
import { listTaskReview, type TaskReviewRow } from './xCaptureStore';
import './systemInformation.css';

const TERMINAL = new Set(['discarded', 'duplicate', 'auto_published', 'ready_review', 'publisher_failed', 'notified', 'expired', 'legacy_skipped', 'judge_failed', 'search_failed', 'write_failed']);

export default function TaskReviewPanel({ refreshToken = 0 }: { refreshToken?: number }) {
  const [items, setItems] = useState<TaskReviewRow[]>([]);
  const [selected, setSelected] = useState<TaskReviewRow | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  async function load() {
    setLoading(true);
    setError('');
    try {
      const result = await listTaskReview(100);
      setItems(result.items);
      setSelected((current) => result.items.find((item) => item.task.id === current?.task.id) || result.items[0] || null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, [refreshToken]);

  return <section className="systemInformationLayout">
    <header className="systemInformationMeta"><strong>最近五天的已结束任务与仍在处理的任务</strong><button className="iconButton" type="button" title="刷新复盘" onClick={() => void load()}><RefreshCcw size={16} /></button></header>
    {error && <div className="notice error" role="alert">{error}</div>}
    <div className="systemInformationColumns">
      <div className="systemInformationList" aria-busy={loading}>
        {items.map((item) => <button className={selected?.task.id === item.task.id ? 'systemInformationRow selected' : 'systemInformationRow'} type="button" key={String(item.task.id)} onClick={() => setSelected(item)}>
          <span className="systemInformationRowTop"><strong>{String(item.task.title || `任务 ${item.task.id}`)}</strong><span className="systemInformationResult">{String(item.task.status || 'unknown')}</span></span>
          <span className="systemInformationRowMeta"><span>{String(item.task.source || '')}</span><time>{formatDate(String(item.task.updated_at || item.task.created_at || ''))}</time></span>
          <span className="systemInformationSummary">{String(item.pipeline?.last_error || item.task.source_url || '')}</span>
        </button>)}
        {!loading && !items.length && <div className="systemInformationEmpty">没有可复盘任务。</div>}
      </div>
      <aside className="systemInformationDetail">
        {!selected ? <div className="systemInformationEmpty">选择任务查看阶段快照。</div> : <>
          <header className="systemInformationDetailHeader"><div><span>任务 {String(selected.task.id)} · {String(selected.task.status)}</span><h2>{String(selected.pipeline?.final_title || selected.task.title || '无标题')}</h2></div>{typeof selected.task.source_url === 'string' && selected.task.source_url && <a href={selected.task.source_url} target="_blank" rel="noreferrer" title="打开原始信源"><ExternalLink size={16} /></a>}</header>
          <section><h3>来源</h3><p>{String(selected.task.source || '未知来源')} · {String(selected.task.source_item_id || '')}</p><pre>{String(selected.task.content || '')}</pre></section>
          {selected.pipeline && <>
            <section><h3>判断</h3><pre>{pretty(selected.pipeline.judge_output)}</pre></section>
            <section><h3>查重与搜索</h3><pre>{pretty(selected.pipeline.search_result)}</pre></section>
            <section><h3>写作</h3><pre>{pretty({ model: selected.pipeline.writer_model, prompt: selected.pipeline.prompt_template_key, draft_title: selected.pipeline.draft_title, draft_content: selected.pipeline.draft_content, final_title: selected.pipeline.final_title, final_content: selected.pipeline.final_content })}</pre></section>
            <section><h3>发布</h3><pre>{pretty({ decision: selected.pipeline.publisher_decision, reason_code: selected.pipeline.publisher_reason_code, output: selected.pipeline.publisher_output, pushed: selected.pipeline.push_result, telegram: selected.pipeline.telegram_result, error: selected.pipeline.last_error })}</pre></section>
          </>}
          {selected.external_pipeline && <section><h3>外部媒体告警流程</h3><pre>{pretty({ route: selected.external_pipeline.domain_route, discard_reason: selected.external_pipeline.discard_reason, prompt: selected.external_pipeline.prompt_template_key, classification: selected.external_pipeline.domain_output, search: selected.external_pipeline.search_result, notification: selected.external_pipeline.telegram_result, error: selected.external_pipeline.last_error })}</pre></section>}
          <p className="systemInformationNotice">{TERMINAL.has(String(selected.task.status)) ? '终态任务在完成后五天内提供复盘。' : '任务尚未结束，继续保留。'}</p>
        </>}
      </aside>
    </div>
  </section>;
}

function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
}

function pretty(value: unknown) {
  return JSON.stringify(value ?? {}, null, 2);
}
