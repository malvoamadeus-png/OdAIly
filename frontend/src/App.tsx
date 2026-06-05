import { type ReactNode, FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import type { Session } from '@supabase/supabase-js';
import {
  Activity,
  Ban,
  Bot,
  Database,
  FileText,
  Globe2,
  Inbox,
  Pause,
  Plus,
  RefreshCcw,
  Save,
  Send,
  Star,
  Trash2,
  Wallet,
  Zap,
} from 'lucide-react';
import {
  createCompetitorFilterKeywords,
  createAccount,
  createWhaleWatchAddress,
  createWhaleWatchHyperliquidAddress,
  deleteCompetitorFilterKeyword,
  deleteAccount as deleteAccountFromSupabase,
  deleteWhaleWatchAddress,
  deleteWhaleWatchHyperliquidAddress,
  getWhaleWatchHyperliquidSettings,
  getCurrentConsoleAdmin,
  getCurrentSession,
  getPublisherSettings,
  listCompetitorFilterKeywords,
  listNewsflashEventSources,
  listNewsflashEvents,
  listPublisherChannels,
  loadDashboard,
  loadWhaleWatchDashboard,
  listPromptTemplates,
  onConsoleAuthStateChange,
  listPromptVersions,
  createPromptVersion,
  deletePromptVersion,
  publishPromptVersion,
  saveNewsflashEventNote,
  saveNewsflashItemNote,
  signInWithPassword,
  signOut as signOutFromSupabase,
  setNewsflashEventFavorite,
  loadNonMainstreamDashboard,
  updateCompetitorFilterKeyword,
  updateAccount,
  updateNonMainstreamSettings,
  updateNonMainstreamSource,
  updatePublisherChannel,
  updatePublisherSettings,
  updatePromptTemplateFeatureMode,
  updateSettings,
  updateWhaleWatchAddress,
  updateWhaleWatchHyperliquidAddress,
  updateWhaleWatchHyperliquidSettings,
  type Account,
  type AccountPatch,
  type Attempt,
  type NonMainstreamDashboardPayload,
  type NonMainstreamSettings,
  type NonMainstreamSource,
  type PublisherChannel,
  type PublisherSettings,
  type Settings,
  type TaskItem,
  type WhaleWatchActivity,
  type WhaleWatchAddress,
  type WhaleWatchAddressPatch,
  type WhaleWatchChainState,
  type WhaleWatchHyperliquidActivity,
  type WhaleWatchHyperliquidAddress,
  type WhaleWatchHyperliquidAddressPatch,
  type WhaleWatchHyperliquidSettings,
  type WhaleWatchHyperliquidState,
  type PromptTemplate,
  type PromptVersion,
  type CompetitorFilterKeyword,
  type NewsflashEventFilter,
  type NewsflashEventSourceItem,
  type NewsflashSourceSummary,
  type NewsflashEventSummary,
  type ConsoleAdmin,
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

const emptyPublisherSettings: PublisherSettings = {
  enabled: true,
  timezone: 'Asia/Shanghai',
  window_start_local: '00:01',
  window_end_local: '07:30',
  updated_at: null,
};

const publisherCategoryItems = [
  { key: 'policy_regulation', label: '政策法规', description: '命中后允许自动发布' },
  { key: 'people_view', label: '人物观点', description: '命中后允许自动发布' },
  { key: 'major_project_progress', label: '项目重大进展', description: '命中后允许自动发布' },
  { key: 'funding', label: '融资', description: '命中后允许自动发布' },
  { key: 'other', label: '其他', description: '只挂后台，不自动发布' },
] as const;

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

function fmtUnixMs(value: number | null | undefined): string {
  if (!value) return '-';
  return fmtTime(new Date(value).toISOString());
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

function visiblePromptTemplates(templates: PromptTemplate[]): PromptTemplate[] {
  return templates.filter((template) => template.template_key !== 'non_mainstream_media_writer');
}

type ConsoleAppProps = {
  adminEmail: string;
  onSignOut: () => Promise<void>;
  signingOut: boolean;
};

function AuthShell({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: ReactNode;
}) {
  return (
    <div className="authShell">
      <div className="authCard">
        <div className="brand">
          <div className="brandMark">O</div>
          <div>
            <strong>OdAIly</strong>
            <span>Console Access</span>
          </div>
        </div>
        <div className="authIntro">
          <h1>{title}</h1>
          <p>{subtitle}</p>
        </div>
        {children}
      </div>
    </div>
  );
}

export function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [admin, setAdmin] = useState<ConsoleAdmin | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [authChecking, setAuthChecking] = useState(true);
  const [authError, setAuthError] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [signingIn, setSigningIn] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const authRequestRef = useRef(0);
  const mountedRef = useRef(true);
  const sessionRef = useRef<Session | null>(null);
  const adminRef = useRef<ConsoleAdmin | null>(null);

  useEffect(() => {
    sessionRef.current = session;
  }, [session]);

  useEffect(() => {
    adminRef.current = admin;
  }, [admin]);

  useEffect(() => {
    mountedRef.current = true;

    const syncAccess = async (nextSession: Session | null) => {
      const requestId = authRequestRef.current + 1;
      authRequestRef.current = requestId;
      const currentSession = sessionRef.current;
      const currentAdmin = adminRef.current;
      const previousEmail = currentSession?.user.email?.toLowerCase() ?? null;
      const nextEmail = nextSession?.user.email?.toLowerCase() ?? null;
      const isSameAccount = previousEmail !== null && previousEmail === nextEmail;
      setSession(nextSession);

      if (!nextSession) {
        setAdmin(null);
        setAuthError('');
        setAuthChecking(false);
        setAuthReady(true);
        return;
      }

      if (!isSameAccount || !currentAdmin) {
        setAuthChecking(true);
        setAuthReady(false);
      }
      setAuthError('');
      try {
        const nextAdmin = await getCurrentConsoleAdmin();
        if (!mountedRef.current || authRequestRef.current !== requestId) {
          return;
        }
        setAdmin(nextAdmin);
        if (!nextAdmin) {
          setAuthError('当前邮箱没有控制台权限，请先运行 console-grant-admin 加入白名单。');
        }
      } catch (err) {
        if (!mountedRef.current || authRequestRef.current !== requestId) {
          return;
        }
        if (!isSameAccount || !currentAdmin) {
          setAdmin(null);
          setAuthError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!mountedRef.current || authRequestRef.current !== requestId) {
          return;
        }
        if (!isSameAccount || !currentAdmin) {
          setAuthChecking(false);
          setAuthReady(true);
        }
      }
    };

    getCurrentSession()
      .then((nextSession) => syncAccess(nextSession))
      .catch((err) => {
        if (!mountedRef.current) {
          return;
        }
        setSession(null);
        setAdmin(null);
        setAuthChecking(false);
        setAuthReady(true);
        setAuthError(err instanceof Error ? err.message : String(err));
      });

    const unsubscribe = onConsoleAuthStateChange((nextSession) => {
      void syncAccess(nextSession);
    });

    return () => {
      mountedRef.current = false;
      unsubscribe();
    };
  }, []);

  async function handleSignIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSigningIn(true);
    setAuthError('');
    try {
      await signInWithPassword(email, password);
      setPassword('');
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : String(err));
    } finally {
      if (mountedRef.current) {
        setSigningIn(false);
      }
    }
  }

  async function handleSignOut() {
    setSigningOut(true);
    try {
      await signOutFromSupabase();
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : String(err));
    } finally {
      if (mountedRef.current) {
        setSigningOut(false);
      }
    }
  }

  if (!authReady || authChecking) {
    return (
      <AuthShell title="验证控制台访问权限" subtitle="正在检查 Supabase 登录状态和管理员白名单。">
        <div className="emptyState">正在验证，请稍候。</div>
      </AuthShell>
    );
  }

  if (!session) {
    return (
      <AuthShell title="登录控制台" subtitle="使用 Supabase 邮箱密码登录。仅已加入管理员白名单的邮箱可以访问。">
        <form className="authForm" onSubmit={handleSignIn}>
          <label>
            <span>邮箱</span>
            <input
              type="email"
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="name@example.com"
            />
          </label>
          <label>
            <span>密码</span>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="请输入密码"
            />
          </label>
          {authError && <div className="notice error">{authError}</div>}
          <button className="primaryButton authSubmit" type="submit" disabled={signingIn}>
            登录
          </button>
        </form>
      </AuthShell>
    );
  }

  if (!admin) {
    return (
      <AuthShell title="已登录，但没有控制台权限" subtitle="当前账号已通过身份验证，但不在管理员白名单中。">
        <div className="authMeta">
          <strong>当前账号</strong>
          <span>{session.user.email ?? '-'}</span>
        </div>
        {authError && <div className="notice error">{authError}</div>}
        <button className="secondaryButton" type="button" onClick={() => void handleSignOut()} disabled={signingOut}>
          退出登录
        </button>
      </AuthShell>
    );
  }

  return <ConsoleApp adminEmail={admin.email} onSignOut={handleSignOut} signingOut={signingOut} />;
}

