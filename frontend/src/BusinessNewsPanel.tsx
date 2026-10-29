import { FormEvent, useEffect, useState } from 'react';
import { ChevronLeft, ChevronRight, RefreshCcw, Save, Search } from 'lucide-react';
import {
  getBusinessNewsDashboard,
  saveBusinessNewsGroup,
  type BusinessNewsDashboard,
  type BusinessNewsGroup,
  type BusinessNewsItem,
} from './xCaptureStore';

type Section = 'messages' | 'groups';

const classificationLabels: Record<string, string> = {
  newsflash: '快讯',
  activity: '活动',
  long_content: '长内容',
  other: '其他',
  uncertain: '不确定',
};

const statusLabels: Record<string, string> = {
  pending: '待处理',
  processing: 'AI 处理中',
  filtered_short: '字数过滤',
  activity_pending_api: '活动待接 API',
  skipped_long_content: '长内容跳过',
  skipped_other: '其他跳过',
  skipped_uncertain: '不确定跳过',
  formatting: '格式整理',
  publishing: '挂后台中',
  backgrounded: '已挂后台',
  discarded_failure: '失败丢弃',
};

const emptyDashboard: BusinessNewsDashboard = {
  items: [],
  groups: [],
  total: 0,
  page: 0,
  page_size: 50,
};

function labelForStatus(value: string): string {
  return statusLabels[value] || value;
}

function isEnabled(group: BusinessNewsGroup): boolean {
  return group.is_enabled === true || group.is_enabled === 1;
}

