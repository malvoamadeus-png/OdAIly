import { useEffect, useMemo, useState } from 'react';
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Database,
  ListFilter,
  Save,
  Search,
  Settings2,
  X,
} from 'lucide-react';
import {
  newsflashOperations,
  type ContributionsPayload,
  type EventPage,
  type NewsflashPage,
  type NewsflashRow,
  type Person,
  type QualityItem,
  type QualityPayload,
  type SchedulePayload,
  type SummaryPayload,
} from './newsflashOperations';

type TabKey = 'overview' | 'schedule' | 'summary' | 'quality' | 'contributions' | 'events';

const tabs: Array<{ key: TabKey; label: string }> = [
  { key: 'overview', label: '快讯总览' },
  { key: 'schedule', label: '排班表' },
  { key: 'summary', label: '数据总览' },
  { key: 'quality', label: '优质快讯' },
  { key: 'contributions', label: '贡献快讯' },
  { key: 'events', label: '事件与首发' },
];

const contributionLabels: Record<string, string> = { regular: '常规', night: '夜间', ppp: 'PPP' };
const publisherLabels: Record<string, string> = {
  human: '人类',
  human_unmapped: '未映射人员',
  odaily_ai: 'OdAIly',
  other_ai: '其他 AI',
  pending_ai: '识别中',
};
const shiftLabels: Record<string, string> = { morning: '早班', middle: '中班', late: '晚班' };
const qualityReasonLabels: Record<string, string> = {
  contribution: '贡献快讯',
  competitor_first: '晚于竞品',
  regular_source: '常规信源',
  automated_coverage: '自动覆盖',
  jin10_content: '正文含金十',
};

function personColor(personKey: string) {
  let hash = 2166136261;
  for (const character of personKey) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  const hue = Math.abs(hash) % 360;
  return {
    backgroundColor: `hsl(${hue} 58% 93%)`,
    borderColor: `hsl(${hue} 44% 62%)`,
    color: `hsl(${hue} 42% 27%)`,
  };
}

function localDate(value: string | null): string {
  if (!value) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value));
}

function currentMonth(): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit' }).format(new Date()).slice(0, 7);
}

function mondayKey(value = new Date()): string {
  const shanghai = new Date(value.toLocaleString('en-US', { timeZone: 'Asia/Shanghai' }));
  const day = shanghai.getDay() || 7;
  shanghai.setDate(shanghai.getDate() - day + 1);
  return `${shanghai.getFullYear()}-${String(shanghai.getMonth() + 1).padStart(2, '0')}-${String(shanghai.getDate()).padStart(2, '0')}`;
}

