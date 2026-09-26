import { useEffect, useMemo, useState } from 'react';
import { ChevronRight, CircleAlert, Flame, RefreshCcw, Users } from 'lucide-react';
import {
  getHotTopicDashboard,
  getHotTopicDetail,
  type HotTopicCard,
  type HotTopicDashboard,
  type HotTopicDetail,
} from './xCaptureStore';

function formatTime(value: string | null | undefined) {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '-' : new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}

export function HotTopicPanel({ hotTopicAccountCount }: { hotTopicAccountCount?: number }) {
  const [dashboard, setDashboard] = useState<HotTopicDashboard | null>(null);
  const [selected, setSelected] = useState<HotTopicCard | null>(null);
  const [detail, setDetail] = useState<HotTopicDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const refresh = async () => {
    setLoading(true);
    try {
      const nextDashboard = await getHotTopicDashboard();
      setDashboard(nextDashboard);
      setError('');
      if (!selected && nextDashboard.topics[0]) setSelected(nextDashboard.topics[0]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '热点话题数据暂不可用');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void refresh(); }, []);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let active = true;
    void getHotTopicDetail(selected.id).then((value) => active && setDetail(value)).catch((cause) => active && setError(cause instanceof Error ? cause.message : '无法读取话题详情'));
    return () => { active = false; };
  }, [selected]);

  const health = dashboard?.health.accounts;
  const visibleTopics = useMemo(() => dashboard?.topics ?? [], [dashboard]);

  return (
    <section className="hottopicLayout">
      <div className="hottopicSummary">
        <Metric label="订阅账号" value={hotTopicAccountCount ?? health?.followed ?? 0} />
        <Metric label="正在热议" value={visibleTopics.length} />
        <Metric label="待处理内容" value={dashboard?.health.inboxPending ?? 0} />
        <Metric label="抓取异常" value={health?.errors ?? 0} warning={(health?.errors ?? 0) > 0} />
        <button className="iconButton" type="button" title="刷新热点话题" onClick={() => void refresh()} disabled={loading}><RefreshCcw size={18} /></button>
      </div>

      {error && <div className="notice error"><CircleAlert size={17} /> {error}</div>}

      <div className="hottopicWorkspace">
        <section className="hottopicTopics">
          <div className="sectionHeader"><div><h2>当前热点</h2><span>只显示已满足独立参与门槛的话题</span></div><span>{loading ? '加载中' : `${visibleTopics.length} 条`}</span></div>
          <div className="hottopicTopicList">
            {!loading && visibleTopics.length === 0 && <div className="emptyState">新部署会从服务启动后的内容开始形成热点。</div>}
            {visibleTopics.map((topic) => <TopicRow key={topic.id} topic={topic} active={selected?.id === topic.id} onSelect={setSelected} />)}
          </div>
        </section>
        <section className="hottopicDetail">
          {detail ? <TopicDetail detail={detail} /> : <div className="emptyState">选择一个热点查看参与账号。</div>}
        </section>
      </div>
    </section>
  );
}

function Metric({ label, value, warning = false }: { label: string; value: number; warning?: boolean }) {
  return <div className={warning ? 'hottopicMetric warning' : 'hottopicMetric'}><span>{label}</span><strong>{value.toLocaleString()}</strong></div>;
}

function TopicRow({ topic, active, onSelect }: { topic: HotTopicCard; active: boolean; onSelect: (topic: HotTopicCard) => void }) {
  return <button className={active ? 'hottopicTopicRow active' : 'hottopicTopicRow'} type="button" onClick={() => onSelect(topic)}>
    <div><strong>{topic.title}</strong><p>{topic.brief}</p><span><Users size={14} /> 1h {topic.participants.oneHour} · 6h {topic.participants.sixHours} · 24h {topic.participants.twentyFourHours}</span></div>
    <div className="hottopicScore"><span><Flame size={14} /> {topic.hotness.toFixed(1)}</span><ChevronRight size={18} /></div>
  </button>;
}

function TopicDetail({ detail }: { detail: HotTopicDetail }) {
  return <article className="hottopicDetailCopy"><div className="hottopicDetailMeta"><span><Flame size={15} /> 热度 {detail.hotness.toFixed(1)}</span><span><Users size={15} /> {detail.participants.twentyFourHours} 位参与者</span></div><h2>{detail.title}</h2><p>{detail.brief}</p><section><h3>参与账号</h3>{detail.speakers.length === 0 ? <span className="muted">暂无参与记录</span> : <div className="hottopicSpeakers">{detail.speakers.map((speaker) => <a key={speaker.account} href={speaker.sourceUrl || `https://x.com/${speaker.account}`} target="_blank" rel="noreferrer"><span>@{speaker.account}</span><time>{formatTime(speaker.lastParticipationAt)}</time></a>)}</div>}</section></article>;
}