export function BusinessNewsPanel({ refreshToken = 0 }: { refreshToken?: number }) {
  const [section, setSection] = useState<Section>('messages');
  const [dashboard, setDashboard] = useState<BusinessNewsDashboard>(emptyDashboard);
  const [page, setPage] = useState(0);
  const [filters, setFilters] = useState({ chat_id: '', classification: '', status: '', query: '', date_from: '', date_to: '' });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [savingGroup, setSavingGroup] = useState<string | null>(null);
  const [groupDrafts, setGroupDrafts] = useState<Record<string, string>>({});

  async function load(nextPage = page, nextFilters = filters) {
    setLoading(true);
    setError('');
    try {
      const result = await getBusinessNewsDashboard({ ...nextFilters, page: nextPage, page_size: 50 });
      setDashboard(result);
      setGroupDrafts((current) => {
        const next = { ...current };
        for (const group of result.groups) next[group.chat_id] ??= group.customer_name;
        return next;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load(0);
  }, [refreshToken]);

  async function submitFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPage(0);
    await load(0);
  }

  async function saveGroup(group: BusinessNewsGroup, enabled = isEnabled(group)) {
    setSavingGroup(group.chat_id);
    setError('');
    try {
      const saved = await saveBusinessNewsGroup({
        chat_id: group.chat_id,
        customer_name: groupDrafts[group.chat_id] ?? group.customer_name,
        enabled,
      });
      setDashboard((current) => ({
        ...current,
        groups: current.groups.map((item) => item.chat_id === saved.chat_id ? saved : item),
      }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingGroup(null);
    }
  }

  const pages = Math.max(1, Math.ceil(dashboard.total / dashboard.page_size));

  return (
    <section className="businessNewsLayout">
      <div className="businessNewsTabs" role="tablist" aria-label="商务快讯视图">
        <button type="button" role="tab" aria-selected={section === 'messages'} className={section === 'messages' ? 'active' : ''} onClick={() => setSection('messages')}>消息记录</button>
        <button type="button" role="tab" aria-selected={section === 'groups'} className={section === 'groups' ? 'active' : ''} onClick={() => setSection('groups')}>客户群</button>
        <button type="button" className="businessNewsRefresh" title="刷新" aria-label="刷新" onClick={() => void load()} disabled={loading}>
          <RefreshCcw size={16} />
        </button>
      </div>
      {error && <div className="notice error">{error}</div>}
      {section === 'messages' ? (
        <>
          <form className="newsflashFilters businessNewsFilters" onSubmit={(event) => void submitFilters(event)}>
            <label><span>客户群</span><select value={filters.chat_id} onChange={(event) => setFilters({ ...filters, chat_id: event.target.value })}>
              <option value="">全部</option>{dashboard.groups.map((group) => <option key={group.chat_id} value={group.chat_id}>{group.customer_name || group.chat_title || group.chat_id}</option>)}
            </select></label>
            <label><span>分类</span><select value={filters.classification} onChange={(event) => setFilters({ ...filters, classification: event.target.value })}>
              <option value="">全部</option>{Object.entries(classificationLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select></label>
            <label><span>状态</span><select value={filters.status} onChange={(event) => setFilters({ ...filters, status: event.target.value })}>
              <option value="">全部</option>{Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select></label>
            <label><span>开始日期</span><input type="date" value={filters.date_from} onChange={(event) => setFilters({ ...filters, date_from: event.target.value })} /></label>
            <label><span>结束日期</span><input type="date" value={filters.date_to} onChange={(event) => setFilters({ ...filters, date_to: event.target.value })} /></label>
            <label className="wideFilter"><span>关键词</span><div className="inputWithIcon"><Search size={15} /><input value={filters.query} onChange={(event) => setFilters({ ...filters, query: event.target.value })} /></div></label>
            <button className="primaryButton filterButton" type="submit" disabled={loading}>筛选</button>
          </form>
          <div className="tableMeta"><span>{loading ? '加载中…' : `${dashboard.total} 条消息`}</span><span>第 {page + 1} / {pages} 页</span></div>
          <div className="newsflashTableWrap">
            <table className="newsflashTable businessNewsTable">
              <thead><tr><th>接收时间</th><th>客户 / 群</th><th>发送者</th><th>AI 分类</th><th>属性</th><th>状态</th><th>原文链接</th><th>详情</th></tr></thead>
              <tbody>
                {dashboard.items.map((item) => <BusinessNewsRow key={item.id} item={item} />)}
                {!loading && dashboard.items.length === 0 && <tr><td colSpan={8} className="emptyCell">没有消息记录</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="businessNewsPagination">
            <button className="iconButton" type="button" aria-label="上一页" title="上一页" disabled={page <= 0 || loading} onClick={() => { const next = Math.max(0, page - 1); setPage(next); void load(next); }}><ChevronLeft size={17} /></button>
            <span>{page + 1} / {pages}</span>
            <button className="iconButton" type="button" aria-label="下一页" title="下一页" disabled={loading || page + 1 >= pages} onClick={() => { const next = page + 1; setPage(next); void load(next); }}><ChevronRight size={17} /></button>
          </div>
        </>
      ) : (
        <div className="newsflashTableWrap businessNewsGroupsWrap">
          <table className="newsflashTable businessNewsGroupsTable">
            <thead><tr><th>Telegram 群</th><th>chat_id</th><th>客户名</th><th>发现时间</th><th>最近消息</th><th>接收</th><th>保存</th></tr></thead>
            <tbody>
              {dashboard.groups.map((group) => <BusinessGroupRow
                key={group.chat_id}
                group={group}
                customerName={groupDrafts[group.chat_id] ?? group.customer_name}
                saving={savingGroup === group.chat_id}
                onCustomerName={(value) => setGroupDrafts({ ...groupDrafts, [group.chat_id]: value })}
                onToggle={(enabled) => void saveGroup(group, enabled)}
                onSave={() => void saveGroup(group)}
              />)}
              {!loading && dashboard.groups.length === 0 && <tr><td colSpan={7} className="emptyCell">暂无发现的群</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function BusinessNewsRow({ item }: { item: BusinessNewsItem }) {
  return (
    <tr>
      <td>{fmtTime(item.received_at)}</td>
      <td>{item.customer_name || '未配置'}<small className="businessNewsSecondary">{item.chat_title || item.chat_id}</small></td>
      <td>{item.sender_name || item.sender_id || '—'}</td>
      <td>{item.classification ? classificationLabels[item.classification] || item.classification : '—'}<small className="businessNewsSecondary">{item.classification_reason || item.status_reason || ''}</small></td>
      <td>{item.writing_style === 'plain' ? '朴素' : item.writing_style === 'promotional' ? '宣传性' : '—'}</td>
      <td>{labelForStatus(item.status)}<small className="businessNewsSecondary">{item.status_reason || ''}</small></td>
      <td>{item.source_url ? <a href={item.source_url} target="_blank" rel="noreferrer">打开</a> : '—'}</td>
      <td><details className="businessNewsDetails"><summary>查看</summary><div>
        <strong>原文</strong><p>{item.raw_text || '原文已清理'}</p>
        {item.writer4_title && <><strong>Writer4 标题 / 正文</strong><p>{item.writer4_title}<br />{item.writer4_content}</p></>}
        {item.writer2_title && <><strong>Writer2 定稿</strong><p>{item.writer2_title}<br />{item.writer2_content}</p></>}
        {item.push_result && <><strong>Push API</strong><p>{item.push_result}</p></>}
        {item.plugin_notice_result && <><strong>插件提示</strong><p>{item.plugin_notice_result}</p></>}
      </div></details></td>
    </tr>
  );
}

function BusinessGroupRow({
  group,
  customerName,
  saving,
  onCustomerName,
  onToggle,
  onSave,
}: {
  group: BusinessNewsGroup;
  customerName: string;
  saving: boolean;
  onCustomerName: (value: string) => void;
  onToggle: (enabled: boolean) => void;
  onSave: () => void;
}) {
  return <tr>
    <td>{group.chat_title || group.chat_username || 'Telegram 群'}</td>
    <td className="monoCell">{group.chat_id}</td>
    <td><input className="businessNewsCustomerInput" value={customerName} maxLength={80} onChange={(event) => onCustomerName(event.target.value)} /></td>
    <td>{fmtTime(group.discovered_at)}</td>
    <td>{fmtTime(group.last_message_at)}</td>
    <td><label className="inlineToggle compact"><input type="checkbox" checked={isEnabled(group)} disabled={saving || !customerName.trim()} onChange={(event) => onToggle(event.target.checked)} /><span>{isEnabled(group) ? '启用' : '停用'}</span></label></td>
    <td><button className="iconButton" type="button" title="保存客户群配置" aria-label="保存客户群配置" disabled={saving || !customerName.trim()} onClick={onSave}><Save size={16} /></button></td>
  </tr>;
}

function fmtTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(date);
}
