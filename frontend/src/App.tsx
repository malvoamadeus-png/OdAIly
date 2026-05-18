import { FormEvent, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  Ban,
  Database,
  FileText,
  Globe2,
  Inbox,
  Pause,
  Plus,
  RefreshCcw,
  Save,
  Star,
  Trash2,
  Zap,
} from 'lucide-react';
import {
  createCompetitorFilterKeywords,
  createAccount,
  deleteCompetitorFilterKeyword,
  deleteAccount as deleteAccountFromSupabase,
  listCompetitorFilterKeywords,
  listNewsflashEventSources,
  listNewsflashEvents,
  loadDashboard,
  listPromptTemplates,
  listPromptVersions,
  createPromptVersion,
  publishPromptVersion,
  saveNewsflashEventNote,
  saveNewsflashItemNote,
  setNewsflashEventFavorite,
  loadNonMainstreamDashboard,
  updateCompetitorFilterKeyword,
  updateAccount,
  updateNonMainstreamSettings,
  updateNonMainstreamSource,
  updateSettings,
  type Account,
  type AccountPatch,
  type Attempt,
  type NonMainstreamDashboardPayload,
  type NonMainstreamSettings,
  type NonMainstreamSource,
  type Settings,
  type TaskItem,
  type PromptTemplate,
  type PromptVersion,
  type CompetitorFilterKeyword,
  type NewsflashEventFilter,
  type NewsflashEventSourceItem,
  type NewsflashSourceSummary,
  type NewsflashEventSummary,
} from './xCaptureStore';

const emptySettings: Settings = {
  global_interval_seconds: 30,
  max_concurrency: 2,
  jitter_seconds: 5,
  updated_at: null,
};

const emptyNonMainstreamSettings: NonMainstreamSettings = {
  global_interval_seconds: 60,
  jitter_seconds: 5,
  updated_at: null,
};

function fmtTime(value: string | null | undefined): string {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date);
}

const sourceNames: Record<string, string> = {
  odaily: 'Odaily',
  blockbeats: 'BlockBeats',
  panews: 'PANews',
  jinse: '金色',
};

const eventSourceColumns = [
  { key: 'odaily', label: 'Odaily' },
  { key: 'blockbeats', label: 'BlockBeats' },
  { key: 'panews', label: 'PANews' },
  { key: 'jinse', label: '金色' },
] as const;

const eventFilters: { key: NewsflashEventFilter; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'multi', label: '多家共同' },
  { key: 'with_odaily', label: '我们也发' },
  { key: 'high_value', label: '含Odaily群发' },
  { key: 'competitor_only', label: '竞品有我方无' },
  { key: 'competitor_consensus_missing', label: '竞品共识缺口' },
  { key: 'odaily_only', label: '仅我方' },
  { key: 'odaily_late', label: '我方晚发' },
  { key: 'odaily_first', label: '我方首发' },
  { key: 'favorite', label: '已收藏' },
];

const eventPageSize = 100;

