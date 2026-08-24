import { AlertTriangle, CheckCircle2, ExternalLink, Search } from 'lucide-react';
import { FormEvent, useState } from 'react';
import { searchNewsflashDuplicates, type DuplicateSearchCandidate, type DuplicateSearchResult } from './xCaptureStore';

function formatCandidateTime(value: string | null): string {
  if (!value) return '时间未知';
  const raw = value.trim();
  const normalized = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(raw)
    ? `${raw.replace(' ', 'T')}Z`
    : raw;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

function candidateTypeLabel(candidate: DuplicateSearchCandidate): string {
  return candidate.target_type === 'inflight_candidate' ? '运行中候选' : '已发布快讯';
}

function SimilarCandidate({ candidate }: { candidate: DuplicateSearchCandidate }) {
  const similarity = Math.max(0, Math.min(1, Number(candidate.similarity) || 0));
  return (
    <article className="duplicateCandidate">
      <div className="duplicateCandidateMeta">
        <span className="duplicateCandidateType">{candidateTypeLabel(candidate)}</span>
        <span>{formatCandidateTime(candidate.published_at)}</span>
        <strong>相似度 {(similarity * 100).toFixed(1)}%</strong>
      </div>
      <h3>{candidate.title || '无标题快讯'}</h3>
      {candidate.source_url && (
        <a href={candidate.source_url} target="_blank" rel="noreferrer">
          打开原文 <ExternalLink size={14} />
        </a>
      )}
    </article>
  );
}

export default function DuplicateSearchPanel() {
  const [text, setText] = useState('');
  const [result, setResult] = useState<DuplicateSearchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = text.trim();
    if (!normalized) {
      setError('请先粘贴需要查重的快讯内容。');
      setResult(null);
      return;
    }
    setLoading(true);
    setError('');
    try {
      setResult(await searchNewsflashDuplicates(normalized));
    } catch (err) {
      setResult(null);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="duplicateSearchLayout">
      <section className="section duplicateInputSection">
        <div className="sectionHeader">
          <div>
            <h2>粘贴快讯内容</h2>
            <p className="duplicateHint">复用插件现有 AI 查重能力，检索近期已发布快讯和运行中的候选稿。</p>
          </div>
          <span>{text.length} 字</span>
        </div>
        <form onSubmit={handleSubmit}>
          <textarea
            className="duplicateInput"
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="把待查重的快讯标题或正文粘贴到这里……"
            aria-label="待查重快讯内容"
          />
          <div className="duplicateFormFooter">
            <span>结果仅作为编辑辅助，最终是否为重复快讯请结合事件进展人工判断。</span>
            <button className="primaryButton duplicateSubmit" type="submit" disabled={loading}>
              <Search size={17} /> {loading ? '查重中…' : '开始查重'}
            </button>
          </div>
        </form>
        {error && <div className="notice error duplicateNotice">{error}</div>}
      </section>

      <section className="section duplicateResultSection">
        <div className="sectionHeader">
          <div>
            <h2>疑似重复快讯</h2>
            <p className="duplicateHint">候选按语义相似度排序，最多展示 5 条。</p>
          </div>
          {result && <span>{result.top_candidates.length} 条候选</span>}
        </div>

        {loading ? (
          <div className="emptyState duplicateLoading">正在调用搜索者查重，请稍候…</div>
        ) : !result ? (
          <div className="emptyState duplicateLoading">粘贴内容并点击“开始查重”，这里会显示疑似重复的快讯。</div>
        ) : (
          <>
            <div className={result.is_duplicate ? 'duplicateConclusion isDuplicate' : 'duplicateConclusion isClear'}>
              {result.is_duplicate ? <AlertTriangle size={20} /> : <CheckCircle2 size={20} />}
              <div>
                <strong>{result.is_duplicate ? '疑似重复' : '未发现明显重复'}</strong>
                <span>{result.summary}</span>
              </div>
            </div>
            <div className="duplicateCandidateList">
              {result.top_candidates.length > 0 ? (
                result.top_candidates.map((candidate) => <SimilarCandidate key={`${candidate.target_type}:${candidate.target_id}`} candidate={candidate} />)
              ) : (
                <div className="emptyState">暂无可对照的历史快讯。</div>
              )}
            </div>
          </>
        )}
      </section>
    </div>
  );
}