function dateKey(value: string, days = 0): string {
  const [year, month, day] = value.split('-').map(Number);
  const date = new Date(year, month - 1, day, 12);
  date.setDate(date.getDate() + days);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function formatMonthLabel(value: string): string {
  const [year, month] = value.split('-').map(Number);
  return `${year}年${month}月`;
}

function formatWeekLabel(weekStart: string, weekEnd = dateKey(weekStart, 6)): string {
  const [year, month, day] = weekStart.split('-').map(Number);
  const firstDay = new Date(year, month - 1, 1, 12);
  const mondayOffset = (firstDay.getDay() + 6) % 7;
  const weekOfMonth = Math.floor((mondayOffset + day - 1) / 7) + 1;
  const [, endMonth, endDay] = weekEnd.split('-').map(Number);
  return `${year}年${month}月第${weekOfMonth}周（${month}月${day}日 - ${endMonth}月${endDay}日）`;
}

function coverage(value: { known: number; total: number } | undefined): string {
  if (!value || value.total === 0 || value.known === value.total) return '';
  return `${value.known}/${value.total}`;
}

export default function NewsflashOperationsPanel({ refreshToken = 0 }: { refreshToken?: number }) {
  const [tab, setTab] = useState<TabKey>('overview');
  const [people, setPeople] = useState<Person[]>([]);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  useEffect(() => {
    newsflashOperations<{ people: Person[] }>('roster').then((result) => setPeople(result.people)).catch((reason) => setError(String(reason)));
  }, [refreshToken]);

  return (
    <div className="newsflashWorkspace">
      <div className="newsflashTabs" role="tablist" aria-label="快讯及数据总览子页">
        {tabs.map((item) => (
          <button key={item.key} type="button" className={tab === item.key ? 'active' : ''} onClick={() => setTab(item.key)}>
            {item.label}
          </button>
        ))}
      </div>
      {(error || message) && <div className={error ? 'notice error' : 'notice'}>{error || message}</div>}
      {tab === 'overview' && <Overview people={people} refreshToken={refreshToken} onError={setError} onMessage={setMessage} />}
      {tab === 'schedule' && <Schedule refreshToken={refreshToken} onPeople={setPeople} onError={setError} onMessage={setMessage} />}
      {tab === 'summary' && <Summary refreshToken={refreshToken} onError={setError} />}
      {tab === 'quality' && <Quality refreshToken={refreshToken} onError={setError} />}
      {tab === 'contributions' && <Contributions people={people} refreshToken={refreshToken} onError={setError} onMessage={setMessage} />}
      {tab === 'events' && <Events refreshToken={refreshToken} onError={setError} />}
    </div>
  );
}

function Overview({
  people,
  refreshToken,
  onError,
  onMessage,
}: {
  people: Person[];
  refreshToken: number;
  onError: (value: string) => void;
  onMessage: (value: string) => void;
}) {
  const [data, setData] = useState<NewsflashPage>({ items: [], page: 1, page_size: 50, total: 0, pages: 1 });
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [filters, setFilters] = useState({
    search: '', date_from: '', date_to: '', publisher_kind: '', publisher_person_key: '',
    is_pushed: '', contributor_person_key: '', contribution_type: '', first_status: '', first_source: '',
  });

  async function load(nextPage = page) {
    setLoading(true);
    onError('');
    try {
      const payload = Object.fromEntries(Object.entries(filters).filter(([, value]) => value !== ''));
      const result = await newsflashOperations<NewsflashPage>('list', { ...payload, page: nextPage });
      setData(result);
      setPage(result.page);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(1); }, [refreshToken]);

  async function patchRow(row: NewsflashRow, patch: Record<string, unknown>) {
    onError('');
    try {
      const updated = await newsflashOperations<NewsflashRow>('update', { source_item_id: row.source_item_id, patch });
      setData((current) => ({ ...current, items: current.items.map((item) => item.source_item_id === updated.source_item_id ? updated : item) }));
      onMessage(`快讯 ${row.source_item_id} 已保存`);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  const activePeople = people.filter((person) => person.active);
  const contributors = activePeople.filter((person) => person.contributor_enabled);

  return (
    <section className="newsflashSection">
      <form className="newsflashFilters" onSubmit={(event) => { event.preventDefault(); void load(1); }}>
        <label className="wideFilter"><span>标题或快讯 ID</span><div className="inputWithIcon"><Search size={15} /><input value={filters.search} onChange={(event) => setFilters({ ...filters, search: event.target.value })} /></div></label>
        <label><span>开始日期</span><input type="date" value={filters.date_from} onChange={(event) => setFilters({ ...filters, date_from: event.target.value })} /></label>
        <label><span>结束日期</span><input type="date" value={filters.date_to} onChange={(event) => setFilters({ ...filters, date_to: event.target.value })} /></label>
        <label><span>发布类型</span><select value={filters.publisher_kind} onChange={(event) => setFilters({ ...filters, publisher_kind: event.target.value })}><option value="">全部</option><option value="human">人类</option><option value="human_unmapped">未映射</option><option value="odaily_ai">OdAIly</option><option value="other_ai">其他 AI</option></select></label>
        <label><span>操作人</span><select value={filters.publisher_person_key} onChange={(event) => setFilters({ ...filters, publisher_person_key: event.target.value })}><option value="">全部</option>{activePeople.map((person) => <option key={person.person_key} value={person.person_key}>{person.display_name}</option>)}</select></label>
        <label><span>推送</span><select value={filters.is_pushed} onChange={(event) => setFilters({ ...filters, is_pushed: event.target.value })}><option value="">全部</option><option value="1">是</option><option value="0">否</option></select></label>
        <label><span>贡献者</span><select value={filters.contributor_person_key} onChange={(event) => setFilters({ ...filters, contributor_person_key: event.target.value })}><option value="">全部</option>{contributors.map((person) => <option key={person.person_key} value={person.person_key}>{person.display_name}</option>)}</select></label>
        <label><span>贡献类型</span><select value={filters.contribution_type} onChange={(event) => setFilters({ ...filters, contribution_type: event.target.value })}><option value="">全部</option><option value="regular">常规</option><option value="night">夜间</option><option value="ppp">PPP</option></select></label>
        <label><span>首发状态</span><select value={filters.first_status} onChange={(event) => setFilters({ ...filters, first_status: event.target.value })}><option value="">全部</option><option value="ready">已计算</option><option value="unmatched">未匹配事件</option><option value="insufficient">数据不足</option><option value="excluded">已排除</option></select></label>
        <label><span>首发单位</span><select value={filters.first_source} onChange={(event) => setFilters({ ...filters, first_source: event.target.value })}><option value="">全部</option><option value="odaily">Odaily</option><option value="blockbeats">BlockBeats</option><option value="panews">PANews</option><option value="jinse">金色财经</option></select></label>
        <button className="primaryButton filterButton" type="submit"><ListFilter size={16} /> 筛选</button>
      </form>
      <div className="tableMeta"><span>{loading ? '加载中…' : `${data.total} 条快讯`}</span><span>每页 50 条</span></div>
      <div className="newsflashTableWrap">
        <table className="newsflashTable">
          <thead><tr><th>ID</th><th className="titleColumn">快讯标题</th><th>发布时间</th><th>推送</th><th>操作人</th><th>浏览量</th><th>贡献者</th><th>类型</th><th>首发单位</th></tr></thead>
          <tbody>
            {data.items.map((row) => (
              <tr key={row.source_item_id}>
                <td className="monoCell">{row.source_item_id}</td>
                <td className="titleColumn">{row.source_url ? <a href={row.source_url} target="_blank" rel="noreferrer">{row.title || '无标题'}</a> : row.title || '无标题'}</td>
                <td>{localDate(row.published_at)}</td>
                <td>{row.is_pushed == null ? <Unavailable /> : row.is_pushed ? '是' : '否'}</td>
                <td>
                  <select className="inlineSelect" value={row.publisher_kind === 'human' && row.publisher_person_key ? `human:${row.publisher_person_key}` : row.publisher_kind || ''} onChange={(event) => {
                    const value = event.target.value;
                    if (value.startsWith('human:')) void patchRow(row, { publisher_kind: 'human', publisher_person_key: value.slice(6) });
                    else if (value) void patchRow(row, { publisher_kind: value });
                  }}>
                    <option value="">{row.operator_raw || '—'}</option>
                    {activePeople.filter((person) => person.duty_enabled).map((person) => <option key={person.person_key} value={`human:${person.person_key}`}>{person.display_name}</option>)}
                    <option value="odaily_ai">OdAIly</option><option value="other_ai">其他 AI</option>
                  </select>
                  {row.publisher_kind && row.publisher_kind !== 'human' && <small>{publisherLabels[row.publisher_kind] || row.publisher_kind}</small>}
                </td>
                <td className="numberCell">{row.view_count == null ? <Unavailable /> : row.view_count.toLocaleString()}</td>
                <td><select className="inlineSelect" value={row.contributor_person_key || ''} onChange={(event) => void patchRow(row, event.target.value ? { contributor_person_key: event.target.value, is_contribution: true } : { is_contribution: false })} disabled={['odaily_ai', 'other_ai', 'pending_ai'].includes(row.publisher_kind || '')}><option value="">—</option>{contributors.map((person) => <option key={person.person_key} value={person.person_key}>{person.display_name}</option>)}</select></td>
                <td><select className="inlineSelect" value={row.contribution_type || 'regular'} onChange={(event) => void patchRow(row, { contribution_type: event.target.value })} disabled={!row.is_contribution}><option value="regular">常规</option><option value="night">夜间</option><option value="ppp">PPP</option></select></td>
                <td><span className={`statusText ${row.first_publication.status}`}>{row.first_publication.label}</span></td>
              </tr>
            ))}
            {!loading && data.items.length === 0 && <tr><td colSpan={9} className="emptyCell">没有符合条件的快讯</td></tr>}
          </tbody>
        </table>
      </div>
      <Pagination page={page} pages={data.pages} onChange={(next) => void load(next)} />
    </section>
  );
}

function Schedule({ refreshToken, onPeople, onError, onMessage }: { refreshToken: number; onPeople: (people: Person[]) => void; onError: (value: string) => void; onMessage: (value: string) => void }) {
  const [month, setMonth] = useState(currentMonth());
  const [data, setData] = useState<SchedulePayload | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  async function load() {
    onError('');
    try {
      const result = await newsflashOperations<SchedulePayload>('schedule', { month });
      setData(result); onPeople(result.people);
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  }
  useEffect(() => { void load(); }, [month, refreshToken]);

  async function saveMode(day: string, mode: 'two' | 'three') {
    if (data?.assignments.some((item) => item.duty_date === day) && !window.confirm('切换班制会清空当天已有排班，是否继续？')) return;
    try { const result = await newsflashOperations<SchedulePayload>('save_day_mode', { date: day, mode }); setData(result); onMessage(`${day} 班制已更新`); }
    catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  }
  async function saveAssignment(day: string, shiftKey: string, personKey: string) {
    if (personKey && data?.assignments.some((item) => item.duty_date === day && item.shift_key !== shiftKey && item.person_key === personKey)) {
      if (!window.confirm('该人员当天已经排了其他班次，是否仍然保存？')) return;
    }
    try { const result = await newsflashOperations<SchedulePayload>('save_assignment', { date: day, shift_key: shiftKey, person_key: personKey || null }); setData(result); }
    catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  }

  const assignmentMap = useMemo(() => new Map((data?.assignments || []).map((item) => [`${item.duty_date}:${item.shift_key}`, item.person_key || ''])), [data]);
  const days = data?.days || [];
  const leading = days.length ? (new Date(`${days[0].date}T12:00:00+08:00`).getDay() + 6) % 7 : 0;
  const dutyPeople = (data?.people || []).filter((person) => person.active && person.duty_enabled);

  return (
    <section className="newsflashSection">
      <div className="sectionCommandBar"><label><span>月份</span><input type="month" value={month} onChange={(event) => setMonth(event.target.value)} /></label><button className="iconTextButton" type="button" onClick={() => setSettingsOpen(true)}><Settings2 size={17} /> 人员设置</button></div>
      <div className="weekAssignmentBand">
        {(data?.weeks || []).map((week) => {
          const months = Array.from(new Set([week.week_start.slice(0, 7), week.week_end.slice(0, 7)]));
          return <label key={week.week_start}><span>{formatWeekLabel(week.week_start, week.week_end)}</span><select aria-label={`${formatWeekLabel(week.week_start, week.week_end)}归属月份`} value={week.report_month || ''} onChange={async (event) => { const result = await newsflashOperations<{ week_start: string; report_month: string }>('save_week_month', { week_start: week.week_start, report_month: event.target.value }); setData((current) => current ? { ...current, weeks: current.weeks.map((item) => item.week_start === result.week_start ? { ...item, report_month: result.report_month, report_month_manual: true } : item) } : current); }}><option value="">未归属</option>{months.map((value) => <option value={value} key={value}>{formatMonthLabel(value)}</option>)}</select></label>;
        })}
      </div>
      <div className="personColorLegend" aria-label="值班人员颜色图例">
        <span className="legendTitle">人员颜色</span>
        {dutyPeople.map((person) => <span className="personColorChip" style={personColor(person.person_key)} key={person.person_key}>{person.display_name}</span>)}
      </div>
      <div className="calendarHeader">{['周一','周二','周三','周四','周五','周六','周日'].map((label) => <span key={label}>{label}</span>)}</div>
      <div className="scheduleCalendar">
        {Array.from({ length: leading }, (_, index) => <div className="calendarSpacer" key={`spacer-${index}`} />)}
        {days.map((day) => {
          const shifts = day.mode === 'three' ? ['morning','middle','late'] : ['morning','late'];
          return <article className="scheduleDay" key={day.date}>
            <div className="scheduleDayHeader"><strong>{Number(day.date.slice(-2))}</strong><div className="modeToggle"><button type="button" className={day.mode === 'three' ? 'active' : ''} onClick={() => void saveMode(day.date, 'three')}>三班</button><button type="button" className={day.mode === 'two' ? 'active' : ''} onClick={() => void saveMode(day.date, 'two')}>两班</button></div></div>
            <div className="shiftList">{shifts.map((shift) => {
              const selected = assignmentMap.get(`${day.date}:${shift}`) || '';
              return <label key={shift}><span>{shiftLabels[shift]}</span><select className={selected ? 'personSelected' : ''} style={selected ? personColor(selected) : undefined} value={selected} onChange={(event) => void saveAssignment(day.date, shift, event.target.value)}><option value="">未排班</option>{dutyPeople.map((person) => <option key={person.person_key} value={person.person_key}>{person.display_name}</option>)}</select></label>;
            })}</div>
            <div className="aiDuty"><Database size={13} /> OdAIly 全天</div>
          </article>;
        })}
      </div>
      {settingsOpen && data && <RosterDrawer people={data.people} onClose={() => setSettingsOpen(false)} onSaved={(result) => { setData({ ...data, people: result.people }); onPeople(result.people); }} onError={onError} />}
    </section>
  );
}

function RosterDrawer({ people, onClose, onSaved, onError }: { people: Person[]; onClose: () => void; onSaved: (value: { people: Person[] }) => void; onError: (value: string) => void }) {
  const [drafts, setDrafts] = useState<Record<string, Person>>(Object.fromEntries(people.map((person) => [person.person_key, { ...person, aliases: [...person.aliases] }])));
  const [newKey, setNewKey] = useState('');
  function update(key: string, patch: Partial<Person>) { setDrafts((current) => ({ ...current, [key]: { ...current[key], ...patch } })); }
  async function save(person: Person) {
    try { onSaved(await newsflashOperations<{ people: Person[] }>('save_person', person)); }
    catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  }
  return <div className="drawerBackdrop" onMouseDown={onClose}><aside className="rosterDrawer" onMouseDown={(event) => event.stopPropagation()}><div className="drawerHeader"><div><h2>人员与历史别名</h2><p>停用人员仍保留历史统计。</p></div><button className="iconButton" type="button" title="关闭" onClick={onClose}><X size={18} /></button></div><div className="rosterList">{Object.values(drafts).map((person) => <div className="rosterRow" key={person.person_key}><input value={person.display_name} onChange={(event) => update(person.person_key, { display_name: event.target.value })} /><input value={person.aliases.join(', ')} onChange={(event) => update(person.person_key, { aliases: event.target.value.split(',').map((value) => value.trim()).filter(Boolean) })} placeholder="别名，逗号分隔" /><label><input type="checkbox" checked={person.duty_enabled} onChange={(event) => update(person.person_key, { duty_enabled: event.target.checked })} />值班</label><label><input type="checkbox" checked={person.contributor_enabled} onChange={(event) => update(person.person_key, { contributor_enabled: event.target.checked })} />贡献</label><label><input type="checkbox" checked={person.active} onChange={(event) => update(person.person_key, { active: event.target.checked })} />启用</label><button className="iconButton" title="保存" type="button" onClick={() => void save(person)}><Save size={16} /></button></div>)}</div><div className="newPersonBar"><input placeholder="新人员 ID" value={newKey} onChange={(event) => setNewKey(event.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ''))} /><button className="secondaryButton" type="button" disabled={!newKey} onClick={() => { const person: Person = { person_key: newKey, display_name: newKey, duty_enabled: false, contributor_enabled: true, active: true, aliases: [newKey] }; setDrafts((current) => ({ ...current, [newKey]: person })); setNewKey(''); }}>新增</button></div></aside></div>;
}

function Summary({ refreshToken, onError }: { refreshToken: number; onError: (value: string) => void }) {
  const [mode, setMode] = useState<'week' | 'month'>('week');
  const [week, setWeek] = useState(mondayKey());
  const [month, setMonth] = useState(currentMonth());
  const [data, setData] = useState<SummaryPayload | null>(null);
  async function load() { try { setData(await newsflashOperations<SummaryPayload>('summary', mode === 'week' ? { week_start: week } : { report_month: month })); } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); } }
  useEffect(() => { void load(); }, [mode, week, month, refreshToken]);
  return <section className="newsflashSection"><div className="sectionCommandBar"><div className="segmentedControl"><button type="button" className={mode === 'week' ? 'active' : ''} onClick={() => setMode('week')}>按周</button><button type="button" className={mode === 'month' ? 'active' : ''} onClick={() => setMode('month')}>按月</button></div>{mode === 'week' ? <div className="weekPicker"><label><span>统计周</span><input type="date" value={week} onChange={(event) => setWeek(mondayKey(new Date(`${event.target.value}T12:00:00+08:00`)))} /></label><span className="periodHint">{formatWeekLabel(week)}</span></div> : <label><span>统计月份</span><input type="month" value={month} onChange={(event) => setMonth(event.target.value)} /></label>}</div>{data?.unassigned_count ? <div className="notice error">{data.unassigned_count} 条人类快讯未能归入排班。</div> : null}<h2 className="subsectionTitle">人员汇总</h2><MetricTable rows={data?.people || []} /><h2 className="subsectionTitle">班次明细</h2><div className="newsflashTableWrap"><table className="newsflashTable"><thead><tr><th>日期</th><th>班次</th><th>值班人</th><th>发布数量</th><th>推送数量</th><th>平均浏览量</th><th>推送浏览量</th></tr></thead><tbody>{(data?.rows || []).map((row) => <tr key={`${row.date}:${row.shift_key}:${row.person_key}`}><td>{row.date}</td><td>{row.shift_label}</td><td>{row.person_name}</td><MetricCells row={row} /></tr>)}</tbody></table></div></section>;
}

function MetricTable({ rows }: { rows: SummaryPayload['people'] }) { return <div className="newsflashTableWrap"><table className="newsflashTable"><thead><tr><th>人员</th><th>发布数量</th><th>推送数量</th><th>平均浏览量</th><th>推送浏览量</th></tr></thead><tbody>{rows.map((row) => <tr key={row.person_key}><td>{row.person_name}</td><MetricCells row={row} /></tr>)}</tbody></table></div>; }
function MetricCells({ row }: { row: SummaryPayload['people'][number] | SummaryPayload['rows'][number] }) { return <><td className="numberCell">{row.published_count}</td><td className="numberCell">{row.pushed_count}</td><td className="numberCell">{row.average_views == null ? '—' : row.average_views.toLocaleString()} <small>{coverage(row.view_coverage)}</small></td><td className="numberCell">{row.pushed_views == null ? '—' : row.pushed_views.toLocaleString()}</td></>; }

function Quality({ refreshToken, onError }: { refreshToken: number; onError: (value: string) => void }) {
  const [week, setWeek] = useState('');
  const [mode, setMode] = useState<'qualified' | 'excluded'>('qualified');
  const [data, setData] = useState<QualityPayload | null>(null);
  const [loading, setLoading] = useState(false);

  async function load(nextWeek?: string) {
    setLoading(true);
    onError('');
    try {
      const result = await newsflashOperations<QualityPayload>('quality', nextWeek ? { week_start: nextWeek } : {});
      setData(result);
      setWeek(result.week_start);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(week || undefined); }, [refreshToken]);

  const metric = (value: number | null, digits = 1) => value == null ? '—' : value.toLocaleString('zh-CN', { maximumFractionDigits: digits });
  return <section className="newsflashSection qualitySection">
    <div className="sectionCommandBar">
      <div className="weekPicker"><label><span>统计周</span><input type="date" min="2026-08-03" value={week} onChange={(event) => { const normalized = mondayKey(new Date(`${event.target.value}T12:00:00+08:00`)); setWeek(normalized); void load(normalized); }} /></label>{week && <span className="periodHint">{formatWeekLabel(week)}</span>}</div>
      <div className="segmentedControl" aria-label="优质快讯显示范围"><button type="button" className={mode === 'qualified' ? 'active' : ''} onClick={() => setMode('qualified')}>入选</button><button type="button" className={mode === 'excluded' ? 'active' : ''} onClick={() => setMode('excluded')}>达标但排除</button></div>
    </div>
    {loading && <div className="emptyInline">正在计算当周优质快讯…</div>}
    {!loading && data?.status === 'insufficient' && <div className="qualityInsufficient"><strong>数据不足</strong><span>本周没有已知浏览量的推送快讯，无法计算周均值、门槛和 KPI。</span></div>}
    {!loading && data?.status === 'ready' && <>
      <div className="qualityMetrics">
        <div><span>推送浏览量均值</span><strong>{metric(data.average_views)}</strong><small>{data.pushed_view_count}/{data.pushed_count} 条有浏览量</small></div>
        <div><span>1.5 倍门槛</span><strong>{metric(data.threshold_views)}</strong><small>严格大于才达标</small></div>
        <div><span>入选快讯</span><strong>{data.qualified_count}</strong><small>另有 {data.excluded_count} 条达标但排除</small></div>
        <div><span>总 KPI</span><strong>{metric(data.total_kpi, 10)}</strong><small>每条 {data.rules.kpi_per_item}，无上限</small></div>
      </div>
      {data.unassigned_count > 0 && <div className="notice error">{data.unassigned_count} 条人类快讯未能归入排班，不展示且不计 KPI。</div>}
      <div className="qualityGroups">{data.groups.map((group) => {
        const items = mode === 'qualified' ? group.qualified : group.excluded;
        return <section className="qualityGroup" key={group.person_key}>
          <div className="qualityGroupHeader"><span className="personColorChip" style={personColor(group.person_key)}>{group.person_name}</span><span>入选 <strong>{group.qualified_count}</strong> 条 · KPI <strong>{metric(group.kpi, 10)}</strong> · 排除 <strong>{group.excluded_count}</strong> 条</span></div>
          {items.length ? <QualityTable items={items} excluded={mode === 'excluded'} /> : <div className="emptyInline">本周没有{mode === 'qualified' ? '入选' : '达标但排除的'}快讯</div>}
        </section>;
      })}</div>
    </>}
    {data && <details className="qualityRules"><summary>查看判定规则与当周冻结信源范围</summary><div className="qualityRuleGrid"><section><h3>固定常规信源（{data.rules.regular_source_accounts.length}）</h3><div className="ruleTagList">{data.rules.regular_source_accounts.map((account) => <a key={account} href={`https://x.com/${account}`} target="_blank" rel="noreferrer">@{account}</a>)}</div></section><section><h3>当周 X 自动覆盖（{data.rules.automated_x_accounts.length}）</h3><div className="ruleTagList">{data.rules.automated_x_accounts.length ? data.rules.automated_x_accounts.map((account) => <span key={account}>@{account}</span>) : <span>无</span>}</div></section><section><h3>当周媒体自动覆盖（{data.rules.automated_media_domains.length}）</h3><div className="ruleTagList">{data.rules.automated_media_domains.map((domain) => <span key={domain}>{domain}</span>)}</div></section></div><p>规则快照：{localDate(data.rules.snapshot_at)}。浏览量、贡献状态、首发和排班仍按当前数据实时重算。</p></details>}
  </section>;
}

function QualityTable({ items, excluded }: { items: QualityItem[]; excluded: boolean }) {
  return <div className="newsflashTableWrap"><table className="newsflashTable qualityTable"><thead><tr><th>ID</th><th className="titleColumn">快讯标题</th><th>发布时间</th><th>浏览量</th><th>推送</th><th>首发单位</th><th>原文</th>{excluded && <th>排除原因</th>}</tr></thead><tbody>{items.map((item) => <tr key={item.source_item_id}><td className="monoCell">{item.source_item_id}</td><td className="titleColumn"><a href={item.odaily_url} target="_blank" rel="noreferrer">{item.title || '无标题'}</a></td><td>{localDate(item.published_at)}</td><td className="numberCell">{item.view_count.toLocaleString()}</td><td>{item.is_pushed == null ? '—' : item.is_pushed ? '是' : '否'}</td><td>{item.first_publication.label}</td><td>{item.original_url ? <a href={item.original_url} target="_blank" rel="noreferrer">查看原文</a> : <span className="unavailable">无原文链接</span>}</td>{excluded && <td><div className="reasonTags">{item.exclusion_reasons.map((reason) => <span key={reason}>{qualityReasonLabels[reason] || reason}</span>)}</div></td>}</tr>)}</tbody></table></div>;
}

function Contributions({ people, refreshToken, onError, onMessage }: { people: Person[]; refreshToken: number; onError: (value: string) => void; onMessage: (value: string) => void }) {
  const [week, setWeek] = useState(mondayKey());
  const [data, setData] = useState<ContributionsPayload | null>(null);
  async function load() { try { setData(await newsflashOperations<ContributionsPayload>('contributions', { week_start: week })); } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); } }
  useEffect(() => { void load(); }, [week, refreshToken]);
  async function patch(sourceItemId: string, patchValue: Record<string, unknown>) { try { await newsflashOperations('update', { source_item_id: sourceItemId, patch: patchValue }); onMessage(`快讯 ${sourceItemId} 已保存`); await load(); } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); } }
  const contributors = people.filter((person) => person.active && person.contributor_enabled);
  return <section className="newsflashSection"><div className="sectionCommandBar"><div className="weekPicker"><label><span>统计周</span><input type="date" value={week} onChange={(event) => setWeek(mondayKey(new Date(`${event.target.value}T12:00:00+08:00`)))} /></label><span className="periodHint">{formatWeekLabel(week)}</span></div>{data?.in_progress && <span className="statusText ready">本周进行中</span>}</div><div className="contributionGroups">{(data?.groups || []).map((group) => <section className="contributionGroup" key={group.person_key}><div className="contributionGroupHeader"><h2>{group.display_name}</h2><div><strong>{group.count}</strong><span>条</span><strong>{group.total_views.toLocaleString()}</strong><span>总浏览量</span><strong>{group.average_views == null ? '—' : group.average_views.toLocaleString()}</strong><span>平均</span></div></div>{group.items.length ? <div className="newsflashTableWrap"><table className="newsflashTable"><thead><tr><th>ID</th><th className="titleColumn">快讯标题</th><th>浏览量</th><th>贡献者</th><th>类型</th><th>首发单位</th></tr></thead><tbody>{group.items.map((item) => <tr key={item.source_item_id}><td>{item.source_item_id}</td><td className="titleColumn">{item.source_url ? <a href={item.source_url} target="_blank" rel="noreferrer">{item.title}</a> : item.title}</td><td className="numberCell">{item.view_count == null ? '—' : item.view_count.toLocaleString()}</td><td><select className="inlineSelect" value={item.contributor_person_key} onChange={(event) => void patch(item.source_item_id, event.target.value ? { contributor_person_key: event.target.value, is_contribution: true } : { is_contribution: false })}><option value="">—</option>{contributors.map((person) => <option key={person.person_key} value={person.person_key}>{person.display_name}</option>)}</select></td><td><select className="inlineSelect" value={item.contribution_type} onChange={(event) => void patch(item.source_item_id, { contribution_type: event.target.value })}><option value="regular">常规</option><option value="night">夜间</option><option value="ppp">PPP</option></select></td><td>{item.first_publication.label}</td></tr>)}</tbody></table></div> : <div className="emptyInline">本周 0 条</div>}</section>)}</div></section>;
}

