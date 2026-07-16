import { type ReactNode, FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import type { Session } from '@supabase/supabase-js';
import {
  Ban,
  Database,
  FileText,
  Globe2,
  Inbox,
  Layers3,
  Pause,
  Plus,
  Radio,
  RefreshCcw,
  Save,
  Send,
  Star,
  Timer,
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
  getJin10Settings,
  getCurrentConsoleAdmin,
  getCurrentSession,
  getPipelineTimingDashboard,
  getPublisherRuleConfig,
  getPublisherRuleConfigSnapshot,
  listCompetitorFilterKeywords,
  listNewsflashEventSources,
  listNewsflashEvents,
  listRecentTasksBySources,
  listRecentJin10Tasks,
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
  updateJin10Settings,
  savePublisherRuleConfig,
  updatePromptTemplateFeatureMode,
  updateSettings,
  updateWhaleWatchAddress,
  updateWhaleWatchHyperliquidAddress,
  updateWhaleWatchHyperliquidSettings,
  type Account,
  type AccountPatch,
  type Attempt,
  type NonMainstreamDashboardPayload,
  type Jin10Settings,
  type NonMainstreamSettings,
  type PipelineTimingDashboard,
  type PipelineTimingFlow,
  type PipelineTimingStage,
  type PipelineTimingWindow,
  type NonMainstreamSource,
  type PublisherRule,
  type PublisherRuleConfig,
  type PublisherRuleProfile,
  type PublisherRuleProfileKey,
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
  processingTaskSources,
} from './xCaptureStore';
import {
  workflowFilters,
  workflowGroups,
  workflowPrinciples,
  workflowSideNotes,
  type WorkflowFilterKey,
  type WorkflowGroup,
  type WorkflowNode,
} from './workflowCatalog';

type SourceManagementView = 'x' | 'non_mainstream' | 'ai_source' | 'mixed_source';

type ConsoleView =
  | SourceManagementView
  | 'tasks'
  | 'timing'
  | 'publisher'
  | 'workflow'
  | 'whale'
  | 'prompts'
  | 'competitor'
  | 'events'
  | 'favorites'
  | 'jin10';

function isSourceManagementView(view: ConsoleView): view is SourceManagementView {
  return view === 'x' || view === 'non_mainstream' || view === 'ai_source' || view === 'mixed_source';
}

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

const emptyJin10Settings: Jin10Settings = {
  enabled: false,
  interval_seconds: 60,
  endpoint_url: 'https://www.jin10.com/flash_newest.js',
  channel: null,
  request_headers: {},
  last_polled_at: null,
  last_success_at: null,
  last_error: null,
  updated_at: null,
};

const emptyPublisherRuleConfig: PublisherRuleConfig = {
  version: 1,
    regular: {
      key: 'regular',
      label: '常规',
      enabled: true,
      note: '适用于 X、Crypto信源、竞品、金十等非 AI 信源链路。',
      allow_rules: [],
      deny_rules: [],
    },
  ai_source: {
    key: 'ai_source',
    label: 'AI信源',
    enabled: false,
    note: '暂未启用。当前 AI 信源进入发布者时只挂后台，不自动放行。',
    allow_rules: [],
    deny_rules: [],
  },
  updated_at: null,
  updated_by: null,
};

const emptyPipelineTimingDashboard: PipelineTimingDashboard = {
  generated_at: null,
  windows: [],
  last_error: null,
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

function fmtSeconds(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '-';
  if (value >= 60) return `${(value / 60).toFixed(1)}m`;
  return `${value.toFixed(1)}s`;
}

function fmtPercent(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '-';
  return `${(value * 100).toFixed(1)}%`;
}

function fmtUnixMs(value: number | null | undefined): string {
  if (!value) return '-';
  return fmtTime(new Date(value).toISOString());
}

function trimText(value: string | null | undefined): string {
  return typeof value === 'string' ? value.trim() : '';
}

function firstLine(value: string | null | undefined): string {
  return trimText(value).split(/\r?\n/).map((line) => line.trim()).find(Boolean) || '';
}

function compactText(value: string | null | undefined): string {
  return trimText(value).replace(/\s+/g, ' ');
}

function taskHeadline(task: TaskItem): string {
  return trimText(task.pipeline?.final_title) || trimText(task.title) || firstLine(task.content) || task.source_item_id;
}

function taskSummary(task: TaskItem): string {
  const summary = compactText(task.pipeline?.final_content) || compactText(task.content);
  if (!summary) return '-';
  return summary.length > 160 ? `${summary.slice(0, 157)}...` : summary;
}

function taskMetadataValue(task: TaskItem, key: string): string {
  const raw = task.metadata && typeof task.metadata === 'object' ? task.metadata[key] : null;
  return typeof raw === 'string' ? raw.trim() : '';
}

function taskSourceLabel(task: TaskItem): { primary: string; secondary: string } {
  if (task.source === 'x') {
    const primary = taskMetadataValue(task, 'effective_author_name')
      || taskMetadataValue(task, 'author_display_name')
      || taskMetadataValue(task, 'author_username')
      || 'X 账号';
    const username = taskMetadataValue(task, 'author_username');
    return {
      primary,
      secondary: username ? `@${username.replace(/^@+/, '')}` : 'X',
    };
  }

  if (task.source === 'jin10') {
    return { primary: '金十', secondary: '网页端快讯接口' };
  }

  if (task.source === 'blockbeats') {
    return { primary: 'BlockBeats', secondary: '竞品快讯' };
  }

  if (task.source === 'panews') {
    return { primary: 'PANews', secondary: '竞品快讯' };
  }

  if (task.source === 'jinse') {
    return { primary: '金色财经', secondary: '竞品快讯' };
  }

  if (task.source === 'external_media_alert') {
    return { primary: taskMetadataValue(task, 'site_display_name') || 'Crypto信源', secondary: '标题提醒' };
  }

  if (task.source === 'ai_source_alert') {
    return { primary: taskMetadataValue(task, 'site_display_name') || 'AI信源', secondary: '标题提醒' };
  }

  const siteDisplayName = taskMetadataValue(task, 'site_display_name');
  if (siteDisplayName) {
    const target = taskMetadataValue(task, 'classified_target');
    return {
      primary: siteDisplayName,
      secondary: target ? `混合信源 -> ${target === 'ai' ? 'AI信源' : 'Crypto信源'}` : task.source,
    };
  }

  return { primary: task.source, secondary: task.source_url || '-' };
}

function taskStatusLabel(status: string): string {
  switch (status) {
    case 'pending':
      return '已入库待处理';
    case 'judged':
      return '判断完成';
    case 'deduped':
      return '已进入主链路';
    case 'domain_failed':
      return '领域判断失败';
    case 'searched':
      return '已完成查重';
    case 'duplicate':
      return '判定重复';
    case 'discarded':
      return '已丢弃';
    case 'published':
      return '已发布';
    case 'auto_published':
      return '已直发';
    case 'ready_review':
      return '挂后台';
    case 'notified':
      return '已提醒';
    case 'judge_failed':
      return '判断失败';
    case 'search_failed':
      return '查重失败';
    case 'write_failed':
      return '写作失败';
    case 'format_failed':
      return '格式化失败';
    case 'publish_failed':
      return '发布失败';
    case 'publisher_failed':
      return '发布阶段失败';
    case 'notify_failed':
      return '提醒失败';
    case 'legacy_skipped':
      return '旧任务已跳过';
    default:
      return status || '-';
  }
}

function isTaskStatusWarn(status: string): boolean {
  return status.endsWith('_failed') || status === 'duplicate' || status === 'discarded';
}

type TaskOverviewFilter = 'all' | 'x' | 'media' | 'alert' | 'competitor' | 'jin10' | 'publisher';

const taskOverviewFilters: { key: TaskOverviewFilter; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'x', label: 'X' },
  { key: 'media', label: '媒体信源' },
  { key: 'alert', label: '标题提醒' },
  { key: 'competitor', label: '竞品' },
  { key: 'jin10', label: '金十' },
  { key: 'publisher', label: '到发布者' },
];

function taskSourceBucket(task: TaskItem): TaskOverviewFilter {
  if (task.source === 'x') return 'x';
  if (task.source === 'jin10') return 'jin10';
  if (['blockbeats', 'panews', 'jinse'].includes(task.source)) return 'competitor';
  if (['external_media_alert', 'ai_source_alert'].includes(task.source)) return 'alert';
  if (['non_mainstream_media', 'ai_source', 'mainstream_media'].includes(task.source)) return 'media';
  return 'all';
}

function taskMatchesOverviewQuery(task: TaskItem, query: string): boolean {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;
  const source = taskSourceLabel(task);
  const fields = [
    String(task.id),
    task.source,
    task.source_item_id,
    task.source_url || '',
    task.title || '',
    task.content || '',
    task.pipeline?.final_title || '',
    task.pipeline?.final_content || '',
    taskStatusLabel(task.status),
    source.primary,
    source.secondary,
    taskPublisherExplanation(task),
  ];
  return fields.some((field) => field.toLowerCase().includes(normalized));
}

function filterTaskOverview(tasks: TaskItem[], filter: TaskOverviewFilter, query = ''): TaskItem[] {
  const filtered =
    filter === 'all'
      ? tasks
      : filter === 'publisher'
        ? tasks.filter((task) => Boolean(task.pipeline?.publisher_decided_at || task.pipeline?.publisher_decision))
        : tasks.filter((task) => taskSourceBucket(task) === filter);
  return filtered.filter((task) => taskMatchesOverviewQuery(task, query));
}

function taskPublisherExplanation(task: TaskItem): string {
  const pipeline = task.pipeline;
  if (!pipeline?.publisher_decided_at && !pipeline?.publisher_decision) return '';
  const output = pipeline.publisher_output && typeof pipeline.publisher_output === 'object' ? pipeline.publisher_output : {};
  const reason = typeof output.reason === 'string' ? output.reason.trim() : '';
  if (!reason) return '';
  const pieces = [`发布者理由：${reason}`];
  if (pipeline.last_error) {
    pieces.push(`错误：${pipeline.last_error}`);
  }
  return pieces.join(' ');
}

function TaskTable({ tasks, emptyText }: { tasks: TaskItem[]; emptyText: string }) {
  return (
    <div className="taskTable">
      <div className="taskTableHeader">
        <span>收集到什么</span>
        <span>来自哪里</span>
        <span>如何处理了</span>
      </div>
      {tasks.length === 0 && <div className="emptyState">{emptyText}</div>}
      {tasks.map((task) => {
        const source = taskSourceLabel(task);
        const statusLabel = taskStatusLabel(task.status);
        const publisherExplanation = taskPublisherExplanation(task);
        return (
          <div className="taskTableRow" key={task.id}>
            <div className="taskCell taskPrimaryCell">
              <a href={task.source_url ?? '#'} target="_blank" rel="noreferrer">
                {taskHeadline(task)}
              </a>
              <p>{taskSummary(task)}</p>
            </div>
            <div className="taskCell">
              <strong>{source.primary}</strong>
              <span>{source.secondary}</span>
            </div>
            <div className="taskCell">
              <span className={isTaskStatusWarn(task.status) ? 'statusPill warn' : 'statusPill'}>{statusLabel}</span>
              <span>{fmtTime(task.created_at)}</span>
              {publisherExplanation && <span className="publisherReason">{publisherExplanation}</span>}
            </div>
          </div>
        );
      })}
    </div>
  );
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

function normalizePublisherRulesForSave(config: PublisherRuleConfig): PublisherRuleConfig {
  const normalizeRule = (rule: PublisherRule) => ({
    ...rule,
    name: rule.name.trim(),
    description: rule.description.trim(),
    examples: rule.examples.map((example) => example.trim()).filter(Boolean),
  });
  const keepRule = (rule: PublisherRule) => rule.name.trim() || rule.description.trim() || rule.examples.some((example) => example.trim());
  const normalizeProfile = (profile: PublisherRuleProfile): PublisherRuleProfile => ({
    ...profile,
    allow_rules: profile.allow_rules.filter(keepRule).map(normalizeRule),
    deny_rules: profile.deny_rules.filter(keepRule).map(normalizeRule),
  });
  return {
    ...config,
    regular: normalizeProfile(config.regular),
    ai_source: normalizeProfile(config.ai_source),
  };
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
  const [publisherRules, setPublisherRules] = useState<PublisherRuleConfig>(emptyPublisherRuleConfig);
  const [publisherPromptPreview, setPublisherPromptPreview] = useState('');
  const [publisherLoadWarning, setPublisherLoadWarning] = useState('');
  const [pipelineTiming, setPipelineTiming] = useState<PipelineTimingDashboard>(emptyPipelineTimingDashboard);
  const [jin10Settings, setJin10Settings] = useState<Jin10Settings>(emptyJin10Settings);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [nonMainstreamSources, setNonMainstreamSources] = useState<NonMainstreamSource[]>([]);
  const [attempts, setAttempts] = useState<Attempt[]>([]);
  const [processingTasks, setProcessingTasks] = useState<TaskItem[]>([]);
  const [jin10Tasks, setJin10Tasks] = useState<TaskItem[]>([]);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [view, setView] = useState<ConsoleView>('x');
  const [lastSourceManagementView, setLastSourceManagementView] = useState<SourceManagementView>('x');
  const [loading, setLoading] = useState(true);
  const [loadingProcessingTasks, setLoadingProcessingTasks] = useState(true);
  const [loadingPipelineTiming, setLoadingPipelineTiming] = useState(true);
  const [loadingNonMainstream, setLoadingNonMainstream] = useState(true);
  const [loadingPublisher, setLoadingPublisher] = useState(true);
  const [loadingJin10, setLoadingJin10] = useState(true);
  const [loadingWhaleWatch, setLoadingWhaleWatch] = useState(true);
  const [savingSettings, setSavingSettings] = useState(false);
  const [savingNonMainstreamSettings, setSavingNonMainstreamSettings] = useState(false);
  const [savingPublisherSettings, setSavingPublisherSettings] = useState(false);
  const [savingJin10Settings, setSavingJin10Settings] = useState(false);
  const [newAccount, setNewAccount] = useState({
    username_or_url: '',
    display_name: '',
    write_name: '',
    interval_seconds: '',
    is_ai_source: false,
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
  const [taskOverviewFilter, setTaskOverviewFilter] = useState<TaskOverviewFilter>('all');
  const [taskOverviewQuery, setTaskOverviewQuery] = useState('');
  const loadedViewsRef = useRef<Set<string>>(new Set());

  const enabledCount = useMemo(() => accounts.filter((account) => account.enabled).length, [accounts]);
  const enabledNonMainstreamCount = useMemo(
    () => nonMainstreamSources.filter((source) => source.enabled && source.source_group === 'external_media').length,
    [nonMainstreamSources],
  );
  const enabledAiSourceCount = useMemo(
    () => nonMainstreamSources.filter((source) => source.enabled && source.source_group === 'ai_source').length,
    [nonMainstreamSources],
  );
  const enabledMixedSourceCount = useMemo(
    () => nonMainstreamSources.filter((source) => source.enabled && source.source_group === 'mixed_source').length,
    [nonMainstreamSources],
  );
  const externalMediaSources = useMemo(
    () => nonMainstreamSources.filter((source) => source.source_group === 'external_media'),
    [nonMainstreamSources],
  );
  const aiSources = useMemo(
    () => nonMainstreamSources.filter((source) => source.source_group === 'ai_source'),
    [nonMainstreamSources],
  );
  const mixedSources = useMemo(
    () => nonMainstreamSources.filter((source) => source.source_group === 'mixed_source'),
    [nonMainstreamSources],
  );
  const sourceManagementActive = isSourceManagementView(view);
  const sourceManagementView: SourceManagementView = isSourceManagementView(view) ? view : lastSourceManagementView;
  const sourceManagementLabel =
    sourceManagementView === 'x'
      ? 'X'
      : sourceManagementView === 'non_mainstream'
        ? 'Crypto信源'
        : sourceManagementView === 'ai_source'
          ? 'AI信源'
          : '混合信源';
  const sourceManagementSummary =
    sourceManagementView === 'x'
      ? `${enabledCount} 个启用 X 账号 · 全局 ${settings.global_interval_seconds}s · 抓取频率 / 写作名 / AI标签`
      : sourceManagementView === 'non_mainstream'
        ? `${enabledNonMainstreamCount} 个启用站点 · 全局 ${nonMainstreamSettings.global_interval_seconds}s · 全文 / 标题提醒`
        : sourceManagementView === 'ai_source'
          ? `${enabledAiSourceCount} 个启用站点 · 默认 300s · AI/半导体全文链路`
          : `${enabledMixedSourceCount} 个启用站点 · 全局 ${nonMainstreamSettings.global_interval_seconds}s · 轻量 AI 分流`;
  const enabledRegularRuleCount = useMemo(
    () => publisherRules.regular.allow_rules.filter((rule) => rule.enabled).length + publisherRules.regular.deny_rules.filter((rule) => rule.enabled).length,
    [publisherRules],
  );
  const visibleProcessingTasks = useMemo(
    () => filterTaskOverview(processingTasks, taskOverviewFilter, taskOverviewQuery),
    [processingTasks, taskOverviewFilter, taskOverviewQuery],
  );

  useEffect(() => {
    if (isSourceManagementView(view)) {
      setLastSourceManagementView(view);
    }
  }, [view]);

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
    setLoading(false);
  }

  async function loadNonMainstreamAll() {
    setError('');
    const dashboard: NonMainstreamDashboardPayload = await loadNonMainstreamDashboard();
    setNonMainstreamSettings(dashboard.settings);
    setNonMainstreamSources(dashboard.sources);
    setLoadingNonMainstream(false);
  }

  async function loadProcessingTasks() {
    setError('');
    setLoadingProcessingTasks(true);
    try {
      const nextTasks = await listRecentTasksBySources(processingTaskSources, 30);
      setProcessingTasks(nextTasks);
    } finally {
      setLoadingProcessingTasks(false);
    }
  }

  async function loadPipelineTiming() {
    setError('');
    setLoadingPipelineTiming(true);
    try {
      const dashboard = await getPipelineTimingDashboard();
      setPipelineTiming(dashboard);
    } finally {
      setLoadingPipelineTiming(false);
    }
  }

  async function loadPublisherAll() {
    setError('');
    setPublisherLoadWarning('');
    try {
      const payload = await getPublisherRuleConfig();
      setPublisherRules(payload.config);
      setPublisherPromptPreview(payload.prompt_text);
      setLoadingPublisher(false);
      return;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      try {
        const snapshot = await getPublisherRuleConfigSnapshot();
        if (snapshot) {
          setPublisherRules(snapshot.config);
          setPublisherPromptPreview(snapshot.prompt_text);
          setPublisherLoadWarning(`发布者接口加载失败，已回退显示 Supabase 快照：${message}`);
          setLoadingPublisher(false);
          return;
        }
      } catch (snapshotErr) {
        const snapshotMessage = snapshotErr instanceof Error ? snapshotErr.message : String(snapshotErr);
        setPublisherLoadWarning(`发布者接口与快照加载均失败：${message}；快照错误：${snapshotMessage}`);
        setLoadingPublisher(false);
        throw err;
      }
      setPublisherLoadWarning(`发布者接口加载失败，当前显示本地默认规则：${message}`);
      setLoadingPublisher(false);
    }
  }

  async function loadJin10All() {
    setError('');
    const [nextSettings, nextTasks] = await Promise.all([getJin10Settings(), listRecentJin10Tasks(30)]);
    setJin10Settings(nextSettings);
    setJin10Tasks(nextTasks);
    setLoadingJin10(false);
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
    const loadCurrentView = async () => {
      if (view === 'events' || view === 'favorites') {
        await loadEvents(eventFilter, view, eventPage);
        return;
      }
      if (loadedViewsRef.current.has(view)) {
        return;
      }
      loadedViewsRef.current.add(view);
      switch (view) {
        case 'x':
          await loadAll();
          return;
        case 'tasks':
          await loadProcessingTasks();
          return;
        case 'timing':
          await loadPipelineTiming();
          return;
        case 'non_mainstream':
        case 'ai_source':
        case 'mixed_source':
          await loadNonMainstreamAll();
          return;
        case 'publisher':
          await loadPublisherAll();
          return;
        case 'jin10':
          await loadJin10All();
          return;
        case 'whale':
          await loadWhaleWatchAll();
          return;
        case 'prompts':
          await loadPrompts();
          return;
        case 'competitor':
          await loadCompetitorKeywords();
          return;
        case 'workflow':
        default:
          return;
      }
    };

    loadCurrentView().catch((err: Error) => {
      setError(err.message);
      setLoading(false);
      setLoadingProcessingTasks(false);
      setLoadingPipelineTiming(false);
      setLoadingNonMainstream(false);
      setLoadingPublisher(false);
      setLoadingJin10(false);
      setLoadingWhaleWatch(false);
    });
  }, [view, eventFilter, eventPage]);

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
      setMessage('Crypto信源设置已保存');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingNonMainstreamSettings(false);
    }
  }

  async function savePublisherRules() {
    setSavingPublisherSettings(true);
    setError('');
    try {
      const updated = await savePublisherRuleConfig(normalizePublisherRulesForSave(publisherRules));
      setPublisherRules(updated.config);
      setPublisherPromptPreview(updated.prompt_text);
      setMessage('发布者配置已保存');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingPublisherSettings(false);
    }
  }

  async function saveJin10Settings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSavingJin10Settings(true);
    setError('');
    const form = new FormData(event.currentTarget);
    try {
      const headersText = String(form.get('request_headers') || '{}');
      const parsedHeaders = JSON.parse(headersText) as Record<string, string>;
      if (!parsedHeaders || typeof parsedHeaders !== 'object' || Array.isArray(parsedHeaders)) {
        throw new Error('headers 必须是 JSON 对象');
      }
      const updated = await updateJin10Settings({
        enabled: form.get('enabled') === 'on',
        interval_seconds: Number(form.get('interval_seconds')),
        endpoint_url: String(form.get('endpoint_url') || jin10Settings.endpoint_url).trim(),
        channel: String(form.get('channel') || '').trim() || null,
        request_headers: parsedHeaders,
      });
      setJin10Settings(updated);
      setMessage('金十配置已保存');
      await loadJin10All();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingJin10Settings(false);
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
      is_ai_source: newAccount.is_ai_source,
    };
    try {
      await createAccount(body);
      setNewAccount({ username_or_url: '', display_name: '', write_name: '', interval_seconds: '', is_ai_source: false });
      setMessage('X 账号已保存');
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

  async function addWhaleAddress(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (savingWhaleAddress) {
      return;
    }
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
    if (savingWhaleHyperliquidAddress) {
      return;
    }
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
    sourceManagementActive
      ? 'Source Management'
      : view === 'tasks'
        ? 'Cycle Monitor'
      : view === 'timing'
        ? 'Timing'
      : view === 'publisher'
        ? 'Publisher'
      : view === 'jin10'
        ? 'Jin10'
      : view === 'workflow'
        ? 'Workflow'
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
    sourceManagementActive
      ? '信源管理'
      : view === 'tasks'
        ? '信息周期监控'
      : view === 'timing'
        ? '耗时看板'
      : view === 'publisher'
        ? '发布者控制台'
      : view === 'jin10'
        ? '金十监控'
      : view === 'workflow'
        ? '流程展示'
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
    sourceManagementActive
      ? `当前子页：${sourceManagementLabel} · ${sourceManagementSummary}`
      : view === 'tasks'
        ? `${visibleProcessingTasks.length} / ${processingTasks.length} 条最近任务 · 发布者说明随任务展示`
      : view === 'timing'
        ? `本地快照${pipelineTiming.generated_at ? ` · ${fmtTime(pipelineTiming.generated_at)}` : '生成中'} · 每小时刷新`
      : view === 'publisher'
        ? `常规${publisherRules.regular.enabled ? '已开启' : '已关闭'} · ${enabledRegularRuleCount} 条启用规则 · AI信源暂未启用`
      : view === 'jin10'
        ? `${jin10Settings.enabled ? '已开启' : '已关闭'} · ${jin10Settings.interval_seconds}s`
      : view === 'workflow'
        ? '自动化主链路全景 · 分流、查重、发布后审核一图看清'
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
      : view === 'tasks'
        ? loadProcessingTasks()
      : view === 'timing'
        ? loadPipelineTiming()
      : view === 'non_mainstream' || view === 'ai_source' || view === 'mixed_source'
        ? loadNonMainstreamAll()
      : view === 'publisher'
        ? loadPublisherAll()
      : view === 'jin10'
        ? loadJin10All()
      : view === 'workflow'
        ? Promise.resolve()
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
          <button
            className={sourceManagementActive ? 'navItem active' : 'navItem'}
            type="button"
            onClick={() => switchView(sourceManagementActive ? view : lastSourceManagementView)}
          >
            <Globe2 size={18} /> 信源管理
          </button>
          <button className={view === 'tasks' ? 'navItem active' : 'navItem'} type="button" onClick={() => switchView('tasks')}>
            <Database size={18} /> 信息周期监控
          </button>
          <button className={view === 'timing' ? 'navItem active' : 'navItem'} type="button" onClick={() => switchView('timing')}>
            <Timer size={18} /> 耗时看板
          </button>
          <button className={view === 'publisher' ? 'navItem active' : 'navItem'} type="button" onClick={() => switchView('publisher')}>
            <Send size={18} /> 发布者
          </button>
          <button className={view === 'jin10' ? 'navItem active' : 'navItem'} type="button" onClick={() => switchView('jin10')}>
            <Radio size={18} /> 金十
          </button>
          <button className={view === 'workflow' ? 'navItem active' : 'navItem'} type="button" onClick={() => switchView('workflow')}>
            <Layers3 size={18} /> 流程展示
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

        {sourceManagementActive && (
          <section className="sourceManagementTabsGrid" aria-label="信源管理子页">
            {[
              {
                key: 'x' as const,
                label: 'X',
                summary: `${enabledCount} 个启用 X 账号 · 抓取频率 / 写作名 / AI标签`,
              },
              {
                key: 'non_mainstream' as const,
                label: 'Crypto信源',
                summary: `${enabledNonMainstreamCount} 个启用站点 · 全文 / 标题提醒`,
              },
              {
                key: 'ai_source' as const,
                label: 'AI信源',
                summary: `${enabledAiSourceCount} 个启用站点 · 默认 300s`,
              },
              {
                key: 'mixed_source' as const,
                label: '混合信源',
                summary: `${enabledMixedSourceCount} 个启用站点 · 轻量 AI 分流`,
              },
            ].map((item) => (
              <button
                className={sourceManagementView === item.key ? 'promptTab active' : 'promptTab'}
                type="button"
                key={item.key}
                onClick={() => switchView(item.key)}
              >
                <strong>{item.label}</strong>
                <span>{item.summary}</span>
              </button>
            ))}
          </section>
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
                <h2>X 规则</h2>
                <span>{loading ? '加载中' : `${accounts.length} 个 X 账号`}</span>
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
                <label className="inlineToggle">
                  <input
                    type="checkbox"
                    checked={newAccount.is_ai_source}
                    onChange={(event) => setNewAccount({ ...newAccount, is_ai_source: event.target.checked })}
                  />
                  <span>AI信源</span>
                </label>
                <button className="primaryButton" type="submit">
                  <Plus size={17} /> 添加
                </button>
              </form>

              <div className="accountList">
                {!loading && accounts.length === 0 && <div className="emptyState">暂无 X 账号，先添加一个账号。</div>}
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

          </>
        ) : view === 'tasks' ? (
          <TaskOverviewPanel
            tasks={visibleProcessingTasks}
            totalCount={processingTasks.length}
            loading={loadingProcessingTasks}
            filter={taskOverviewFilter}
            onFilterChange={setTaskOverviewFilter}
            query={taskOverviewQuery}
            onQueryChange={setTaskOverviewQuery}
          />
        ) : view === 'timing' ? (
          <PipelineTimingPanel dashboard={pipelineTiming} loading={loadingPipelineTiming} />
        ) : view === 'non_mainstream' ? (
          <NonMainstreamPanel
            settings={nonMainstreamSettings}
            sources={externalMediaSources}
            loading={loadingNonMainstream}
            saving={savingNonMainstreamSettings}
            title="已接入Crypto信源"
            emptyText="暂无已接入Crypto信源，请先运行初始化命令。"
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
        ) : view === 'mixed_source' ? (
          <NonMainstreamPanel
            settings={nonMainstreamSettings}
            sources={mixedSources}
            loading={loadingNonMainstream}
            saving={savingNonMainstreamSettings}
            title="已接入混合信源"
            emptyText="暂无已接入混合信源，请先运行初始化命令。"
            onSettingChange={updateNonMainstreamSetting}
            onSave={saveNonMainstreamSettings}
            onToggleSource={patchNonMainstreamSource}
          />
        ) : view === 'publisher' ? (
          <PublisherPanel
            config={publisherRules}
            promptPreview={publisherPromptPreview}
            loading={loadingPublisher}
            loadWarning={publisherLoadWarning}
            saving={savingPublisherSettings}
            onChange={setPublisherRules}
            onSave={savePublisherRules}
          />
        ) : view === 'jin10' ? (
          <Jin10Panel
            settings={jin10Settings}
            tasks={jin10Tasks}
            loading={loadingJin10}
            saving={savingJin10Settings}
            onSave={saveJin10Settings}
          />
        ) : view === 'workflow' ? (
          <WorkflowPanel />
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

function TaskOverviewPanel({
  tasks,
  totalCount,
  loading,
  filter,
  onFilterChange,
  query,
  onQueryChange,
}: {
  tasks: TaskItem[];
  totalCount: number;
  loading: boolean;
  filter: TaskOverviewFilter;
  onFilterChange: (filter: TaskOverviewFilter) => void;
  query: string;
  onQueryChange: (query: string) => void;
}) {
  const publisherCount = tasks.filter((task) => Boolean(task.pipeline?.publisher_decision || task.pipeline?.publisher_decided_at)).length;
  return (
    <section className="tasksOverviewLayout">
      <div className="filterBar">
        {taskOverviewFilters.map((item) => (
          <button
            className={filter === item.key ? 'filterButton active' : 'filterButton'}
            type="button"
            key={item.key}
            onClick={() => onFilterChange(item.key)}
          >
            {item.label}
          </button>
        ))}
        <input
          className="taskSearchInput"
          type="search"
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder="搜索标题、链接、来源"
        />
      </div>

      <section className="section">
        <div className="sectionHeader">
          <h2>最近信息</h2>
          <span>{loading ? '加载中' : `${tasks.length} 条显示 / ${totalCount} 条最近任务 · ${publisherCount} 条到发布者`}</span>
        </div>
        <TaskTable tasks={tasks} emptyText={loading ? '正在加载最近任务。' : '当前筛选没有任务。'} />
      </section>
    </section>
  );
}

function PipelineTimingPanel({ dashboard, loading }: { dashboard: PipelineTimingDashboard; loading: boolean }) {
  const [selectedHours, setSelectedHours] = useState(24);
  const [mode, setMode] = useState<'overview' | 'flows'>('overview');
  const selectedWindow =
    dashboard.windows.find((window) => window.hours === selectedHours)
    || dashboard.windows[0]
    || null;

  useEffect(() => {
    if (dashboard.windows.length > 0 && !dashboard.windows.some((window) => window.hours === selectedHours)) {
      setSelectedHours(dashboard.windows[0].hours);
    }
  }, [dashboard.windows, selectedHours]);

  if (loading) {
    return <div className="emptyState timingEmpty">耗时快照加载中。</div>;
  }

  if (!selectedWindow) {
    return (
      <section className="timingLayout">
        <div className="emptyState timingEmpty">
          本地耗时快照还在生成中。服务启动后会在首轮计算完成后显示数据。
        </div>
      </section>
    );
  }

  return (
    <section className="timingLayout">
      <div className="timingToolbar">
        <div className="workflowModeSwitch" aria-label="耗时窗口">
          {dashboard.windows.map((window) => (
            <button
              className={selectedWindow.hours === window.hours ? 'active' : ''}
              type="button"
              key={window.hours}
              onClick={() => setSelectedHours(window.hours)}
            >
              {window.label}
            </button>
          ))}
        </div>
        <div className="workflowModeSwitch" aria-label="耗时展示模式">
          <button className={mode === 'overview' ? 'active' : ''} type="button" onClick={() => setMode('overview')}>
            总览
          </button>
          <button className={mode === 'flows' ? 'active' : ''} type="button" onClick={() => setMode('flows')}>
            按流程拆分
          </button>
        </div>
      </div>

      {dashboard.last_error && <div className="notice error">最近一次快照刷新失败：{dashboard.last_error}</div>}

      <div className="timingSummaryGrid">
        <TimingSummaryCard label="样本" value={`${selectedWindow.overall.sample_count}`} detail="进入主写作链路" />
        <TimingSummaryCard
          label="完成率"
          value={fmtPercent(selectedWindow.overall.completion_rate)}
          detail={`${selectedWindow.overall.completed_count} 条到发布完成`}
        />
        <TimingSummaryCard label="均值" value={fmtSeconds(selectedWindow.overall.mean_seconds)} detail="已完成样本总耗时" />
        <TimingSummaryCard label="中位数" value={fmtSeconds(selectedWindow.overall.median_seconds)} detail="已完成样本总耗时" />
      </div>

      {mode === 'overview' ? (
        <>
          <section className="section">
            <div className="sectionHeader">
              <h2>阶段耗时</h2>
              <span>{selectedWindow.label} · 均值 / 中位数</span>
            </div>
            <TimingStageTable stages={selectedWindow.by_stage} />
          </section>
          <section className="section">
            <div className="sectionHeader">
              <h2>状态分布</h2>
              <span>{selectedWindow.status_breakdown.length} 类状态</span>
            </div>
            <div className="timingStatusGrid">
              {selectedWindow.status_breakdown.map((item) => (
                <div className="timingStatusPill" key={item.status}>
                  <strong>{taskStatusLabel(item.status)}</strong>
                  <span>{item.count}</span>
                </div>
              ))}
            </div>
          </section>
        </>
      ) : (
        <TimingFlowTable window={selectedWindow} />
      )}
    </section>
  );
}

function TimingSummaryCard({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="timingSummaryCard">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function TimingStageTable({ stages }: { stages: PipelineTimingStage[] }) {
  return (
    <div className="timingTable">
      <div className="timingTableHeader">
        <span>环节</span>
        <span>样本</span>
        <span>均值</span>
        <span>中位数</span>
      </div>
      {stages.map((stage) => (
        <div className="timingTableRow" key={stage.stage_key}>
          <strong>{stage.stage_name}</strong>
          <span>{stage.count}</span>
          <span>{fmtSeconds(stage.mean_seconds)}</span>
          <span>{fmtSeconds(stage.median_seconds)}</span>
        </div>
      ))}
    </div>
  );
}

function TimingFlowTable({ window }: { window: PipelineTimingWindow }) {
  return (
    <div className="timingFlowList">
      {window.by_flow.map((flow) => (
        <TimingFlowCard flow={flow} key={flow.flow_key} />
      ))}
      {window.by_flow.length === 0 && <div className="emptyState">当前窗口没有可展示的流程数据。</div>}
    </div>
  );
}

function TimingFlowCard({ flow }: { flow: PipelineTimingFlow }) {
  return (
    <article className="timingFlowCard">
      <div className="timingFlowHeader">
        <div>
          <h2>{flow.flow_name}</h2>
          <span>{flow.sample_count} 条样本 · {flow.completed_count} 条完成 · 完成率 {fmtPercent(flow.completion_rate)}</span>
        </div>
        <div className="timingFlowTotals">
          <strong>{fmtSeconds(flow.mean_seconds)}</strong>
          <span>均值</span>
          <strong>{fmtSeconds(flow.median_seconds)}</strong>
          <span>中位数</span>
        </div>
      </div>
      <TimingStageTable stages={flow.by_stage} />
    </article>
  );
}

function WorkflowPanel() {
  const [mode, setMode] = useState<'detail' | 'compact'>('detail');
  const [filter, setFilter] = useState<WorkflowFilterKey>('all');
  const [expanded, setExpanded] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(workflowGroups.map((group) => [group.id, true])),
  );

  const visibleGroups = workflowGroups.filter((group) => filter === 'all' || group.filterTags.includes(filter));
  const aiNodeCount = workflowGroups.reduce((count, group) => count + group.nodes.filter((node) => node.usesAi).length, 0);
  const modelSet = new Set(
    workflowGroups
      .flatMap((group) => group.nodes)
      .filter((node) => node.usesAi && node.model && !node.model.includes('规则 + 向量'))
      .map((node) => node.model),
  );

  function toggleExpanded(groupId: string) {
    setExpanded((current) => ({ ...current, [groupId]: !current[groupId] }));
  }

  return (
    <section className="workflowLayout">
      <div className="workflowHero">
        <div className="workflowHeroText">
          <span className="workflowEyebrow">OdAIly workflow atlas</span>
          <h2>一条信息从来源到发布，再到发布后复核，会经过哪些闸门？</h2>
          <p>
            这里把当前自动化主链路拆成可读的节点：谁负责收集、怎么按管理标签分流、搜索者如何查重、哪里用模型判断和写作定稿，以及发布后怎样进入审核者异步质检。
          </p>
        </div>
        <div className="workflowHeroStats">
          <div>
            <strong>{workflowGroups.length}</strong>
            <span>条主工作流</span>
          </div>
          <div>
            <strong>{aiNodeCount}</strong>
            <span>个 AI 节点</span>
          </div>
          <div>
            <strong>{modelSet.size}</strong>
            <span>类固定模型口径</span>
          </div>
        </div>
      </div>

      <div className="workflowToolbar">
        <div className="workflowFilters" aria-label="工作流筛选">
          {workflowFilters.map((item) => (
            <button
              className={filter === item.key ? 'workflowFilterButton active' : 'workflowFilterButton'}
              type="button"
              key={item.key}
              onClick={() => setFilter(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="workflowModeSwitch" aria-label="展示模式">
          <button className={mode === 'detail' ? 'active' : ''} type="button" onClick={() => setMode('detail')}>
            全景详细
          </button>
          <button className={mode === 'compact' ? 'active' : ''} type="button" onClick={() => setMode('compact')}>
            极简流向
          </button>
        </div>
      </div>

      {visibleGroups.length === 0 ? (
        <div className="emptyState">当前筛选没有匹配的工作流。</div>
      ) : mode === 'detail' ? (
        <div className="workflowDetailList">
          {visibleGroups.map((group) => (
            <WorkflowGroupCard
              expanded={expanded[group.id] ?? true}
              group={group}
              key={group.id}
              onToggle={() => toggleExpanded(group.id)}
            />
          ))}
        </div>
      ) : (
        <div className="workflowCompactGrid">
          {visibleGroups.map((group) => (
            <WorkflowCompactCard group={group} key={group.id} />
          ))}
        </div>
      )}

      <div className="workflowPrinciples">
        <div className="sectionHeader">
          <h2>统一规律</h2>
          <span>跨链路不变的系统约定</span>
        </div>
        <div className="workflowPrincipleGrid">
          {workflowPrinciples.map((item) => (
            <article className="workflowPrincipleCard" key={item.title}>
              <strong>{item.title}</strong>
              <p>{item.body}</p>
            </article>
          ))}
        </div>
      </div>

      <div className="workflowSideNotes">
        <div className="sectionHeader">
          <h2>系统旁路</h2>
          <span>不放入主画布，但与生产运转相关</span>
        </div>
        <div className="workflowSideNoteGrid">
          {workflowSideNotes.map((item) => (
            <article className="workflowSideNote" key={item.title}>
              <strong>{item.title}</strong>
              <p>{item.body}</p>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

function WorkflowGroupCard({
  group,
  expanded,
  onToggle,
}: {
  group: WorkflowGroup;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <article className="workflowCard">
      <header className="workflowCardHeader">
        <div>
          <div className="workflowCardTitle">
            <span>{group.badge}</span>
            <h3>{group.title}</h3>
          </div>
          <p>{group.summary}</p>
        </div>
        <button className="secondaryButton" type="button" onClick={onToggle}>
          {expanded ? '收起说明' : '展开说明'}
        </button>
      </header>

      <div className="workflowMetaStrip">
        <span>入口：{group.sourceEntry}</span>
        <span>状态：{group.defaultStatus}</span>
      </div>

      {expanded && (
        <div className="workflowRoutingNote">
          <strong>分流口径</strong>
          <p>{group.routingNote}</p>
        </div>
      )}

      {group.routes && expanded && (
        <div className="workflowRoutes">
          {group.routes.map((route) => (
            <div className="workflowRoute" key={route.label}>
              <strong>{route.label}</strong>
              <div>
                {route.steps.map((step, index) => (
                  <span key={`${route.label}-${step}`}>
                    {index > 0 && <b>→</b>}
                    {step}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className={expanded ? 'workflowRail' : 'workflowRail compact'}>
        {group.nodes.map((node, index) => (
          <WorkflowNodeCard expanded={expanded} isLast={index === group.nodes.length - 1} key={node.id} node={node} />
        ))}
      </div>
    </article>
  );
}

function WorkflowNodeCard({
  node,
  expanded,
  isLast,
}: {
  node: WorkflowNode;
  expanded: boolean;
  isLast: boolean;
}) {
  return (
    <div className="workflowNodeWrap">
      <div className={`workflowNode ${node.kind}${node.usesAi ? ' usesAi' : ''}`}>
        <div className="workflowNodeTop">
          <span className="workflowKind">{nodeKindLabel(node.kind)}</span>
          {node.usesAi && <span className="workflowAiDot">AI</span>}
        </div>
        <strong>{node.name}</strong>
        {expanded && <p>{node.description}</p>}
        {node.usesAi && (
          <div className="workflowModelCard">
            <span>{node.model ?? '模型配置未固定'}</span>
            {node.reasoningEffort && <small>思考等级：{node.reasoningEffort}</small>}
          </div>
        )}
        {expanded && (
          <div className="workflowOutput">
            <span>产出</span>
            <p>{node.output}</p>
          </div>
        )}
      </div>
      {!isLast && <div className="workflowConnector">→</div>}
    </div>
  );
}

function WorkflowCompactCard({ group }: { group: WorkflowGroup }) {
  return (
    <article className="workflowMiniMap">
      <div className="workflowMiniMapHeader">
        <span>{group.badge}</span>
        <h3>{group.title}</h3>
      </div>
      <div className="workflowMiniRail">
        {group.nodes.map((node, index) => (
          <span className={node.usesAi ? 'workflowMiniNode ai' : 'workflowMiniNode'} key={node.id}>
            {index > 0 && <b>→</b>}
            <em>{node.compactLabel}</em>
            {node.usesAi && node.model && !node.model.includes('规则 + 向量') && <small>{node.model}{node.reasoningEffort ? ` / ${node.reasoningEffort}` : ''}</small>}
          </span>
        ))}
      </div>
      {group.routes && (
        <div className="workflowMiniRoutes">
          {group.routes.map((route) => (
            <span key={route.label}>
              <strong>{route.label}</strong>
              {route.steps.join(' -> ')}
            </span>
          ))}
        </div>
      )}
    </article>
  );
}

function nodeKindLabel(kind: WorkflowNode['kind']): string {
  const labels: Record<WorkflowNode['kind'], string> = {
    collector: '收集',
    dedupe: '查重',
    judge: '判断',
    writer: '写作',
    formatter: '定稿',
    publisher: '发布',
    notify: '提醒',
    split: '分流',
  };
  return labels[kind];
}

function Jin10Panel({
  settings,
  tasks,
  loading,
  saving,
  onSave,
}: {
  settings: Jin10Settings;
  tasks: TaskItem[];
  loading: boolean;
  saving: boolean;
  onSave: (event: FormEvent<HTMLFormElement>) => Promise<void>;
}) {
  return (
    <section className="jin10Layout">
      <form className="settingsForm jin10SettingsForm" onSubmit={onSave} key={settings.updated_at ?? 'initial'}>
        <label className="inlineToggle">
          <input name="enabled" type="checkbox" defaultChecked={settings.enabled} />
          <span>{settings.enabled ? '开启' : '关闭'}</span>
        </label>
        <label>
          <span>频率秒</span>
          <input name="interval_seconds" type="number" min="10" max="3600" defaultValue={settings.interval_seconds} />
        </label>
        <label>
          <span>Channel</span>
          <input name="channel" placeholder="默认空" defaultValue={settings.channel ?? ''} />
        </label>
        <label className="wideField">
          <span>Endpoint</span>
          <input name="endpoint_url" defaultValue={settings.endpoint_url} />
        </label>
        <label className="wideField">
          <span>Headers</span>
          <textarea name="request_headers" rows={7} defaultValue={JSON.stringify(settings.request_headers || {}, null, 2)} />
        </label>
        <button className="primaryButton" type="submit" disabled={saving}>
          <Save size={17} /> 保存
        </button>
      </form>

      <section className="section">
        <div className="sectionHeader">
          <h2>运行状态</h2>
          <span>{loading ? '加载中' : settings.enabled ? '已开启' : '已关闭'}</span>
        </div>
        <div className="statusGrid">
          <div>
            <strong>最近轮询</strong>
            <span>{fmtTime(settings.last_polled_at)}</span>
          </div>
          <div>
            <strong>最近成功</strong>
            <span>{fmtTime(settings.last_success_at)}</span>
          </div>
          <div>
            <strong>最近错误</strong>
            <span>{settings.last_error || '-'}</span>
          </div>
        </div>
      </section>

      <section className="section">
        <div className="sectionHeader">
          <h2>最近入库</h2>
          <span>{tasks.length} 条</span>
        </div>
        <TaskTable tasks={tasks} emptyText="暂无金十入库任务。" />
      </section>
    </section>
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
  config,
  promptPreview,
  loading,
  loadWarning,
  saving,
  onChange,
  onSave,
}: {
  config: PublisherRuleConfig;
  promptPreview: string;
  loading: boolean;
  loadWarning: string;
  saving: boolean;
  onChange: (config: PublisherRuleConfig) => void;
  onSave: () => Promise<void>;
}) {
  function patchProfile(profileKey: PublisherRuleProfileKey, patch: Partial<PublisherRuleProfile>) {
    onChange({ ...config, [profileKey]: { ...config[profileKey], ...patch } });
  }

  function patchRule(profileKey: PublisherRuleProfileKey, kind: 'allow_rules' | 'deny_rules', ruleId: string, patch: Partial<PublisherRule>) {
    const profile = config[profileKey];
    patchProfile(profileKey, {
      [kind]: profile[kind].map((rule) => (rule.id === ruleId ? { ...rule, ...patch } : rule)),
    } as Partial<PublisherRuleProfile>);
  }

  function addRule(profileKey: PublisherRuleProfileKey, kind: 'allow_rules' | 'deny_rules') {
    const profile = config[profileKey];
    const rule: PublisherRule = {
      id: `${kind}-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      name: '',
      description: '',
      enabled: false,
      examples: [],
    };
    patchProfile(profileKey, { [kind]: [...profile[kind], rule] } as Partial<PublisherRuleProfile>);
  }

  function removeRule(profileKey: PublisherRuleProfileKey, kind: 'allow_rules' | 'deny_rules', ruleId: string) {
    const profile = config[profileKey];
    patchProfile(profileKey, { [kind]: profile[kind].filter((rule) => rule.id !== ruleId) } as Partial<PublisherRuleProfile>);
  }

  function updateExample(profileKey: PublisherRuleProfileKey, kind: 'allow_rules' | 'deny_rules', rule: PublisherRule, index: number, value: string) {
    patchRule(profileKey, kind, rule.id, {
      examples: rule.examples.map((example, currentIndex) => (currentIndex === index ? value : example)),
    });
  }

  function addExample(profileKey: PublisherRuleProfileKey, kind: 'allow_rules' | 'deny_rules', rule: PublisherRule) {
    patchRule(profileKey, kind, rule.id, { examples: [...rule.examples, ''] });
  }

  function removeExample(profileKey: PublisherRuleProfileKey, kind: 'allow_rules' | 'deny_rules', rule: PublisherRule, index: number) {
    patchRule(profileKey, kind, rule.id, { examples: rule.examples.filter((_, currentIndex) => currentIndex !== index) });
  }

  return (
    <section className="publisherLayout">
      <div className="publisherToolbar">
        <div>
          <strong>发布者规则</strong>
          <span>
            排除优先；未命中启用通过规则时默认挂后台。{config.updated_at ? `上次保存：${fmtTime(config.updated_at)}` : ''}
          </span>
        </div>
        <button className="primaryButton" type="button" disabled={saving || loading} onClick={() => void onSave()}>
          <Save size={17} /> 保存规则
        </button>
      </div>

      {loading ? <div className="emptyState">发布者规则加载中。</div> : null}
      {!loading && loadWarning ? <div className="emptyState compact">{loadWarning}</div> : null}

      {!loading && (
        <>
          <PublisherProfileEditor
            profile={config.regular}
            disabled={false}
            onPatch={(patch) => patchProfile('regular', patch)}
            onPatchRule={(kind, ruleId, patch) => patchRule('regular', kind, ruleId, patch)}
            onAddRule={(kind) => addRule('regular', kind)}
            onRemoveRule={(kind, ruleId) => removeRule('regular', kind, ruleId)}
            onAddExample={(kind, rule) => addExample('regular', kind, rule)}
            onUpdateExample={(kind, rule, index, value) => updateExample('regular', kind, rule, index, value)}
            onRemoveExample={(kind, rule, index) => removeExample('regular', kind, rule, index)}
          />

          <PublisherProfileEditor
            profile={config.ai_source}
            disabled
            badge="暂未启用"
            onPatch={(patch) => patchProfile('ai_source', patch)}
            onPatchRule={(kind, ruleId, patch) => patchRule('ai_source', kind, ruleId, patch)}
            onAddRule={(kind) => addRule('ai_source', kind)}
            onRemoveRule={(kind, ruleId) => removeRule('ai_source', kind, ruleId)}
            onAddExample={(kind, rule) => addExample('ai_source', kind, rule)}
            onUpdateExample={(kind, rule, index, value) => updateExample('ai_source', kind, rule, index, value)}
            onRemoveExample={(kind, rule, index) => removeExample('ai_source', kind, rule, index)}
          />

          {promptPreview && (
            <div className="publisherSection">
              <div className="sectionHeader">
                <h2>服务器拼接 Prompt 预览</h2>
                <span>常规</span>
              </div>
              <pre className="publisherPromptPreview">{promptPreview}</pre>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function PublisherProfileEditor({
  profile,
  disabled,
  badge,
  onPatch,
  onPatchRule,
  onAddRule,
  onRemoveRule,
  onAddExample,
  onUpdateExample,
  onRemoveExample,
}: {
  profile: PublisherRuleProfile;
  disabled: boolean;
  badge?: string;
  onPatch: (patch: Partial<PublisherRuleProfile>) => void;
  onPatchRule: (kind: 'allow_rules' | 'deny_rules', ruleId: string, patch: Partial<PublisherRule>) => void;
  onAddRule: (kind: 'allow_rules' | 'deny_rules') => void;
  onRemoveRule: (kind: 'allow_rules' | 'deny_rules', ruleId: string) => void;
  onAddExample: (kind: 'allow_rules' | 'deny_rules', rule: PublisherRule) => void;
  onUpdateExample: (kind: 'allow_rules' | 'deny_rules', rule: PublisherRule, index: number, value: string) => void;
  onRemoveExample: (kind: 'allow_rules' | 'deny_rules', rule: PublisherRule, index: number) => void;
}) {
  return (
    <div className={disabled ? 'publisherProfile disabled' : 'publisherProfile'}>
      <div className="publisherProfileHeader">
        <div>
          <h2>
            {profile.label}
            {badge && <span>{badge}</span>}
          </h2>
          <p>{profile.note}</p>
        </div>
        <label className="publisherToggle">
          <span>{profile.enabled ? '已启用' : '已停用'}</span>
          <input type="checkbox" checked={profile.enabled} disabled={disabled} onChange={(event) => onPatch({ enabled: event.target.checked })} />
        </label>
      </div>

      <div className="publisherRuleColumns">
        <PublisherRuleGroup
          title="通过规则"
          hint="命中任意启用通过规则，且未命中排除规则时，允许自动发布。"
          kind="allow_rules"
          rules={profile.allow_rules}
          disabled={disabled}
          onPatchRule={onPatchRule}
          onAddRule={onAddRule}
          onRemoveRule={onRemoveRule}
          onAddExample={onAddExample}
          onUpdateExample={onUpdateExample}
          onRemoveExample={onRemoveExample}
        />
        <PublisherRuleGroup
          title="排除规则"
          hint="排除优先。命中任意启用排除规则时直接挂后台。"
          kind="deny_rules"
          rules={profile.deny_rules}
          disabled={disabled}
          onPatchRule={onPatchRule}
          onAddRule={onAddRule}
          onRemoveRule={onRemoveRule}
          onAddExample={onAddExample}
          onUpdateExample={onUpdateExample}
          onRemoveExample={onRemoveExample}
        />
      </div>
    </div>
  );
}

function PublisherRuleGroup({
  title,
  hint,
  kind,
  rules,
  disabled,
  onPatchRule,
  onAddRule,
  onRemoveRule,
  onAddExample,
  onUpdateExample,
  onRemoveExample,
}: {
  title: string;
  hint: string;
  kind: 'allow_rules' | 'deny_rules';
  rules: PublisherRule[];
  disabled: boolean;
  onPatchRule: (kind: 'allow_rules' | 'deny_rules', ruleId: string, patch: Partial<PublisherRule>) => void;
  onAddRule: (kind: 'allow_rules' | 'deny_rules') => void;
  onRemoveRule: (kind: 'allow_rules' | 'deny_rules', ruleId: string) => void;
  onAddExample: (kind: 'allow_rules' | 'deny_rules', rule: PublisherRule) => void;
  onUpdateExample: (kind: 'allow_rules' | 'deny_rules', rule: PublisherRule, index: number, value: string) => void;
  onRemoveExample: (kind: 'allow_rules' | 'deny_rules', rule: PublisherRule, index: number) => void;
}) {
  return (
    <div className="publisherSection">
        <div className="sectionHeader">
          <h2>{title}</h2>
          <button className="secondaryButton compact" type="button" disabled={disabled} onClick={() => onAddRule(kind)}>
            <Plus size={15} /> 增加规则
          </button>
        </div>
        <p className="publisherHint">{hint}</p>
        <div className="publisherRuleList">
          {rules.map((rule) => (
            <div className="publisherRuleCard" key={rule.id}>
              <div className="publisherRuleTop">
                <label>
                  <span>规则名</span>
                  <input disabled={disabled} value={rule.name} onChange={(event) => onPatchRule(kind, rule.id, { name: event.target.value })} />
                </label>
                <label className="publisherToggle inline">
                  <span>{rule.enabled ? '启用' : '停用'}</span>
                  <input
                    type="checkbox"
                    disabled={disabled}
                    checked={rule.enabled}
                    onChange={(event) => onPatchRule(kind, rule.id, { enabled: event.target.checked })}
                  />
                </label>
                <button className="iconButton" type="button" disabled={disabled} onClick={() => onRemoveRule(kind, rule.id)}>
                  <Trash2 size={16} />
                </button>
              </div>
              <label>
                <span>规则说明</span>
                <textarea
                  disabled={disabled}
                  rows={3}
                  value={rule.description}
                  onChange={(event) => onPatchRule(kind, rule.id, { description: event.target.value })}
                />
              </label>
              <div className="publisherExamples">
                <div className="publisherExamplesHeader">
                  <span>案例列表</span>
                  <button className="secondaryButton compact" type="button" disabled={disabled} onClick={() => onAddExample(kind, rule)}>
                    <Plus size={14} /> 增加案例
                  </button>
                </div>
                {(rule.examples.length > 0 ? rule.examples : ['']).map((example, index) => (
                  <div className="publisherExampleRow" key={`${rule.id}-${index}`}>
                    <input
                      disabled={disabled}
                      value={example}
                      placeholder="例如：某项目完成 1000 万美元融资"
                      onChange={(event) => {
                        if (rule.examples.length === 0) {
                          onPatchRule(kind, rule.id, { examples: [event.target.value] });
                        } else {
                          onUpdateExample(kind, rule, index, event.target.value);
                        }
                      }}
                    />
                    <button
                      className="iconButton"
                      type="button"
                      disabled={disabled || rule.examples.length === 0}
                      onClick={() => onRemoveExample(kind, rule, index)}
                    >
                      <Trash2 size={15} />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          ))}
          {rules.length === 0 && <p className="emptyHint">暂无规则。点击“增加规则”后，新规则默认停用。</p>}
        </div>
      </div>
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
  const discoveryLabel =
    source.discovery_mode === 'telegram_primary_direct_fallback' ? 'Telegram 优先 · 直抓兜底' : '站点直抓';

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
        <span>{source.seeded_at ? '已 seed' : '待 seed'} · {effectiveInterval}s · {discoveryLabel}</span>
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
          <span>
            {account.seeded_at ? '已 seed' : '待 seed'} · {effectiveInterval}s
            {account.is_ai_source ? ' · AI信源' : ''}
          </span>
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
          className={account.is_ai_source ? 'textIconButton active' : 'textIconButton'}
          type="button"
          onClick={() => onPatch(account, { is_ai_source: !account.is_ai_source })}
          title={account.is_ai_source ? '取消 AI信源标记' : '标记为 AI信源'}
        >
          AI
        </button>
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