export function App() {
  const [settings, setSettings] = useState<Settings>(emptySettings);
  const [nonMainstreamSettings, setNonMainstreamSettings] = useState<NonMainstreamSettings>(emptyNonMainstreamSettings);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [nonMainstreamSources, setNonMainstreamSources] = useState<NonMainstreamSource[]>([]);
  const [attempts, setAttempts] = useState<Attempt[]>([]);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [view, setView] = useState<'x' | 'non_mainstream' | 'prompts' | 'competitor' | 'events' | 'favorites'>('x');
  const [loading, setLoading] = useState(true);
  const [loadingNonMainstream, setLoadingNonMainstream] = useState(true);
  const [savingSettings, setSavingSettings] = useState(false);
  const [savingNonMainstreamSettings, setSavingNonMainstreamSettings] = useState(false);
  const [newAccount, setNewAccount] = useState({
    username_or_url: '',
    display_name: '',
    interval_seconds: '',
  });
  const [promptTemplates, setPromptTemplates] = useState<PromptTemplate[]>([]);
  const [selectedPromptKey, setSelectedPromptKey] = useState('');
  const [promptVersions, setPromptVersions] = useState<PromptVersion[]>([]);
  const [promptContent, setPromptContent] = useState('');
  const [promptNote, setPromptNote] = useState('');
  const [savingPrompt, setSavingPrompt] = useState(false);
  const [competitorKeywords, setCompetitorKeywords] = useState<CompetitorFilterKeyword[]>([]);
  const [newKeywords, setNewKeywords] = useState('');
  const [savingKeywords, setSavingKeywords] = useState(false);
  const [eventFilter, setEventFilter] = useState<NewsflashEventFilter>('all');
  const [eventPage, setEventPage] = useState(0);
  const [events, setEvents] = useState<NewsflashEventSummary[]>([]);
  const [hasNextEventPage, setHasNextEventPage] = useState(false);
  const [eventSources, setEventSources] = useState<Record<string, NewsflashEventSourceItem[]>>({});
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
  const [loadingEvents, setLoadingEvents] = useState(false);
  const [loadingEventDetails, setLoadingEventDetails] = useState<Record<string, boolean>>({});
  const [eventDetailErrors, setEventDetailErrors] = useState<Record<string, string>>({});

  const enabledCount = useMemo(() => accounts.filter((account) => account.enabled).length, [accounts]);
  const enabledNonMainstreamCount = useMemo(
    () => nonMainstreamSources.filter((source) => source.enabled).length,
    [nonMainstreamSources],
  );
  function updateSetting<K extends keyof Pick<Settings, 'global_interval_seconds' | 'max_concurrency' | 'jitter_seconds'>>(
    key: K,
    value: string,
  ) {
    setSettings((current) => ({
      ...current,
      [key]: Number(value),
    }));
  }

  function updateNonMainstreamSetting<K extends keyof Pick<NonMainstreamSettings, 'global_interval_seconds' | 'jitter_seconds'>>(
    key: K,
    value: string,
  ) {
    setNonMainstreamSettings((current) => ({
      ...current,
      [key]: Number(value),
    }));
  }

  async function loadAll() {
    setError('');
    const dashboard = await loadDashboard();
    setSettings(dashboard.settings);
    setAccounts(dashboard.accounts);
    setAttempts(dashboard.attempts);
    setTasks(dashboard.tasks);
    setLoading(false);
  }

  async function loadNonMainstreamAll() {
    setError('');
    const dashboard: NonMainstreamDashboardPayload = await loadNonMainstreamDashboard();
    setNonMainstreamSettings(dashboard.settings);
    setNonMainstreamSources(dashboard.sources);
    setLoadingNonMainstream(false);
  }

  async function loadPrompts(nextSelectedKey?: string) {
    setError('');
    const templates = await listPromptTemplates();
    setPromptTemplates(templates);
    const key = nextSelectedKey || selectedPromptKey || templates[0]?.template_key || '';
    setSelectedPromptKey(key);
    if (!key) {
      setPromptVersions([]);
      setPromptContent('');
      return;
    }
    const versions = await listPromptVersions(key);
    setPromptVersions(versions);
    const activeId = templates.find((template) => template.template_key === key)?.active_version_id;
    const active = versions.find((version) => version.id === activeId) || versions[0];
    setPromptContent(active?.content ?? '');
    setPromptNote('');
  }

  async function loadCompetitorKeywords() {
    setError('');
    const keywords = await listCompetitorFilterKeywords();
    setCompetitorKeywords(keywords);
  }

  async function loadEvents(nextFilter = eventFilter, nextView = view, nextPage = eventPage) {
    setError('');
    setLoadingEvents(true);
    const effectiveFilter = nextView === 'favorites' ? 'favorite' : nextFilter;
    try {
      const rows = await listNewsflashEvents(effectiveFilter, eventPageSize + 1, nextPage * eventPageSize);
      const pageRows = rows.slice(0, eventPageSize);
      if (nextPage > 0 && pageRows.length === 0) {
        setEventPage(Math.max(0, nextPage - 1));
        return;
      }
      setHasNextEventPage(rows.length > eventPageSize);
      setEvents(pageRows);
      setEventSources((current) => {
        const next = { ...current };
        for (const event of pageRows) {
          if (!next[event.event_id] || next[event.event_id].length === 0) {
            next[event.event_id] = summarySourcesToDetails(event);
          }
        }
        return next;
      });
    } finally {
      setLoadingEvents(false);
    }
  }

  useEffect(() => {
    Promise.all([loadAll(), loadNonMainstreamAll()]).catch((err: Error) => {
      setError(err.message);
      setLoading(false);
      setLoadingNonMainstream(false);
    });
    const timer = window.setInterval(() => {
      loadAll().catch((err: Error) => setError(err.message));
      loadNonMainstreamAll().catch((err: Error) => setError(err.message));
    }, 10000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    loadPrompts().catch((err: Error) => setError(err.message));
  }, []);

  useEffect(() => {
    loadCompetitorKeywords().catch((err: Error) => setError(err.message));
  }, []);

  useEffect(() => {
    loadEvents(eventFilter, view, eventPage).catch((err: Error) => setError(err.message));
  }, [eventFilter, eventPage, view]);

  async function saveSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSavingSettings(true);
    setError('');
    const form = new FormData(event.currentTarget);
    try {
      const updated = await updateSettings({
        global_interval_seconds: Number(form.get('global_interval_seconds')),
        max_concurrency: Number(form.get('max_concurrency')),
        jitter_seconds: Number(form.get('jitter_seconds')),
      });
      setSettings(updated);
      setMessage('设置已保存');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingSettings(false);
    }
  }

  async function saveNonMainstreamSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSavingNonMainstreamSettings(true);
    setError('');
    const form = new FormData(event.currentTarget);
    try {
      const updated = await updateNonMainstreamSettings({
        global_interval_seconds: Number(form.get('global_interval_seconds')),
        jitter_seconds: Number(form.get('jitter_seconds')),
      });
      setNonMainstreamSettings(updated);
      setMessage('非主流媒体设置已保存');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingNonMainstreamSettings(false);
    }
  }

  function switchView(nextView: typeof view) {
    if (nextView === 'events' || nextView === 'favorites') {
      setEventPage(0);
      setSelectedEventId(null);
    }
    setView(nextView);
  }

  async function addAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    const body = {
      username_or_url: newAccount.username_or_url,
      display_name: newAccount.display_name || null,
      interval_seconds: newAccount.interval_seconds ? Number(newAccount.interval_seconds) : null,
      enabled: true,
    };
    try {
      await createAccount(body);
      setNewAccount({ username_or_url: '', display_name: '', interval_seconds: '' });
      setMessage('账号已保存');
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function patchAccount(account: Account, patch: AccountPatch) {
    setError('');
    try {
      await updateAccount(account.id, patch);
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function patchNonMainstreamSource(source: NonMainstreamSource, enabled: boolean) {
    setError('');
    try {
      await updateNonMainstreamSource(source.id, { enabled });
      await loadNonMainstreamAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function deleteAccount(account: Account) {
    setError('');
    try {
      await deleteAccountFromSupabase(account.id);
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function selectPrompt(templateKey: string) {
    await loadPrompts(templateKey);
  }

  async function savePromptVersion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedPromptKey) return;
    setSavingPrompt(true);
    setError('');
    try {
      const version = await createPromptVersion(selectedPromptKey, promptContent, promptNote || null);
      await publishPromptVersion(selectedPromptKey, version.id);
      setMessage(`Prompt 已发布为 v${version.version_number}`);
      await loadPrompts(selectedPromptKey);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingPrompt(false);
    }
  }

  async function publishExistingVersion(version: PromptVersion) {
    setError('');
    try {
      await publishPromptVersion(version.template_key, version.id);
      setMessage(`已发布 v${version.version_number}`);
      await loadPrompts(version.template_key);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function addCompetitorKeywords(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSavingKeywords(true);
    setError('');
    try {
      const created = await createCompetitorFilterKeywords(newKeywords);
      setNewKeywords('');
      setMessage(`已保存 ${created.length} 个排除词`);
      await loadCompetitorKeywords();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingKeywords(false);
    }
  }

  async function toggleCompetitorKeyword(keyword: CompetitorFilterKeyword) {
    setError('');
    try {
      await updateCompetitorFilterKeyword(keyword.id, { enabled: !keyword.enabled });
      await loadCompetitorKeywords();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function removeCompetitorKeyword(keyword: CompetitorFilterKeyword) {
    setError('');
    try {
      await deleteCompetitorFilterKeyword(keyword.id);
      await loadCompetitorKeywords();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function openEvent(eventId: string) {
    let shouldOpen = true;
    setSelectedEventId((current) => {
      shouldOpen = current !== eventId;
      return shouldOpen ? eventId : null;
    });
    if (!shouldOpen) return;

    setEventDetailErrors((current) => ({ ...current, [eventId]: '' }));
    if (!eventSources[eventId]?.some((source) => source.content)) {
      setLoadingEventDetails((current) => ({ ...current, [eventId]: true }));
      try {
        const rows = await listNewsflashEventSources(eventId);
        setEventSources((current) => ({ ...current, [eventId]: rows.length > 0 ? rows : current[eventId] || [] }));
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        setEventDetailErrors((current) => ({ ...current, [eventId]: message }));
      } finally {
        setLoadingEventDetails((current) => ({ ...current, [eventId]: false }));
      }
    }
  }

  async function toggleEventFavorite(event: NewsflashEventSummary) {
    setError('');
    try {
      await setNewsflashEventFavorite(event.event_id, !event.favorite);
      await loadEvents(eventFilter, view);
      setMessage(event.favorite ? '已取消收藏' : '已收藏');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function saveItemNote(item: NewsflashEventSourceItem, note: string) {
    setError('');
    try {
      await saveNewsflashItemNote(item.item_id, note);
      const rows = await listNewsflashEventSources(item.event_id);
      setEventSources((current) => ({ ...current, [item.event_id]: rows }));
      setMessage('备注已保存');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function saveEventNote(event: NewsflashEventSummary, note: string) {
    setError('');
    try {
      await saveNewsflashEventNote(event.event_id, note);
      setEvents((current) => current.map((item) => (item.event_id === event.event_id ? { ...item, note: note.trim() } : item)));
      setMessage('事件备注已保存');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const productLabel =
    view === 'x'
      ? 'X Capture'
      : view === 'non_mainstream'
        ? 'Non-Mainstream Media'
        : view === 'prompts'
          ? 'Prompt'
          : view === 'competitor'
            ? '排除词'
            : view === 'favorites'
              ? '收藏'
              : '事件';
  const titleLabel =
    view === 'x'
      ? 'X 抓取控制台'
      : view === 'non_mainstream'
        ? '非主流媒体抓取控制台'
      : view === 'prompts'
        ? 'Prompt 编制'
        : view === 'competitor'
          ? '排除词'
          : view === 'favorites'
            ? '收藏事件'
            : '事件复盘';
  const subtitle =
    view === 'x'
      ? `${enabledCount} 个启用账号 · 全局 ${settings.global_interval_seconds}s`
      : view === 'non_mainstream'
        ? `${enabledNonMainstreamCount} 个启用站点 · 全局 ${nonMainstreamSettings.global_interval_seconds}s`
      : view === 'prompts'
        ? `${promptTemplates.length} 个模板 · ${selectedPromptKey || '-'}`
        : view === 'competitor'
          ? `${competitorKeywords.filter((item) => item.enabled).length} 个启用排除词`
          : `${events.length} 个事件`;
  const refreshCurrent = () =>
    view === 'x'
      ? loadAll()
      : view === 'non_mainstream'
        ? loadNonMainstreamAll()
      : view === 'prompts'
        ? loadPrompts(selectedPromptKey)
        : view === 'competitor'
          ? loadCompetitorKeywords()
          : loadEvents(eventFilter, view, eventPage);

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brandMark">O</div>
          <div>
            <strong>OdAIly</strong>
            <span>{productLabel}</span>
          </div>
        </div>
        <nav>
          <button className={view === 'x' ? 'navItem active' : 'navItem'} type="button" onClick={() => switchView('x')}>
            <Activity size={18} /> 账号
          </button>
          <button
            className={view === 'non_mainstream' ? 'navItem active' : 'navItem'}
            type="button"
            onClick={() => switchView('non_mainstream')}
          >
            <Globe2 size={18} /> 外媒
          </button>
          <button className={view === 'prompts' ? 'navItem active' : 'navItem'} type="button" onClick={() => switchView('prompts')}>
            <FileText size={18} /> Prompt
          </button>
          <button className={view === 'competitor' ? 'navItem active' : 'navItem'} type="button" onClick={() => switchView('competitor')}>
            <Ban size={18} /> 排除词
          </button>
          <button className={view === 'events' ? 'navItem active' : 'navItem'} type="button" onClick={() => switchView('events')}>
            <Inbox size={18} /> 事件
          </button>
          <button className={view === 'favorites' ? 'navItem active' : 'navItem'} type="button" onClick={() => switchView('favorites')}>
            <Star size={18} /> 收藏
          </button>
          <a href="#tasks" onClick={() => switchView('x')}>
            <Database size={18} /> 入库
          </a>
        </nav>
      </aside>

      <main className="content">
        <header className="topbar">
          <div>
            <h1>{titleLabel}</h1>
            <p>{subtitle}</p>
          </div>
          <button
            className="iconButton"
            type="button"
            onClick={refreshCurrent}
            title="刷新"
          >
            <RefreshCcw size={18} />
          </button>
        </header>

        {(message || error) && (
          <div className={error ? 'notice error' : 'notice'} onAnimationEnd={() => !error && setMessage('')}>
            {error || message}
          </div>
        )}

        {view === 'x' ? (
          <>
            <section className="toolbarBand">
              <form className="settingsForm" onSubmit={saveSettings}>
            <label>
              <span>全局频率</span>
              <input
                name="global_interval_seconds"
                type="number"
                min="5"
                max="3600"
                value={settings.global_interval_seconds}
                onChange={(event) => updateSetting('global_interval_seconds', event.target.value)}
              />
            </label>
            <label>
              <span>并发</span>
              <input
                name="max_concurrency"
                type="number"
                min="1"
                max="20"
                value={settings.max_concurrency}
                onChange={(event) => updateSetting('max_concurrency', event.target.value)}
              />
            </label>
            <label>
              <span>抖动秒</span>
              <input
                name="jitter_seconds"
                type="number"
                min="0"
                max="300"
                value={settings.jitter_seconds}
                onChange={(event) => updateSetting('jitter_seconds', event.target.value)}
              />
            </label>
            <button className="primaryButton" type="submit" disabled={savingSettings}>
              <Save size={17} /> 保存
            </button>
              </form>
            </section>

            <section id="accounts" className="section">
          <div className="sectionHeader">
            <h2>账号规则</h2>
            <span>{loading ? '加载中' : `${accounts.length} 个账号`}</span>
          </div>
          <form className="addAccount" onSubmit={addAccount}>
            <input
              placeholder="@username 或 x.com/username"
              value={newAccount.username_or_url}
              onChange={(event) => setNewAccount({ ...newAccount, username_or_url: event.target.value })}
            />
            <input
              placeholder="显示名"
              value={newAccount.display_name}
              onChange={(event) => setNewAccount({ ...newAccount, display_name: event.target.value })}
            />
            <input
              placeholder="频率秒"
              type="number"
              min="5"
              max="3600"
              value={newAccount.interval_seconds}
              onChange={(event) => setNewAccount({ ...newAccount, interval_seconds: event.target.value })}
            />
            <button className="primaryButton" type="submit">
              <Plus size={17} /> 添加
            </button>
          </form>

          <div className="accountList">
            {!loading && accounts.length === 0 && <div className="emptyState">暂无账号，先添加一个 X 账号。</div>}
            {accounts.map((account) => (
              <AccountRow
                key={account.id}
                account={account}
                settings={settings}
                onPatch={patchAccount}
                onDelete={deleteAccount}
              />
            ))}
          </div>
            </section>

            <section id="tasks" className="section">
          <div className="sectionHeader">
            <h2>最近入库</h2>
            <span>{tasks.length} 条</span>
          </div>
          <div className="taskList">
            {tasks.length === 0 && <div className="emptyState">暂无入库任务。</div>}
            {tasks.map((task) => (
              <article className="taskItem" key={task.id}>
                <a href={task.source_url ?? '#'} target="_blank" rel="noreferrer">
                  {task.title || task.source_item_id}
                </a>
                <p>{task.content}</p>
                <div>
                  <span>{task.status}</span>
                  <time>{fmtTime(task.created_at)}</time>
                </div>
              </article>
            ))}
          </div>
            </section>
          </>
        ) : view === 'non_mainstream' ? (
          <NonMainstreamPanel
            settings={nonMainstreamSettings}
            sources={nonMainstreamSources}
            loading={loadingNonMainstream}
            saving={savingNonMainstreamSettings}
            onSettingChange={updateNonMainstreamSetting}
            onSave={saveNonMainstreamSettings}
            onToggleSource={patchNonMainstreamSource}
          />
        ) : view === 'prompts' ? (
          <PromptPanel
            templates={promptTemplates}
            selectedKey={selectedPromptKey}
            versions={promptVersions}
            content={promptContent}
            note={promptNote}
            saving={savingPrompt}
            onSelect={selectPrompt}
            onContentChange={setPromptContent}
            onNoteChange={setPromptNote}
            onSave={savePromptVersion}
            onPublish={publishExistingVersion}
            onRefresh={() => loadPrompts(selectedPromptKey)}
          />
        ) : view === 'competitor' ? (
          <CompetitorPanel
            keywords={competitorKeywords}
            newKeywords={newKeywords}
            saving={savingKeywords}
            onNewKeywordsChange={setNewKeywords}
            onAdd={addCompetitorKeywords}
            onToggle={toggleCompetitorKeyword}
            onDelete={removeCompetitorKeyword}
          />
        ) : (
          <EventsPanel
            events={events}
            selectedEventId={selectedEventId}
            sourcesByEvent={eventSources}
            loadingDetails={loadingEventDetails}
            detailErrors={eventDetailErrors}
            filter={view === 'favorites' ? 'favorite' : eventFilter}
            page={eventPage}
            pageSize={eventPageSize}
            hasNextPage={hasNextEventPage}
            loading={loadingEvents}
            favoritesOnly={view === 'favorites'}
            onFilterChange={(nextFilter) => {
              setEventPage(0);
              setSelectedEventId(null);
              setEventFilter(nextFilter);
            }}
            onPageChange={(nextPage) => {
              setEventPage(nextPage);
              setSelectedEventId(null);
            }}
            onOpen={openEvent}
            onToggleFavorite={toggleEventFavorite}
            onSaveEventNote={saveEventNote}
            onSaveNote={saveItemNote}
          />
        )}
      </main>
    </div>
  );
}

function NonMainstreamPanel({
  settings,
  sources,
  loading,
  saving,
  onSettingChange,
  onSave,
  onToggleSource,
}: {
  settings: NonMainstreamSettings;
  sources: NonMainstreamSource[];
  loading: boolean;
  saving: boolean;
  onSettingChange: (key: 'global_interval_seconds' | 'jitter_seconds', value: string) => void;
  onSave: (event: FormEvent<HTMLFormElement>) => Promise<void>;
  onToggleSource: (source: NonMainstreamSource, enabled: boolean) => Promise<void>;
}) {
  return (
    <section className="nonMainstreamLayout">
      <form className="nonMainstreamSettingsForm" onSubmit={onSave}>
        <label>
          <span>全局频率</span>
          <input
            name="global_interval_seconds"
            type="number"
            min="10"
            max="3600"
            value={settings.global_interval_seconds}
            onChange={(event) => onSettingChange('global_interval_seconds', event.target.value)}
          />
        </label>
        <label>
          <span>抖动秒</span>
          <input
            name="jitter_seconds"
            type="number"
            min="0"
            max="300"
            value={settings.jitter_seconds}
            onChange={(event) => onSettingChange('jitter_seconds', event.target.value)}
          />
        </label>
        <button className="primaryButton" type="submit" disabled={saving}>
          <Save size={17} /> 保存
        </button>
      </form>

      <div className="nonMainstreamList">
        <div className="sectionHeader">
          <h2>已接入站点</h2>
          <span>{loading ? '加载中' : `${sources.length} 个站点`}</span>
        </div>
        {sources.length === 0 && <div className="emptyState">暂无已接入站点，请先运行初始化命令。</div>}
        {sources.map((source) => (
          <NonMainstreamSourceRow key={source.id} source={source} onToggleSource={onToggleSource} />
        ))}
      </div>
    </section>
  );
}

function NonMainstreamSourceRow({
  source,
  onToggleSource,
}: {
  source: NonMainstreamSource;
  onToggleSource: (source: NonMainstreamSource, enabled: boolean) => Promise<void>;
}) {
  return (
    <article className={source.enabled ? 'sourceRow' : 'sourceRow disabled'}>
      <div className="sourceIdentity">
        <div className="statusDot" />
        <div>
          <strong>{source.display_name}</strong>
          <span>{source.site_key}</span>
        </div>
      </div>
      <div className="sourceMeta">
        <strong>{source.capture_method === 'html_request' ? 'HTML 直抓' : '浏览器模拟'}</strong>
        <span>{source.seeded_at ? '已 seed' : '待 seed'}</span>
      </div>
      <a className="sourceLink" href={source.homepage_url} target="_blank" rel="noreferrer">
        {source.homepage_url}
      </a>
      <div className="sourceStatus">
        <strong>{fmtTime(source.last_polled_at)}</strong>
        <span>{source.last_error || (source.last_success_at ? `上次成功 ${fmtTime(source.last_success_at)}` : '暂无执行记录')}</span>
      </div>
      <div className="rowActions">
        <button
          className="iconButton"
          type="button"
          onClick={() => onToggleSource(source, !source.enabled)}
          title={source.enabled ? '停用' : '启用'}
        >
          {source.enabled ? <Pause size={17} /> : <Zap size={17} />}
        </button>
      </div>
    </article>
  );
}

function PromptPanel({
  templates,
  selectedKey,
  versions,
  content,
  note,
  saving,
  onSelect,
  onContentChange,
  onNoteChange,
  onSave,
  onPublish,
  onRefresh,
}: {
  templates: PromptTemplate[];
  selectedKey: string;
  versions: PromptVersion[];
  content: string;
  note: string;
  saving: boolean;
  onSelect: (templateKey: string) => Promise<void>;
  onContentChange: (value: string) => void;
  onNoteChange: (value: string) => void;
  onSave: (event: FormEvent<HTMLFormElement>) => Promise<void>;
  onPublish: (version: PromptVersion) => Promise<void>;
  onRefresh: () => Promise<void>;
}) {
  const selected = templates.find((template) => template.template_key === selectedKey);
  const activeVersion = versions.find((version) => version.id === selected?.active_version_id);

  return (
    <section className="promptLayout">
      <aside className="promptList">
        <div className="sectionHeader">
          <h2>模板</h2>
          <button className="iconButton" type="button" onClick={() => onRefresh()} title="刷新 Prompt">
            <RefreshCcw size={17} />
          </button>
        </div>
        {templates.length === 0 && <div className="emptyState">暂无 Prompt 模板，请先运行 x-process-init-db。</div>}
        {templates.map((template) => (
          <button
            key={template.template_key}
            className={template.template_key === selectedKey ? 'promptTab active' : 'promptTab'}
            type="button"
            onClick={() => onSelect(template.template_key)}
          >
            <strong>{template.display_name}</strong>
            <span>{template.template_key}</span>
          </button>
        ))}
      </aside>

      <form className="promptEditor" onSubmit={onSave}>
        <div className="sectionHeader">
          <div>
            <h2>{selected?.display_name || '选择模板'}</h2>
            <span>当前版本 {activeVersion ? `v${activeVersion.version_number}` : '-'}</span>
          </div>
          <button className="primaryButton" type="submit" disabled={!selectedKey || saving}>
            <Save size={17} /> 发布新版本
          </button>
        </div>
        <textarea
          value={content}
          onChange={(event) => onContentChange(event.target.value)}
          placeholder="Prompt 内容"
          spellCheck={false}
        />
        <input value={note} onChange={(event) => onNoteChange(event.target.value)} placeholder="版本备注" />
      </form>

      <aside className="versionList">
        <h2>版本</h2>
        {versions.length === 0 && <div className="emptyState compact">暂无版本。</div>}
        {versions.map((version) => (
          <article className="versionItem" key={version.id}>
            <div>
              <strong>v{version.version_number}</strong>
              {version.id === selected?.active_version_id && <span className="ok">当前</span>}
            </div>
            <p>{version.note || '无备注'}</p>
            <small>{fmtTime(version.created_at)}</small>
            <button className="iconButton" type="button" onClick={() => onPublish(version)} title="发布此版本">
              <Zap size={16} />
            </button>
          </article>
        ))}
      </aside>
    </section>
  );
}

function CompetitorPanel({
  keywords,
  newKeywords,
  saving,
  onNewKeywordsChange,
  onAdd,
  onToggle,
  onDelete,
}: {
  keywords: CompetitorFilterKeyword[];
  newKeywords: string;
  saving: boolean;
  onNewKeywordsChange: (value: string) => void;
  onAdd: (event: FormEvent<HTMLFormElement>) => Promise<void>;
  onToggle: (keyword: CompetitorFilterKeyword) => Promise<void>;
  onDelete: (keyword: CompetitorFilterKeyword) => Promise<void>;
}) {
  return (
    <section className="competitorLayout">
      <form className="keywordForm" onSubmit={onAdd}>
        <div className="sectionHeader">
          <h2>排除词表</h2>
          <button className="primaryButton" type="submit" disabled={saving}>
            <Plus size={17} /> 保存
          </button>
        </div>
        <textarea
          className="keywordTextarea"
          value={newKeywords}
          onChange={(event) => onNewKeywordsChange(event.target.value)}
          placeholder={'每行一个词\n跌破\n突破'}
          spellCheck={false}
        />
      </form>

      <div className="keywordList">
        {keywords.length === 0 && <div className="emptyState">暂无排除词，请先运行初始化命令或添加新词。</div>}
        {keywords.map((keyword) => (
          <article className={keyword.enabled ? 'keywordRow' : 'keywordRow disabled'} key={keyword.id}>
            <div>
              <strong>{keyword.term}</strong>
              <span>{keyword.enabled ? '启用' : '停用'} · {fmtTime(keyword.updated_at)}</span>
            </div>
            <div className="rowActions">
              <button className="iconButton" type="button" onClick={() => onToggle(keyword)} title={keyword.enabled ? '停用' : '启用'}>
                {keyword.enabled ? <Pause size={17} /> : <Zap size={17} />}
              </button>
              <button className="iconButton danger" type="button" onClick={() => onDelete(keyword)} title="删除">
                <Trash2 size={17} />
              </button>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function EventsPanel({
  events,
  selectedEventId,
  sourcesByEvent,
  loadingDetails,
  detailErrors,
  filter,
  page,
  pageSize,
  hasNextPage,
  loading,
  favoritesOnly,
  onFilterChange,
  onPageChange,
  onOpen,
  onToggleFavorite,
  onSaveEventNote,
  onSaveNote,
}: {
  events: NewsflashEventSummary[];
  selectedEventId: string | null;
  sourcesByEvent: Record<string, NewsflashEventSourceItem[]>;
  loadingDetails: Record<string, boolean>;
  detailErrors: Record<string, string>;
  filter: NewsflashEventFilter;
  page: number;
  pageSize: number;
  hasNextPage: boolean;
  loading: boolean;
  favoritesOnly: boolean;
  onFilterChange: (filter: NewsflashEventFilter) => void;
  onPageChange: (page: number) => void;
  onOpen: (eventId: string) => Promise<void>;
  onToggleFavorite: (event: NewsflashEventSummary) => Promise<void>;
  onSaveEventNote: (event: NewsflashEventSummary, note: string) => Promise<void>;
  onSaveNote: (item: NewsflashEventSourceItem, note: string) => Promise<void>;
}) {
  return (
    <section className="eventsLayout">
      {!favoritesOnly && (
        <div className="filterBar">
          {eventFilters.map((item) => (
            <button
              key={item.key}
              className={filter === item.key ? 'filterButton active' : 'filterButton'}
              type="button"
              onClick={() => onFilterChange(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}

      <div className="eventTable">
        <div className="eventHeader">
          <span>时间</span>
          <span>事件</span>
          {eventSourceColumns.map((source) => (
            <span key={source.key}>{source.label}</span>
          ))}
          <span>共同发布</span>
          <span>首发</span>
          <span>状态</span>
          <span>备注</span>
          <span />
        </div>
        {loading && <div className="emptyState">事件加载中。</div>}
        {!loading && events.length === 0 && <div className="emptyState">暂无事件。</div>}
        {events.map((event) => {
          const sources = event.sources || [];
          const sourcesByName = pickEventSources(sources);
          const sourceNamesText = sources.map((source) => sourceNames[source.source] || source.source).join(' / ');
          const status = event.needs_review
            ? '待确认'
            : event.has_odaily && event.source_count >= 2
              ? '含Odaily群发'
              : event.has_odaily
                ? '仅我方'
                : event.competitor_source_count >= 2
                  ? '竞品共识缺口'
                  : '竞品线索';
          return (
            <article className="eventRowWrap" key={event.event_id}>
              <div className="eventRow">
                <button className="eventTimeButton" type="button" onClick={() => onOpen(event.event_id)}>
                  {fmtTime(event.event_time)}
                </button>
                <button className="eventTitleButton" type="button" onClick={() => onOpen(event.event_id)}>
                  <strong>{shortEventId(event.event_id)}</strong>
                  <span>{event.representative_title || event.event_id}</span>
                </button>
                {eventSourceColumns.map((source) => {
                  const item = sourcesByName[source.key];
                  return (
                    <button
                      className={item ? 'eventSourceCell filled' : 'eventSourceCell'}
                      key={source.key}
                      type="button"
                      onClick={() => onOpen(event.event_id)}
                    >
                      {item?.title || ''}
                    </button>
                  );
                })}
                <div className="sourceCount" title={sourceNamesText || '无来源'}>
                  {event.source_count}
                </div>
                <div className="firstSource">
                  <strong>{event.first_source ? sourceNames[event.first_source] || event.first_source : '-'}</strong>
                  <span>{fmtTime(event.first_published_at)}</span>
                </div>
                <span className={event.needs_review ? 'statusPill warn' : 'statusPill'}>{status}</span>
                <textarea
                  className="eventNoteInput"
                  defaultValue={event.note}
                  maxLength={120}
                  placeholder="备注"
                  onClick={(mouseEvent) => mouseEvent.stopPropagation()}
                  onBlur={(blurEvent) => onSaveEventNote(event, blurEvent.currentTarget.value)}
                />
                <button
                  className={event.favorite ? 'iconButton favorite active' : 'iconButton favorite'}
                  type="button"
                  onClick={() => onToggleFavorite(event)}
                  title={event.favorite ? '取消收藏' : '收藏'}
                >
                  <Star size={17} />
                </button>
              </div>
              {selectedEventId === event.event_id && (
                <EventDetail
                  event={event}
                  sources={sourcesByEvent[event.event_id] || []}
                  loading={Boolean(loadingDetails[event.event_id])}
                  error={detailErrors[event.event_id] || ''}
                  onSaveNote={onSaveNote}
                />
              )}
            </article>
          );
        })}
      </div>
      <div className="paginationBar">
        <button className="filterButton" type="button" disabled={page === 0 || loading} onClick={() => onPageChange(Math.max(0, page - 1))}>
          上一页
        </button>
        <span>
          第 {page + 1} 页 · 每页 {pageSize} 条
        </span>
        <button className="filterButton" type="button" disabled={loading || !hasNextPage} onClick={() => onPageChange(page + 1)}>
          下一页
        </button>
      </div>
    </section>
  );
}

function summarySourcesToDetails(event: NewsflashEventSummary): NewsflashEventSourceItem[] {
  return (event.sources || []).map((source, index) => ({
    id: source.id ?? -index - 1,
    event_id: event.event_id,
    item_id: source.item_id ?? -index - 1,
    source: source.source,
    source_item_id: source.source_item_id ?? '',
    role: index === 0 ? 'primary' : 'supporting',
    match_method: 'summary',
    similarity: null,
    title: source.title,
    content: source.content ?? '',
    source_url: source.source_url,
    published_at: source.published_at,
    note: '',
  }));
}

function pickEventSources(sources: NewsflashSourceSummary[]) {
  const result: Record<string, NewsflashSourceSummary | undefined> = {};
  for (const source of [...sources].sort(compareSourceSummary)) {
    if (!result[source.source]) {
      result[source.source] = source;
    }
  }
  return result;
}

function compareSourceSummary(left: NewsflashSourceSummary, right: NewsflashSourceSummary) {
  const leftTime = left.published_at ? new Date(left.published_at).getTime() : Number.MAX_SAFE_INTEGER;
  const rightTime = right.published_at ? new Date(right.published_at).getTime() : Number.MAX_SAFE_INTEGER;
  if (leftTime !== rightTime) return leftTime - rightTime;
  return (left.title || '').localeCompare(right.title || '');
}

function shortEventId(eventId: string) {
  const normalized = eventId.replace(/^evt_/, '');
  return `事件${normalized.slice(-6) || eventId}`;
}

function EventDetail({
  event,
  sources,
  loading,
  error,
  onSaveNote,
}: {
  event: NewsflashEventSummary;
  sources: NewsflashEventSourceItem[];
  loading: boolean;
  error: string;
  onSaveNote: (item: NewsflashEventSourceItem, note: string) => Promise<void>;
}) {
  return (
    <div className="eventDetail">
      <div className="eventDetailMeta">
        <span>{event.event_id}</span>
        <span>{event.source_count} 个来源</span>
        <span>首发 {event.first_source ? sourceNames[event.first_source] || event.first_source : '-'}</span>
        {loading && <span>详情加载中</span>}
        {error && <span className="bad">详情加载失败：{error}</span>}
      </div>
      {sources.length === 0 && <div className="emptyState compact">暂无详情来源。</div>}
      {sources.map((item) => (
        <article className="sourceDetail" key={item.id}>
          <div className="sourceDetailHead">
            <div>
              <strong>{sourceNames[item.source] || item.source}</strong>
              <span>{fmtTime(item.published_at)} · {item.match_method}{item.similarity != null ? ` · ${item.similarity.toFixed(3)}` : ''}</span>
            </div>
            {item.source_url && (
              <a href={item.source_url} target="_blank" rel="noreferrer">
                原文
              </a>
            )}
          </div>
          <h3>{item.title || item.source_item_id}</h3>
          {item.content ? <p>{item.content}</p> : <p className="detailPending">{loading ? '正文加载中。' : '正文暂未加载。'}</p>}
          {item.item_id > 0 && (
            <textarea
              className="noteInput"
              defaultValue={item.note}
              placeholder="来源备注"
              onBlur={(event) => onSaveNote(item, event.currentTarget.value)}
            />
          )}
        </article>
      ))}
    </div>
  );
}

function AccountRow({
  account,
  settings,
  onPatch,
  onDelete,
}: {
  account: Account;
  settings: Settings;
  onPatch: (account: Account, patch: AccountPatch) => Promise<void>;
  onDelete: (account: Account) => Promise<void>;
}) {
  const [displayName, setDisplayName] = useState(account.display_name ?? '');
  const [interval, setIntervalValue] = useState(account.interval_seconds?.toString() ?? '');

  useEffect(() => {
    setDisplayName(account.display_name ?? '');
    setIntervalValue(account.interval_seconds?.toString() ?? '');
  }, [account.display_name, account.interval_seconds]);

  const effectiveInterval = account.interval_seconds ?? settings.global_interval_seconds;

  return (
    <article className={account.enabled ? 'accountRow' : 'accountRow disabled'}>
      <div className="accountIdentity">
        <div className="statusDot" />
        <div>
          <strong>@{account.username}</strong>
          <span>{account.seeded_at ? '已 seed' : '待 seed'} · {effectiveInterval}s</span>
        </div>
      </div>
      <input
        value={displayName}
        placeholder="显示名"
        onChange={(event) => setDisplayName(event.target.value)}
        onBlur={() => onPatch(account, { display_name: displayName || null })}
      />
      <input
        value={interval}
        placeholder="继承全局"
        type="number"
        min="5"
        max="3600"
        onChange={(event) => setIntervalValue(event.target.value)}
        onBlur={() => onPatch(account, { interval_seconds: interval ? Number(interval) : null })}
      />
      <span className="lastPoll">{fmtTime(account.last_polled_at)}</span>
      <div className="rowActions">
        <button
          className="iconButton"
          type="button"
          onClick={() => onPatch(account, { enabled: !account.enabled })}
          title={account.enabled ? '暂停' : '启用'}
        >
          {account.enabled ? <Pause size={17} /> : <Zap size={17} />}
        </button>
        <button className="iconButton danger" type="button" onClick={() => onDelete(account)} title="删除">
          <Trash2 size={17} />
        </button>
      </div>
    </article>
  );
}
