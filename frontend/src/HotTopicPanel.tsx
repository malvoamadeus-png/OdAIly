import { FormEvent, useEffect, useMemo, useState } from 'react';
import { Ban, Check, ChevronRight, CircleAlert, EyeOff, Flame, Plus, RefreshCcw, Search, Users } from 'lucide-react';
import {
  getHotTopicDashboard,
  getHotTopicDetail,
  listHotTopicAccounts,
  mutateHotTopicAccount,
  type HotTopicAccount,
  type HotTopicAccountStatus,
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

const statusCopy: Record<HotTopicAccountStatus, string> = {
  followed: '跟踪中', unfollowed: '未跟踪', blacklisted: '已拉黑',
};

export function HotTopicPanel() {
  const [dashboard, setDashboard] = useState<HotTopicDashboard | null>(null);
  const [accounts, setAccounts] = useState<HotTopicAccount[]>([]);
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState<'all' | HotTopicAccountStatus>('all');
  const [selected, setSelected] = useState<HotTopicCard | null>(null);
  const [detail, setDetail] = useState<HotTopicDetail | null>(null);
  const [handle, setHandle] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const refresh = async () => {
    setLoading(true);
    try {
      const [nextDashboard, nextAccounts] = await Promise.all([getHotTopicDashboard(), listHotTopicAccounts(query, status)]);
      setDashboard(nextDashboard);
      setAccounts(nextAccounts);
      setError('');
      if (!selected && nextDashboard.topics[0]) setSelected(nextDashboard.topics[0]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '热点话题数据暂不可用');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void refresh(); }, [query, status]); // Refresh only when a deliberate account filter changes.

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

  async function mutate(action: 'add' | 'follow' | 'unfollow' | 'blacklist' | 'unblacklist', screenName: string, name = '') {
    setSaving(true);
    try {
      await mutateHotTopicAccount(action, screenName, name);
      if (action === 'add') {
        setHandle('');
        setDisplayName('');
      }
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '账号操作失败');
    } finally {
      setSaving(false);
    }
  }

  function addAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (handle.trim()) void mutate('add', handle, displayName);
  }

  return (
    <section className="hottopicLayout">
      <div className="hottopicSummary">
        <Metric label="跟踪账号" value={health?.followed ?? 0} />
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

      <section className="hottopicAccounts">
        <div className="sectionHeader"><div><h2>关注账号</h2><span>内容数和热点参与数均从本次部署起累计</span></div><span>{health?.total ?? 0} 个账号</span></div>
        <form className="hottopicAddAccount" onSubmit={addAccount}>
          <input value={handle} onChange={(event) => setHandle(event.target.value)} placeholder="@username 或 username" aria-label="X 用户名" />
          <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="显示名，可选" aria-label="显示名" />
          <button className="primaryButton" type="submit" disabled={saving || !handle.trim()}><Plus size={17} /> 添加并关注</button>
        </form>
        <div className="hottopicFilters">
          <label><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索账号" /></label>
          <div className="segmentedControl" aria-label="账号状态筛选">
            {(['all', 'followed', 'unfollowed', 'blacklisted'] as const).map((item) => <button key={item} type="button" className={status === item ? 'active' : ''} onClick={() => setStatus(item)}>{item === 'all' ? '全部' : statusCopy[item]}</button>)}
          </div>
        </div>
        <div className="hottopicAccountTable" role="table">
          <div className="hottopicAccountHead" role="row"><span>账号</span><span>状态</span><span>累计内容</span><span>参与热点</span><span>最近成功</span><span>操作</span></div>
          {!loading && accounts.length === 0 && <div className="emptyState">没有匹配的账号。</div>}
          {accounts.map((account) => <AccountRow key={account.screen_name} account={account} saving={saving} onMutate={mutate} />)}
        </div>
      </section>
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

function AccountRow({ account, saving, onMutate }: { account: HotTopicAccount; saving: boolean; onMutate: (action: 'follow' | 'unfollow' | 'blacklist' | 'unblacklist', screenName: string) => Promise<void> }) {
  const hasError = Boolean(account.last_error);
  return <div className="hottopicAccountRow" role="row">
    <div><strong>@{account.screen_name}</strong>{account.display_name && <span>{account.display_name}</span>}{hasError && <small title={account.last_error || ''}><CircleAlert size={14} /> {account.consecutive_failures} 次失败</small>}</div>
    <span className={`hottopicStatus ${account.status}`}>{statusCopy[account.status]}</span>
    <strong>{account.cumulative_content_count.toLocaleString()}</strong>
    <strong>{account.cumulative_hot_topic_count.toLocaleString()}</strong>
    <span>{formatTime(account.last_success_at)}</span>
    <div className="hottopicActions">
      {account.status === 'followed' && <button type="button" title="停止跟踪" aria-label={`停止跟踪 ${account.screen_name}`} onClick={() => void onMutate('unfollow', account.screen_name)} disabled={saving}><EyeOff size={16} /></button>}
      {account.status === 'unfollowed' && <button type="button" title="恢复跟踪" aria-label={`恢复跟踪 ${account.screen_name}`} onClick={() => void onMutate('follow', account.screen_name)} disabled={saving}><Check size={16} /></button>}
      {account.status === 'blacklisted' ? <button type="button" title="解除拉黑，账号仍不会自动恢复跟踪" aria-label={`解除拉黑 ${account.screen_name}`} onClick={() => void onMutate('unblacklist', account.screen_name)} disabled={saving}><Check size={16} /></button> : <button type="button" title="拉黑并停止跟踪" aria-label={`拉黑 ${account.screen_name}`} onClick={() => void onMutate('blacklist', account.screen_name)} disabled={saving}><Ban size={16} /></button>}
    </div>
  </div>;
}