function Events({ refreshToken, onError }: { refreshToken: number; onError: (value: string) => void }) {
  const [data, setData] = useState<EventPage>({ items: [], page: 1, pages: 1, total: 0, page_size: 50 });
  const [page, setPage] = useState(1);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [filters, setFilters] = useState({ search: '', date_from: '', date_to: '', has_odaily: '', first_source: '' });
  async function load(nextPage = page) { try { const payload = Object.fromEntries(Object.entries(filters).filter(([, value]) => value !== '')); const result = await newsflashOperations<EventPage>('events', { ...payload, page: nextPage }); setData(result); setPage(result.page); } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); } }
  useEffect(() => { void load(1); }, [refreshToken]);
  return <section className="newsflashSection"><form className="newsflashFilters eventFilters" onSubmit={(event) => { event.preventDefault(); void load(1); }}><label className="wideFilter"><span>事件标题</span><div className="inputWithIcon"><Search size={15} /><input value={filters.search} onChange={(event) => setFilters({ ...filters, search: event.target.value })} /></div></label><label><span>开始日期</span><input type="date" value={filters.date_from} onChange={(event) => setFilters({ ...filters, date_from: event.target.value })} /></label><label><span>结束日期</span><input type="date" value={filters.date_to} onChange={(event) => setFilters({ ...filters, date_to: event.target.value })} /></label><label><span>包含 Odaily</span><select value={filters.has_odaily} onChange={(event) => setFilters({ ...filters, has_odaily: event.target.value })}><option value="">全部</option><option value="1">是</option><option value="0">否</option></select></label><label><span>首发单位</span><select value={filters.first_source} onChange={(event) => setFilters({ ...filters, first_source: event.target.value })}><option value="">全部</option><option value="odaily">Odaily</option><option value="blockbeats">BlockBeats</option><option value="panews">PANews</option><option value="jinse">金色财经</option></select></label><button className="primaryButton filterButton" type="submit"><ListFilter size={16} /> 筛选</button></form><div className="tableMeta"><span>{data.total} 个事件</span><span>每页 50 条</span></div><div className="eventList">{data.items.map((event) => <article className="eventRow" key={event.event_id}><button className="eventMain" type="button" onClick={() => setExpanded(expanded === event.event_id ? null : event.event_id)}><ChevronDown size={17} className={expanded === event.event_id ? 'rotated' : ''} /><span className="eventTitle">{event.representative_title || '无标题事件'}</span><span>{event.first_source_label}</span><span>{localDate(event.first_published_at)}</span><span>{event.source_count} 个来源</span></button>{expanded === event.event_id && <div className="eventSources">{event.sources.map((source) => <div key={`${source.source}:${source.source_item_id}`}><strong>{source.source}</strong>{source.source_url ? <a href={source.source_url} target="_blank" rel="noreferrer">{source.title}</a> : <span>{source.title}</span>}<time>{localDate(source.published_at)}</time></div>)}</div>}</article>)}</div><Pagination page={page} pages={data.pages} onChange={(next) => void load(next)} /></section>;
}

function Pagination({ page, pages, onChange }: { page: number; pages: number; onChange: (value: number) => void }) { return <div className="paginationBar"><button className="iconButton" type="button" title="上一页" disabled={page <= 1} onClick={() => onChange(page - 1)}><ChevronLeft size={17} /></button><span>第 {page} / {pages} 页</span><button className="iconButton" type="button" title="下一页" disabled={page >= pages} onClick={() => onChange(page + 1)}><ChevronRight size={17} /></button></div>; }
function Unavailable() { return <span className="unavailable" title="当前数据源暂未提供">—</span>; }