function ConsoleApp({ adminEmail, onSignOut, signingOut }: ConsoleAppProps) {
  const [settings, setSettings] = useState<Settings>(emptySettings);
  const [nonMainstreamSettings, setNonMainstreamSettings] = useState<NonMainstreamSettings>(emptyNonMainstreamSettings);
  const [publisherSettings, setPublisherSettings] = useState<PublisherSettings>(emptyPublisherSettings);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [nonMainstreamSources, setNonMainstreamSources] = useState<NonMainstreamSource[]>([]);
  const [publisherChannels, setPublisherChannels] = useState<PublisherChannel[]>([]);
  const [attempts, setAttempts] = useState<Attempt[]>([]);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [view, setView] = useState<
    'x' | 'non_mainstream' | 'ai_source' | 'publisher' | 'whale' | 'prompts' | 'competitor' | 'events' | 'favorites'
  >('x');
  const [loading, setLoading] = useState(true);
  const [loadingNonMainstream, setLoadingNonMainstream] = useState(true);
  const [loadingPublisher, setLoadingPublisher] = useState(true);
  const [loadingWhaleWatch, setLoadingWhaleWatch] = useState(true);
  const [savingSettings, setSavingSettings] = useState(false);
  const [savingNonMainstreamSettings, setSavingNonMainstreamSettings] = useState(false);
  const [savingPublisherSettings, setSavingPublisherSettings] = useState(false);
  const [newAccount, setNewAccount] = useState({
    username_or_url: '',
    display_name: '',
    write_name: '',
    interval_seconds: '',
  });
  const [promptTemplates, setPromptTemplates] = useState<PromptTemplate[]>([]);
  const [whaleAddresses, setWhaleAddresses] = useState<WhaleWatchAddress[]>([]);
  const [whaleStates, setWhaleStates] = useState<WhaleWatchChainState[]>([]);
  const [whaleActivities, setWhaleActivities] = useState<WhaleWatchActivity[]>([]);
  const [whaleHyperliquidSettings, setWhaleHyperliquidSettings] = useState<WhaleWatchHyperliquidSettings>({
    single_fill_min_notional_usd: 500000,
    aggregate_min_notional_usd: 1000000,
    aggregate_window_seconds: 600,
    updated_at: null,
  });
  const [whaleHyperliquidAddresses, setWhaleHyperliquidAddresses] = useState<WhaleWatchHyperliquidAddress[]>([]);
  const [whaleHyperliquidStates, setWhaleHyperliquidStates] = useState<WhaleWatchHyperliquidState[]>([]);
  const [whaleHyperliquidActivities, setWhaleHyperliquidActivities] = useState<WhaleWatchHyperliquidActivity[]>([]);
  const [newWhaleAddress, setNewWhaleAddress] = useState({ address: '', label: '' });
  const [savingWhaleAddress, setSavingWhaleAddress] = useState(false);
  const [newWhaleHyperliquidAddress, setNewWhaleHyperliquidAddress] = useState({ address: '', label: '' });
  const [savingWhaleHyperliquidAddress, setSavingWhaleHyperliquidAddress] = useState(false);
  const [savingWhaleHyperliquidSettings, setSavingWhaleHyperliquidSettings] = useState(false);
  const [selectedPromptKey, setSelectedPromptKey] = useState('');
  const [promptVersions, setPromptVersions] = useState<PromptVersion[]>([]);
  const [promptContent, setPromptContent] = useState('');
  const [promptNote, setPromptNote] = useState('');
  const [savingPrompt, setSavingPrompt] = useState(false);
  const [updatingPromptFeatureMode, setUpdatingPromptFeatureMode] = useState(false);
  const [deletingPromptVersionId, setDeletingPromptVersionId] = useState<number | null>(null);
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
    () => nonMainstreamSources.filter((source) => source.enabled && source.source_group !== 'ai_source').length,
    [nonMainstreamSources],
  );
  const enabledAiSourceCount = useMemo(
    () => nonMainstreamSources.filter((source) => source.enabled && source.source_group === 'ai_source').length,
    [nonMainstreamSources],
  );
  const externalMediaSources = useMemo(
    () => nonMainstreamSources.filter((source) => source.source_group !== 'ai_source'),
    [nonMainstreamSources],
  );
  const aiSources = useMemo(
    () => nonMainstreamSources.filter((source) => source.source_group === 'ai_source'),
    [nonMainstreamSources],
  );
  const enabledPublisherChannelCount = useMemo(
    () => publisherChannels.filter((channel) => channel.enabled).length,
    [publisherChannels],
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

  async function loadPublisherAll() {
    setError('');
    const [nextSettings, nextChannels] = await Promise.all([getPublisherSettings(), listPublisherChannels()]);
    setPublisherSettings(nextSettings);
    setPublisherChannels(nextChannels);
    setLoadingPublisher(false);
  }

  async function loadWhaleWatchAll() {
    setError('');
    const dashboard = await loadWhaleWatchDashboard();
    setWhaleAddresses(dashboard.addresses);
    setWhaleStates(dashboard.states);
    setWhaleActivities(dashboard.activities);
    setWhaleHyperliquidSettings(dashboard.hyperliquidSettings);
    setWhaleHyperliquidAddresses(dashboard.hyperliquidAddresses);
    setWhaleHyperliquidStates(dashboard.hyperliquidStates);
    setWhaleHyperliquidActivities(dashboard.hyperliquidActivities);
    setLoadingWhaleWatch(false);
  }

  async function loadPrompts(nextSelectedKey?: string) {
    setError('');
    const templates = visiblePromptTemplates(await listPromptTemplates());
    setPromptTemplates(templates);
    const requestedKey = nextSelectedKey || selectedPromptKey;
    const key = templates.some((template) => template.template_key === requestedKey)
      ? requestedKey || ''
      : templates[0]?.template_key || '';
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
    Promise.all([loadAll(), loadNonMainstreamAll(), loadPublisherAll(), loadWhaleWatchAll()]).catch((err: Error) => {
      setError(err.message);
      setLoading(false);
      setLoadingNonMainstream(false);
      setLoadingPublisher(false);
      setLoadingWhaleWatch(false);
    });
    const timer = window.setInterval(() => {
      loadAll().catch((err: Error) => setError(err.message));
      loadNonMainstreamAll().catch((err: Error) => setError(err.message));
      loadPublisherAll().catch((err: Error) => setError(err.message));
      loadWhaleWatchAll().catch((err: Error) => setError(err.message));
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
      setMessage('外媒设置已保存');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingNonMainstreamSettings(false);
    }
  }

  async function savePublisherSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSavingPublisherSettings(true);
    setError('');
    const form = new FormData(event.currentTarget);
    try {
      const updated = await updatePublisherSettings({
        enabled: form.get('enabled') === 'on',
        window_start_local: String(form.get('window_start_local') || publisherSettings.window_start_local),
        window_end_local: String(form.get('window_end_local') || publisherSettings.window_end_local),
      });
      setPublisherSettings(updated);
      setMessage('发布者配置已保存');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingPublisherSettings(false);
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
      write_name: newAccount.write_name || null,
      interval_seconds: newAccount.interval_seconds ? Number(newAccount.interval_seconds) : null,
      enabled: true,
    };
    try {
      await createAccount(body);
      setNewAccount({ username_or_url: '', display_name: '', write_name: '', interval_seconds: '' });
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

  async function togglePublisherChannel(channel: PublisherChannel, enabled: boolean) {
    setError('');
    try {
      await updatePublisherChannel(channel.channel_key, enabled);
      await loadPublisherAll();
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

  async function addWhaleAddress(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSavingWhaleAddress(true);
    setError('');
    try {
      await createWhaleWatchAddress({ ...newWhaleAddress, enabled: true });
      setNewWhaleAddress({ address: '', label: '' });
      setMessage('巨鲸地址已保存');
      await loadWhaleWatchAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingWhaleAddress(false);
    }
  }

  async function patchWhaleAddress(address: WhaleWatchAddress, patch: WhaleWatchAddressPatch) {
    setError('');
    try {
      await updateWhaleWatchAddress(address.id, patch);
      await loadWhaleWatchAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function removeWhaleAddress(address: WhaleWatchAddress) {
    setError('');
    try {
      await deleteWhaleWatchAddress(address.id);
      await loadWhaleWatchAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function addWhaleHyperliquidAddress(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSavingWhaleHyperliquidAddress(true);
    setError('');
    try {
      await createWhaleWatchHyperliquidAddress({ ...newWhaleHyperliquidAddress, enabled: true });
      setNewWhaleHyperliquidAddress({ address: '', label: '' });
      setMessage('Hyperliquid 地址已保存');
      await loadWhaleWatchAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingWhaleHyperliquidAddress(false);
    }
  }

  async function saveWhaleHyperliquidSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSavingWhaleHyperliquidSettings(true);
    setError('');
    const form = new FormData(event.currentTarget);
    try {
      const updated = await updateWhaleWatchHyperliquidSettings({
        single_fill_min_notional_usd: Number(form.get('single_fill_min_notional_usd')),
        aggregate_min_notional_usd: Number(form.get('aggregate_min_notional_usd')),
        aggregate_window_seconds: Number(form.get('aggregate_window_seconds')),
      });
      setWhaleHyperliquidSettings(updated);
      setMessage('Hyperliquid 设置已保存');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingWhaleHyperliquidSettings(false);
    }
  }

  async function patchWhaleHyperliquidAddress(
    address: WhaleWatchHyperliquidAddress,
    patch: WhaleWatchHyperliquidAddressPatch,
  ) {
    setError('');
    try {
      await updateWhaleWatchHyperliquidAddress(address.id, patch);
      await loadWhaleWatchAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function removeWhaleHyperliquidAddress(address: WhaleWatchHyperliquidAddress) {
    setError('');
    try {
      await deleteWhaleWatchHyperliquidAddress(address.id);
      await loadWhaleWatchAll();
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

  async function togglePromptFeatureMode(enabled: boolean) {
    if (!selectedPromptKey) return;
    setUpdatingPromptFeatureMode(true);
    setError('');
    try {
      await updatePromptTemplateFeatureMode(selectedPromptKey, enabled);
      setMessage(enabled ? '已开启特色模式' : '已关闭特色模式');
      await loadPrompts(selectedPromptKey);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setUpdatingPromptFeatureMode(false);
    }
  }

  async function removePromptVersion(version: PromptVersion) {
    const confirmed = window.confirm(
      version.id === promptTemplates.find((item) => item.template_key === version.template_key)?.active_version_id
        ? `确认删除当前生效的 v${version.version_number} 吗？删除后会自动切换到剩余最新版本。`
        : `确认删除 v${version.version_number} 吗？`,
    );
    if (!confirmed) return;

    setDeletingPromptVersionId(version.id);
    setError('');
    try {
      await deletePromptVersion(version.template_key, version.id);
      setMessage(`已删除 v${version.version_number}`);
      await loadPrompts(version.template_key);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeletingPromptVersionId(null);
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
        ? 'External Media'
      : view === 'ai_source'
        ? 'AI Sources'
        : view === 'publisher'
          ? 'Publisher'
        : view === 'whale'
          ? 'Whale Watch'
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
        ? '外媒抓取控制台'
      : view === 'ai_source'
        ? 'AI信源抓取控制台'
      : view === 'publisher'
        ? '发布者控制台'
      : view === 'whale'
        ? '巨鲸'
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
      : view === 'ai_source'
        ? `${enabledAiSourceCount} 个启用站点 · 默认 300s`
      : view === 'publisher'
        ? `${enabledPublisherChannelCount} 个启用渠道 · 北京时间 ${publisherSettings.window_start_local}-${publisherSettings.window_end_local}`
      : view === 'whale'
        ? `${whaleAddresses.filter((item) => item.enabled).length} 个链上地址 · ${whaleHyperliquidAddresses.filter((item) => item.enabled).length} 个 Hyperliquid 地址`
      : view === 'prompts'
        ? `${promptTemplates.length} 个模板 · ${selectedPromptKey || '-'}`
        : view === 'competitor'
          ? `${competitorKeywords.filter((item) => item.enabled).length} 个启用排除词`
          : `${events.length} 个事件`;
  const refreshCurrent = () =>
    view === 'x'
      ? loadAll()
      : view === 'non_mainstream' || view === 'ai_source'
        ? loadNonMainstreamAll()
      : view === 'publisher'
        ? loadPublisherAll()
      : view === 'whale'
        ? loadWhaleWatchAll()
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
          <button
            className={view === 'ai_source' ? 'navItem active' : 'navItem'}
            type="button"
            onClick={() => switchView('ai_source')}
          >
            <Bot size={18} /> AI信源
          </button>
          <button className={view === 'publisher' ? 'navItem active' : 'navItem'} type="button" onClick={() => switchView('publisher')}>
            <Send size={18} /> 发布者
          </button>
          <button className={view === 'prompts' ? 'navItem active' : 'navItem'} type="button" onClick={() => switchView('prompts')}>
            <FileText size={18} /> Prompt
          </button>
          <button className={view === 'whale' ? 'navItem active' : 'navItem'} type="button" onClick={() => switchView('whale')}>
            <Wallet size={18} /> 巨鲸
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
          <div className="topbarActions">
            <div className="userBadge">
              <strong>管理员</strong>
              <span>{adminEmail}</span>
            </div>
          <button
            className="iconButton"
            type="button"
            onClick={refreshCurrent}
            title="刷新"
          >
            <RefreshCcw size={18} />
          </button>
            <button className="secondaryButton" type="button" onClick={() => void onSignOut()} disabled={signingOut}>
              退出登录
            </button>
          </div>
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
              placeholder="写作名"
              value={newAccount.write_name}
              onChange={(event) => setNewAccount({ ...newAccount, write_name: event.target.value })}
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
            sources={externalMediaSources}
            loading={loadingNonMainstream}
            saving={savingNonMainstreamSettings}
            title="已接入外媒"
            emptyText="暂无已接入外媒，请先运行初始化命令。"
            onSettingChange={updateNonMainstreamSetting}
            onSave={saveNonMainstreamSettings}
            onToggleSource={patchNonMainstreamSource}
          />
        ) : view === 'ai_source' ? (
          <NonMainstreamPanel
            settings={nonMainstreamSettings}
            sources={aiSources}
            loading={loadingNonMainstream}
            saving={savingNonMainstreamSettings}
            title="已接入AI信源"
            emptyText="暂无已接入AI信源，请先运行初始化命令。"
            onSettingChange={updateNonMainstreamSetting}
            onSave={saveNonMainstreamSettings}
            onToggleSource={patchNonMainstreamSource}
          />
        ) : view === 'publisher' ? (
          <PublisherPanel
            settings={publisherSettings}
            channels={publisherChannels}
            loading={loadingPublisher}
            saving={savingPublisherSettings}
            categories={publisherCategoryItems}
            onSave={savePublisherSettings}
            onToggleChannel={togglePublisherChannel}
          />
        ) : view === 'whale' ? (
          <WhaleWatchPanel
            addresses={whaleAddresses}
            states={whaleStates}
            activities={whaleActivities}
            hyperliquidSettings={whaleHyperliquidSettings}
            hyperliquidAddresses={whaleHyperliquidAddresses}
            hyperliquidStates={whaleHyperliquidStates}
            hyperliquidActivities={whaleHyperliquidActivities}
            loading={loadingWhaleWatch}
            newAddress={newWhaleAddress}
            saving={savingWhaleAddress}
            newHyperliquidAddress={newWhaleHyperliquidAddress}
            savingHyperliquidAddress={savingWhaleHyperliquidAddress}
            savingHyperliquidSettings={savingWhaleHyperliquidSettings}
            onNewAddressChange={setNewWhaleAddress}
            onNewHyperliquidAddressChange={setNewWhaleHyperliquidAddress}
            onAdd={addWhaleAddress}
            onAddHyperliquid={addWhaleHyperliquidAddress}
            onSaveHyperliquidSettings={saveWhaleHyperliquidSettings}
            onPatch={patchWhaleAddress}
            onPatchHyperliquid={patchWhaleHyperliquidAddress}
            onDelete={removeWhaleAddress}
            onDeleteHyperliquid={removeWhaleHyperliquidAddress}
          />
        ) : view === 'prompts' ? (
          <PromptPanel
            templates={promptTemplates}
            selectedKey={selectedPromptKey}
            versions={promptVersions}
            content={promptContent}
            note={promptNote}
            saving={savingPrompt}
            featureModeSaving={updatingPromptFeatureMode}
            deletingVersionId={deletingPromptVersionId}
            onSelect={selectPrompt}
            onContentChange={setPromptContent}
            onNoteChange={setPromptNote}
            onSave={savePromptVersion}
            onToggleFeatureMode={togglePromptFeatureMode}
            onPublish={publishExistingVersion}
            onDeleteVersion={removePromptVersion}
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

function WhaleWatchPanel({
  addresses,
  states,
  activities,
  hyperliquidSettings,
  hyperliquidAddresses,
  hyperliquidStates,
  hyperliquidActivities,
  loading,
  newAddress,
  saving,
  newHyperliquidAddress,
  savingHyperliquidAddress,
  savingHyperliquidSettings,
  onNewAddressChange,
  onNewHyperliquidAddressChange,
  onAdd,
  onAddHyperliquid,
  onSaveHyperliquidSettings,
  onPatch,
  onPatchHyperliquid,
  onDelete,
  onDeleteHyperliquid,
}: {
  addresses: WhaleWatchAddress[];
  states: WhaleWatchChainState[];
  activities: WhaleWatchActivity[];
  hyperliquidSettings: WhaleWatchHyperliquidSettings;
  hyperliquidAddresses: WhaleWatchHyperliquidAddress[];
  hyperliquidStates: WhaleWatchHyperliquidState[];
  hyperliquidActivities: WhaleWatchHyperliquidActivity[];
  loading: boolean;
  newAddress: { address: string; label: string };
  saving: boolean;
  newHyperliquidAddress: { address: string; label: string };
  savingHyperliquidAddress: boolean;
  savingHyperliquidSettings: boolean;
  onNewAddressChange: (value: { address: string; label: string }) => void;
  onNewHyperliquidAddressChange: (value: { address: string; label: string }) => void;
  onAdd: (event: FormEvent<HTMLFormElement>) => Promise<void>;
  onAddHyperliquid: (event: FormEvent<HTMLFormElement>) => Promise<void>;
  onSaveHyperliquidSettings: (event: FormEvent<HTMLFormElement>) => Promise<void>;
  onPatch: (address: WhaleWatchAddress, patch: WhaleWatchAddressPatch) => Promise<void>;
  onPatchHyperliquid: (address: WhaleWatchHyperliquidAddress, patch: WhaleWatchHyperliquidAddressPatch) => Promise<void>;
  onDelete: (address: WhaleWatchAddress) => Promise<void>;
  onDeleteHyperliquid: (address: WhaleWatchHyperliquidAddress) => Promise<void>;
}) {
  const [activeTab, setActiveTab] = useState<'onchain' | 'hyperliquid'>('onchain');
  const statesByAddress = useMemo(() => {
    const grouped: Record<number, WhaleWatchChainState[]> = {};
    for (const state of states) {
      grouped[state.address_id] = [...(grouped[state.address_id] || []), state];
    }
    return grouped;
  }, [states]);
  const hyperliquidStatesByAddress = useMemo(() => {
    const grouped: Record<number, WhaleWatchHyperliquidState[]> = {};
    for (const state of hyperliquidStates) {
      grouped[state.address_id] = [...(grouped[state.address_id] || []), state];
    }
    return grouped;
  }, [hyperliquidStates]);

  return (
    <section className="whaleLayout">
      <div className="whaleTabs">
        <button className={activeTab === 'onchain' ? 'promptTab active' : 'promptTab'} type="button" onClick={() => setActiveTab('onchain')}>
          <strong>链上</strong>
          <span>地址活动监控</span>
        </button>
        <button
          className={activeTab === 'hyperliquid' ? 'promptTab active' : 'promptTab'}
          type="button"
          onClick={() => setActiveTab('hyperliquid')}
        >
          <strong>Hyperliquid</strong>
          <span>开仓 / 平仓监控</span>
        </button>
      </div>

      {activeTab === 'onchain' ? (
        <>
          <form className="whaleAddressForm" onSubmit={onAdd}>
            <input
              placeholder="0x..."
              value={newAddress.address}
              onChange={(event) => onNewAddressChange({ ...newAddress, address: event.target.value })}
            />
            <input
              placeholder="自定义标签，例如 Binance 充值地址"
              value={newAddress.label}
              onChange={(event) => onNewAddressChange({ ...newAddress, label: event.target.value })}
            />
            <button className="primaryButton" type="submit" disabled={saving}>
              <Plus size={17} /> 添加地址
            </button>
          </form>

          <div className="sectionHeader">
            <h2>链上地址</h2>
            <span>{loading ? '加载中...' : `${addresses.length} 个地址`}</span>
          </div>
          <div className="whaleAddressList">
            {!loading && addresses.length === 0 && <div className="emptyState">暂无链上地址，先添加一个地址。</div>}
            {addresses.map((address) => (
              <WhaleAddressRow
                key={address.id}
                address={address}
                states={statesByAddress[address.id] || []}
                onPatch={onPatch}
                onDelete={onDelete}
              />
            ))}
          </div>

          <div className="sectionHeader">
            <h2>最近播报</h2>
            <span>{activities.length} 条</span>
          </div>
          <div className="taskList">
            {activities.length === 0 && <div className="emptyState">暂无链上活动播报。</div>}
            {activities.map((activity) => (
              <article className="taskItem" key={activity.id}>
                <a href={activity.tx_url} target="_blank" rel="noreferrer">
                  {activity.activity_type === 'swap' ? 'Swap' : '转账'} · {activity.chain_key}
                </a>
                <p>{activity.telegram_text}</p>
                <div>
                  <span>{activity.summary}</span>
                  <time>{fmtTime(activity.created_at)}</time>
                </div>
              </article>
            ))}
          </div>
        </>
      ) : (
        <>
          <form className="settingsForm" onSubmit={onSaveHyperliquidSettings}>
            <label>
              <span>单次门槛</span>
              <input
                name="single_fill_min_notional_usd"
                type="number"
                min="0"
                step="1000"
                defaultValue={hyperliquidSettings.single_fill_min_notional_usd}
              />
            </label>
            <label>
              <span>聚合门槛</span>
              <input
                name="aggregate_min_notional_usd"
                type="number"
                min="0"
                step="1000"
                defaultValue={hyperliquidSettings.aggregate_min_notional_usd}
              />
            </label>
            <label>
              <span>聚合时长（秒）</span>
              <input
                name="aggregate_window_seconds"
                type="number"
                min="60"
                step="60"
                defaultValue={hyperliquidSettings.aggregate_window_seconds}
              />
            </label>
            <button className="primaryButton" type="submit" disabled={savingHyperliquidSettings}>
              <Save size={17} /> 保存设置
            </button>
          </form>

          <form className="whaleAddressForm" onSubmit={onAddHyperliquid}>
            <input
              placeholder="0x..."
              value={newHyperliquidAddress.address}
              onChange={(event) => onNewHyperliquidAddressChange({ ...newHyperliquidAddress, address: event.target.value })}
            />
            <input
              placeholder="自定义标签，例如 BTC 趋势账户"
              value={newHyperliquidAddress.label}
              onChange={(event) => onNewHyperliquidAddressChange({ ...newHyperliquidAddress, label: event.target.value })}
            />
            <button className="primaryButton" type="submit" disabled={savingHyperliquidAddress}>
              <Plus size={17} /> 添加地址
            </button>
          </form>

          <div className="sectionHeader">
            <h2>Hyperliquid 地址</h2>
            <span>{loading ? '加载中...' : `${hyperliquidAddresses.length} 个地址`}</span>
          </div>
          <div className="whaleAddressList">
            {!loading && hyperliquidAddresses.length === 0 && <div className="emptyState">暂无 Hyperliquid 地址，先添加一个地址。</div>}
            {hyperliquidAddresses.map((address) => (
              <WhaleHyperliquidAddressRow
                key={address.id}
                address={address}
                states={hyperliquidStatesByAddress[address.id] || []}
                onPatch={onPatchHyperliquid}
                onDelete={onDeleteHyperliquid}
              />
            ))}
          </div>

          <div className="sectionHeader">
            <h2>最近播报</h2>
            <span>{hyperliquidActivities.length} 条</span>
          </div>
          <div className="taskList">
            {hyperliquidActivities.length === 0 && <div className="emptyState">暂无 Hyperliquid 播报记录。</div>}
            {hyperliquidActivities.map((activity) => (
              <article className="taskItem" key={activity.id}>
                <a href={activity.tx_url} target="_blank" rel="noreferrer">
                  {activity.direction} · {activity.coin}
                </a>
                <p>{activity.telegram_text}</p>
                <div>
                  <span>{activity.summary}</span>
                  <time>{fmtTime(activity.created_at)}</time>
                </div>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function WhaleAddressRow({
  address,
  states,
  onPatch,
  onDelete,
}: {
  address: WhaleWatchAddress;
  states: WhaleWatchChainState[];
  onPatch: (address: WhaleWatchAddress, patch: WhaleWatchAddressPatch) => Promise<void>;
  onDelete: (address: WhaleWatchAddress) => Promise<void>;
}) {
  return (
    <article className={address.enabled ? 'accountRow' : 'accountRow disabled'}>
      <div className="accountIdentity">
        <div className="statusDot" />
        <div>
          <strong>{address.label}</strong>
          <span>{address.address}</span>
        </div>
      </div>
      <div className="whaleStateList">
        {states.length === 0 ? (
          <span>等待 worker 初始化</span>
        ) : (
          states.map((state) => (
            <span key={`${address.id}-${state.chain_key}`}>
              {state.chain_key}: {state.last_error || `区块 ${state.last_seen_block ?? '-'}`} · {fmtTime(state.last_success_at)}
            </span>
          ))
        )}
      </div>
      <div className="rowActions">
        <button
          className="iconButton"
          type="button"
          onClick={() => onPatch(address, { enabled: !address.enabled })}
          title={address.enabled ? '停用' : '启用'}
        >
          {address.enabled ? <Pause size={17} /> : <Zap size={17} />}
        </button>
        <button className="iconButton danger" type="button" onClick={() => onDelete(address)} title="删除">
          <Trash2 size={17} />
        </button>
      </div>
    </article>
  );
}

function WhaleHyperliquidAddressRow({
  address,
  states,
  onPatch,
  onDelete,
}: {
  address: WhaleWatchHyperliquidAddress;
  states: WhaleWatchHyperliquidState[];
  onPatch: (address: WhaleWatchHyperliquidAddress, patch: WhaleWatchHyperliquidAddressPatch) => Promise<void>;
  onDelete: (address: WhaleWatchHyperliquidAddress) => Promise<void>;
}) {
  return (
    <article className={address.enabled ? 'accountRow' : 'accountRow disabled'}>
      <div className="accountIdentity">
        <div className="statusDot" />
        <div>
          <strong>{address.label}</strong>
          <span>{address.address}</span>
        </div>
      </div>
      <div className="whaleStateList">
        {states.length === 0 ? (
          <span>等待 worker 初始化</span>
        ) : (
          states.map((state) => (
            <span key={`${address.id}-${state.last_seen_time ?? 0}`}>
              {state.last_error || `最近成交 ${fmtUnixMs(state.last_seen_time)}`} · {fmtTime(state.last_success_at)}
            </span>
          ))
        )}
      </div>
      <div className="rowActions">
        <button
          className="iconButton"
          type="button"
          onClick={() => onPatch(address, { enabled: !address.enabled })}
          title={address.enabled ? '停用' : '启用'}
        >
          {address.enabled ? <Pause size={17} /> : <Zap size={17} />}
        </button>
        <button className="iconButton danger" type="button" onClick={() => onDelete(address)} title="删除">
          <Trash2 size={17} />
        </button>
      </div>
    </article>
  );
}

function NonMainstreamPanel({
  settings,
  sources,
  loading,
  saving,
  title,
  emptyText,
  onSettingChange,
  onSave,
  onToggleSource,
}: {
  settings: NonMainstreamSettings;
  sources: NonMainstreamSource[];
  loading: boolean;
  saving: boolean;
  title: string;
  emptyText: string;
  onSettingChange: (key: 'global_interval_seconds' | 'jitter_seconds', value: string) => void;
  onSave: (event: FormEvent<HTMLFormElement>) => Promise<void>;
  onToggleSource: (source: NonMainstreamSource, enabled: boolean) => Promise<void>;
}) {
  const writeFlowSources = sources.filter((source) => source.pipeline_mode !== 'alert_only');
  const alertOnlySources = sources.filter((source) => source.pipeline_mode === 'alert_only');

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
          <h2>{title}</h2>
          <span>{loading ? '加载中' : `${sources.length} 个站点`}</span>
        </div>
        {sources.length === 0 && <div className="emptyState">{emptyText}</div>}
        {writeFlowSources.length > 0 && (
          <>
            <div className="sectionHeader">
              <h2>进入编写链路</h2>
              <span>{writeFlowSources.length} 个站点</span>
            </div>
            {writeFlowSources.map((source) => (
              <NonMainstreamSourceRow key={source.id} source={source} settings={settings} onToggleSource={onToggleSource} />
            ))}
          </>
        )}
        {alertOnlySources.length > 0 && (
          <>
            <div className="sectionHeader">
              <h2>标题提醒链路</h2>
              <span>{alertOnlySources.length} 个站点</span>
            </div>
            {alertOnlySources.map((source) => (
              <NonMainstreamSourceRow key={source.id} source={source} settings={settings} onToggleSource={onToggleSource} />
            ))}
          </>
        )}
      </div>
    </section>
  );
}

function PublisherPanel({
  settings,
  channels,
  loading,
  saving,
  categories,
  onSave,
  onToggleChannel,
}: {
  settings: PublisherSettings;
  channels: PublisherChannel[];
  loading: boolean;
  saving: boolean;
  categories: ReadonlyArray<{ key: string; label: string; description: string }>;
  onSave: (event: FormEvent<HTMLFormElement>) => Promise<void>;
  onToggleChannel: (channel: PublisherChannel, enabled: boolean) => Promise<void>;
}) {
  return (
    <section className="publisherLayout">
      <form
        key={`${settings.enabled}-${settings.window_start_local}-${settings.window_end_local}-${settings.updated_at ?? 'na'}`}
        className="publisherSettingsForm"
        onSubmit={onSave}
      >
        <label className="publisherToggle">
          <span>发布者总开关</span>
          <input name="enabled" type="checkbox" defaultChecked={settings.enabled} />
          <small>关闭后，所有稿件都只调用一次挂后台，不自动发布。</small>
        </label>
        <label>
          <span>时区</span>
          <input className="readonlyInput" value="Asia/Shanghai" readOnly />
        </label>
        <label>
          <span>开始时间</span>
          <input name="window_start_local" type="time" defaultValue={settings.window_start_local} />
        </label>
        <label>
          <span>结束时间</span>
          <input name="window_end_local" type="time" defaultValue={settings.window_end_local} />
        </label>
        <button className="primaryButton" type="submit" disabled={saving}>
          <Save size={17} /> 保存
        </button>
      </form>

      <div className="publisherSection">
        <div className="sectionHeader">
          <h2>自动发布渠道</h2>
          <span>{loading ? '加载中' : `${channels.length} 个渠道`}</span>
        </div>
        <p className="publisherHint">第一版实验口径只建议开启“外媒”，其余渠道默认挂后台。</p>
        <div className="publisherList">
          {channels.map((channel) => (
            <label className="publisherRow" key={channel.channel_key}>
              <div>
                <strong>{channel.display_name}</strong>
                <span>{channel.channel_key}</span>
              </div>
              <input
                type="checkbox"
                checked={channel.enabled}
                onChange={(event) => void onToggleChannel(channel, event.target.checked)}
              />
            </label>
          ))}
        </div>
      </div>

      <div className="publisherSection">
        <div className="sectionHeader">
          <h2>固定分类</h2>
          <span>只读</span>
        </div>
        <div className="publisherCategoryList">
          {categories.map((item) => (
            <div className="publisherCategoryRow" key={item.key}>
              <div>
                <strong>{item.label}</strong>
                <span>{item.key}</span>
              </div>
              <small>{item.description}</small>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function NonMainstreamSourceRow({
  source,
  settings,
  onToggleSource,
}: {
  source: NonMainstreamSource;
  settings: NonMainstreamSettings;
  onToggleSource: (source: NonMainstreamSource, enabled: boolean) => Promise<void>;
}) {
  const effectiveInterval = source.interval_seconds ?? settings.global_interval_seconds;

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
        <span>{source.seeded_at ? '已 seed' : '待 seed'} · {effectiveInterval}s</span>
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
  featureModeSaving,
  deletingVersionId,
  onSelect,
  onContentChange,
  onNoteChange,
  onSave,
  onToggleFeatureMode,
  onPublish,
  onDeleteVersion,
  onRefresh,
}: {
  templates: PromptTemplate[];
  selectedKey: string;
  versions: PromptVersion[];
  content: string;
  note: string;
  saving: boolean;
  featureModeSaving: boolean;
  deletingVersionId: number | null;
  onSelect: (templateKey: string) => Promise<void>;
  onContentChange: (value: string) => void;
  onNoteChange: (value: string) => void;
  onSave: (event: FormEvent<HTMLFormElement>) => Promise<void>;
  onToggleFeatureMode: (enabled: boolean) => Promise<void>;
  onPublish: (version: PromptVersion) => Promise<void>;
  onDeleteVersion: (version: PromptVersion) => Promise<void>;
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
        <label className="featureModeToggle">
          <input
            type="checkbox"
            checked={Boolean(selected?.feature_mode_enabled)}
            disabled={!selected || featureModeSaving}
            onChange={(event) => void onToggleFeatureMode(event.target.checked)}
          />
          <span>开启特色模式</span>
          <small>启用后，后端会在执行时自动在 Prompt 最前面加上“开启特色模式”。</small>
        </label>
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
            <div className="versionActions">
              <button className="iconButton" type="button" onClick={() => onPublish(version)} title="发布此版本">
                <Zap size={16} />
              </button>
              <button
                className="iconButton danger"
                type="button"
                disabled={deletingVersionId === version.id}
                onClick={() => onDeleteVersion(version)}
                title="删除此版本"
              >
                <Trash2 size={16} />
              </button>
            </div>
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
  const [writeName, setWriteName] = useState(account.write_name ?? '');
  const [interval, setIntervalValue] = useState(account.interval_seconds?.toString() ?? '');

  useEffect(() => {
    setDisplayName(account.display_name ?? '');
    setWriteName(account.write_name ?? '');
    setIntervalValue(account.interval_seconds?.toString() ?? '');
  }, [account.display_name, account.write_name, account.interval_seconds]);

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
        value={writeName}
        placeholder="写作名"
        onChange={(event) => setWriteName(event.target.value)}
        onBlur={() => onPatch(account, { write_name: writeName || null })}
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
