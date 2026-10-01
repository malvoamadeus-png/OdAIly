import { type ReactNode, useEffect, useState } from 'react';
import { Activity, AlertTriangle, ChevronRight, ExternalLink, FileText, Radio, ShieldCheck, Trash2 } from 'lucide-react';
import {
  getAutoNewsflashDashboard,
  getAutoNewsflashEvent,
  getAutoNewsflashPrompts,
  dismissAutoNewsflashEvent,
  type AutoNewsflashDashboard,
  type AutoNewsflashEventCard,
  type AutoNewsflashEventDetail,
  type AutoNewsflashPrompt,
} from './xCaptureStore';

function time(value: string | null | undefined): string {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

function statusLabel(value: string): string {
  const labels: Record<string, string> = {
    active: '追踪中',
    discovering: '发现官方账号',
    ended: '已结束',
    discovery_failed: '官方账号未核验',
    capacity_exhausted: '追踪容量已满',
    material_progress: '实质进展',
    relevant_no_progress: '相关但无进展',
    irrelevant: '无关',
    submitted: '已投递',
    pending: '待投递',
    cancelled: '已取消',
    duplicate: '全站重复',
    published: '已发布',
    failed: '失败',
    succeeded: '完成',
    judging: '生成中',
    deduping: '查重中',
    writing: '生成中',
    written: '已生成草稿',
    formatting: '格式化中',
    publisher_pending: '等待发布',
    publishing: '发布中',
    auto_published: '已发布',
    ready_review: '待人工复核',
    publisher_failed: '失败',
    event_tracking_cancelled: '已取消',
  };
  return labels[value] || value;
}

function trackingTypeLabel(value: string): string {
  const labels: Record<string, string> = {
    security_asset_incident: '安全与资产事件',
    official_dispute_or_denial: '官方争议或否认',
    exceptional_project_decision: '重大项目决定',
  };
  return labels[value] || value;
}

export function AutoNewsflashPanel() {
  const [dashboard, setDashboard] = useState<AutoNewsflashDashboard | null>(null);
  const [detail, setDetail] = useState<AutoNewsflashEventDetail | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [prompts, setPrompts] = useState<AutoNewsflashPrompt[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);
  const [dismissing, setDismissing] = useState(false);

  useEffect(() => {
    let active = true;
    void Promise.all([getAutoNewsflashDashboard(), getAutoNewsflashPrompts()])
      .then(([nextDashboard, nextPrompts]) => {
        if (!active) return;
        setDashboard(nextDashboard);
        setPrompts(nextPrompts);
        setSelected((current) => current && nextDashboard.events.some((event) => event.id === current) ? current : nextDashboard.events[0]?.id || null);
      })
      .catch((cause) => active && setError(cause instanceof Error ? cause.message : '无法读取热点自动快讯'));
    return () => { active = false; };
  }, [refreshToken]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let active = true;
    void getAutoNewsflashEvent(selected)
      .then((nextDetail) => active && setDetail(nextDetail))
      .catch((cause) => active && setError(cause instanceof Error ? cause.message : '无法读取事件详情'));
    return () => { active = false; };
  }, [selected]);

  async function dismissSelectedEvent() {
    if (!selected || dismissing) return;
    const current = dashboard?.events.find((event) => event.id === selected);
    if (!current || !window.confirm(`确认删除热点自动快讯“${current.title}”？\n\n这会停止后续追踪并取消尚未发布的任务，已发布内容不会删除。`)) return;
    setDismissing(true);
    setError(null);
    try {
      await dismissAutoNewsflashEvent(selected);
      setSelected(null);
      setDetail(null);
      setRefreshToken((value) => value + 1);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '删除热点自动快讯失败');
    } finally {
      setDismissing(false);
    }
  }

  return (
    <section className="autoNewsflashLayout">
      {error && <div className="notice error"><AlertTriangle size={17} /> {error}</div>}
      <div className="autoNewsflashSummary">
        <Metric icon={<Radio size={16} />} label="追踪中" value={dashboard?.summary.eventsByStatus.active || 0} />
        <Metric icon={<ShieldCheck size={16} />} label="官方账号" value={`${dashboard?.summary.activeAccounts || 0}/${dashboard?.summary.maxActiveAccounts || 15}`} />
        <Metric icon={<Activity size={16} />} label="待判帖子" value={dashboard?.summary.pendingUpdates || 0} />
        <Metric icon={<FileText size={16} />} label="待投递" value={dashboard?.summary.pendingOutbox || 0} />
      </div>
      <div className="autoNewsflashWorkspace">
        <section className="autoNewsflashEvents">
          <div className="sectionHeader"><div><h2>事件</h2><span>{dashboard?.enabled === false ? '任务未启用' : '官方账号短期追踪'}</span></div></div>
          <div className="autoNewsflashEventList">
            {!dashboard && <span className="muted">加载中</span>}
            {dashboard?.events.length === 0 && <span className="muted">暂无自动追踪事件</span>}
            {dashboard?.events.map((event) => <EventRow key={event.id} event={event} active={selected === event.id} onSelect={() => setSelected(event.id)} />)}
          </div>
        </section>
        <section className="autoNewsflashDetail">
          {detail ? <EventDetail detail={detail} dismissing={dismissing} onDismiss={dismissSelectedEvent} /> : <span className="muted">选择事件查看时间线</span>}
        </section>
      </div>
      <section className="autoNewsflashPrompts">
        <div className="sectionHeader"><div><h2>当前 Prompt</h2><span>只读版本记录</span></div></div>
        {prompts.map((prompt) => (
          <details key={prompt.id} className="autoNewsflashPrompt">
            <summary><span>{prompt.key} v{prompt.version}</span><small>{prompt.callCount.toLocaleString()} 次调用 · {time(prompt.createdAt)}</small></summary>
            {prompt.content ? <pre>{prompt.content}</pre> : <span className="muted">未加载完整文本</span>}
          </details>
        ))}
      </section>
    </section>
  );
}

