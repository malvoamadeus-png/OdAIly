import { type ReactNode, useEffect, useState } from 'react';
import { Activity, AlertTriangle, ChevronRight, ExternalLink, FileText, Radio, ShieldCheck } from 'lucide-react';
import {
  getAutoNewsflashDashboard,
  getAutoNewsflashEvent,
  getAutoNewsflashPrompts,
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
    duplicate: '全站重复',
    published: '已发布',
    failed: '失败',
    succeeded: '完成',
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
  }, []);

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
          {detail ? <EventDetail detail={detail} /> : <span className="muted">选择事件查看时间线</span>}
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

function EventDetail({ detail }: { detail: AutoNewsflashEventDetail }) {
  const { event } = detail;
  return (
    <article className="autoNewsflashDetailCopy">
      <div className="autoNewsflashDetailMeta"><span className={`autoNewsflashStatus ${event.status}`}>{statusLabel(event.status)}</span><span>{trackingTypeLabel(event.trackingType)}</span><span>{time(event.updatedAt)}</span></div>
      <h2>{event.title}</h2>
      <p>{event.rationale}</p>
      <DetailList title="官方账号">
        {detail.accounts.length === 0 ? <span className="muted">暂无已核验账号</span> : detail.accounts.map((account) => <a key={account.handle} href={`https://x.com/${account.handle}`} target="_blank" rel="noreferrer"><span>@{account.handle} · {account.officialEntity}</span><small>{statusLabel(account.status)} · {time(account.lastPolledAt)}</small></a>)}
      </DetailList>
      <DetailList title="官方进展">
        {detail.updates.length === 0 ? <span className="muted">尚未发现新官方帖子</span> : detail.updates.map((update) => <div className="autoNewsflashUpdate" key={update.id}><div><span className={`autoNewsflashStatus ${update.classification || update.status}`}>{statusLabel(update.classification || update.status)}</span><small>@{update.handle} · {time(update.classifiedAt)}</small></div>{update.factSummary && <strong>{update.factSummary}</strong>}{update.difference && <p>{update.difference}</p>}{update.post.url && <a href={update.post.url} target="_blank" rel="noreferrer" title="打开官方原帖"><ExternalLink size={15} /></a>}</div>)}
      </DetailList>
      <DetailList title="快讯投递">
        {detail.outbox.length === 0 ? <span className="muted">暂无可投递实质进展</span> : detail.outbox.map((item) => <div className="autoNewsflashOutbox" key={item.id}><span className={`autoNewsflashStatus ${item.status}`}>{statusLabel(item.status)}</span><small>{item.taskId ? `任务 ${item.taskId}` : '-'} · 尝试 {item.attempts}</small></div>)}
      </DetailList>
      <DetailList title="Web Search 核验">
        {detail.discoveries.length === 0 ? <span className="muted">暂无核验记录</span> : detail.discoveries.map((discovery) => <div className="autoNewsflashDiscovery" key={discovery.id}><span className={`autoNewsflashStatus ${discovery.status}`}>{statusLabel(discovery.status)}</span><small>{discovery.citations.length} 个引用 · {time(discovery.completedAt)}</small></div>)}
      </DetailList>
    </article>
  );
}

function DetailList({ title, children }: { title: string; children: ReactNode }) {
  return <section className="autoNewsflashDetailSection"><h3>{title}</h3><div>{children}</div></section>;
}