function Metric({ icon, label, value }: { icon: ReactNode; label: string; value: number | string }) {
  return <div className="autoNewsflashMetric"><span>{icon}{label}</span><strong>{typeof value === 'number' ? value.toLocaleString() : value}</strong></div>;
}

function EventRow({ event, active, onSelect }: { event: AutoNewsflashEventCard; active: boolean; onSelect: () => void }) {
  return (
    <button className={active ? 'autoNewsflashEventRow active' : 'autoNewsflashEventRow'} type="button" onClick={onSelect}>
      <div><strong>{event.title}</strong><span>{trackingTypeLabel(event.trackingType)}</span></div>
      <div className="autoNewsflashRowStatus"><span className={`autoNewsflashStatus ${event.status}`}>{statusLabel(event.status)}</span><ChevronRight size={17} /></div>
    </button>
  );
}

function EventDetail({ detail, dismissing, onDismiss }: { detail: AutoNewsflashEventDetail; dismissing: boolean; onDismiss: () => void }) {
  const { event } = detail;
  return (
    <article className="autoNewsflashDetailCopy">
      <div className="autoNewsflashDetailHeader">
        <div className="autoNewsflashDetailMeta"><span className={`autoNewsflashStatus ${event.status}`}>{statusLabel(event.status)}</span><span>{trackingTypeLabel(event.trackingType)}</span><span>更新于 {time(event.updatedAt)}</span></div>
        <button className="danger ghostButton" type="button" onClick={onDismiss} disabled={dismissing} title="删除这条热点自动快讯并停止追踪">
          <Trash2 size={15} /> {dismissing ? '删除中' : '删除自动快讯'}
        </button>
      </div>
      <h2>{event.title}</h2>
      <p>{event.rationale}</p>
      <DetailList title="关联热点" description="事件从这些热点话题中发现；事件账号帖子不会回灌到热点话题列表。">
        {detail.topics.length === 0 ? <span className="muted">暂无关联热点</span> : detail.topics.map((topic) => <article className="autoNewsflashTopicLink" key={topic.topicId}><strong>{topic.snapshot.title || topic.topicId}</strong>{topic.snapshot.brief ? <p>{topic.snapshot.brief}</p> : <p className="muted">正文生成中</p>}<small>{topic.topicId} · {time(topic.updatedAt)}</small></article>)}
      </DetailList>
      <DetailList title="官方账号">
        {detail.accounts.length === 0 ? <span className="muted">暂无已核验账号</span> : detail.accounts.map((account) => <a key={account.handle} href={`https://x.com/${account.handle}`} target="_blank" rel="noreferrer"><span>@{account.handle} · {account.officialEntity}</span><small>{statusLabel(account.status)} · {time(account.lastPolledAt)}</small></a>)}
      </DetailList>
      <DetailList title="官方进展">
        {detail.updates.length === 0 ? <span className="muted">尚未发现新官方帖子</span> : detail.updates.map((update) => <div className="autoNewsflashUpdate" key={update.id}><div><span className={`autoNewsflashStatus ${update.classification || update.status}`}>{statusLabel(update.classification || update.status)}</span><small>@{update.handle} · {time(update.classifiedAt)}</small></div>{update.factSummary && <strong>{update.factSummary}</strong>}{update.difference && <p>{update.difference}</p>}{update.post.url && <a href={update.post.url} target="_blank" rel="noreferrer" title="打开官方原帖"><ExternalLink size={15} /></a>}</div>)}
      </DetailList>
      <DetailList title="快讯投递">
        {detail.outbox.length === 0 ? <span className="muted">暂无可投递实质进展</span> : detail.outbox.map((item) => (
          <article className="autoNewsflashOutbox" key={item.id}>
            <div className="autoNewsflashOutboxHeader">
              <span className={`autoNewsflashStatus ${item.status}`}>{statusLabel(item.status)}</span>
              {item.taskStatus && <span className={`autoNewsflashStatus ${item.taskStatus}`}>{statusLabel(item.taskStatus)}</span>}
              <small>{item.taskId ? `任务 ${item.taskId}` : '任务尚未创建'} · 尝试 {item.attempts}</small>
            </div>
            {item.title && <strong className="autoNewsflashGeneratedTitle">{item.title}</strong>}
            {item.content && <p className="autoNewsflashGeneratedContent">{item.content}</p>}
            <div className="autoNewsflashOutboxMeta">
              {item.contentStage && <span>{item.contentStage === 'final' ? '最终稿' : '草稿'}</span>}
              {item.publisherDecision && <span>发布决定：{item.publisherDecision}</span>}
              {item.publisherReasonCode && <span>{item.publisherReasonCode}</span>}
              {item.submittedAt && <span>投递时间：{time(item.submittedAt)}</span>}
              {item.publishedAt && <span>发布时间：{time(item.publishedAt)}</span>}
              {!item.submittedAt && item.updatedAt && <span>更新时间：{time(item.updatedAt)}</span>}
              {item.sourceUrl && <a href={item.sourceUrl} target="_blank" rel="noreferrer" title="打开官方原帖"><ExternalLink size={14} /> 官方原帖</a>}
            </div>
            {item.error && <p className="autoNewsflashOutboxError">{item.error}</p>}
          </article>
        ))}
      </DetailList>
      <DetailList title="官方账号发现与身份核验" description="Web Search 只用于寻找事件主体的官方账号并保存身份依据；后续帖子由事件专用账号轮询获取。">
        {detail.discoveries.length === 0 ? <span className="muted">暂无核验记录</span> : detail.discoveries.map((discovery) => <div className="autoNewsflashDiscovery" key={discovery.id}><span className={`autoNewsflashStatus ${discovery.status}`}>{statusLabel(discovery.status)}</span><small>{discovery.citations.length} 个引用 · {time(discovery.completedAt)}</small></div>)}
      </DetailList>
    </article>
  );
}

function DetailList({ title, description, children }: { title: string; description?: string; children: ReactNode }) {
  return <section className="autoNewsflashDetailSection"><h3>{title}</h3>{description && <p className="muted">{description}</p>}<div>{children}</div></section>;
}
