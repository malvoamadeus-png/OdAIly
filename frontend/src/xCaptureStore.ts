import { createClient, type Session, type SupabaseClient } from '@supabase/supabase-js';

export type Settings = {
  global_interval_seconds: number;
  max_concurrency: number;
  jitter_seconds: number;
  updated_at: string | null;
};

export type NonMainstreamSettings = {
  global_interval_seconds: number;
  jitter_seconds: number;
  updated_at: string | null;
};

export type PublisherChannelKey = 'external_media' | 'x' | 'competitor' | 'jin10';

export type PublisherSettings = {
  enabled: boolean;
  timezone: string;
  window_start_local: string;
  window_end_local: string;
  updated_at: string | null;
};

export type PublisherChannel = {
  channel_key: PublisherChannelKey;
  display_name: string;
  enabled: boolean;
  updated_at: string | null;
};

export type PublisherRule = {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  examples: string[];
};

export type PublisherRuleProfileKey = 'regular' | 'ai_source';

export type PublisherRuleProfile = {
  key: PublisherRuleProfileKey;
  label: string;
  enabled: boolean;
  note: string;
  allow_rules: PublisherRule[];
  deny_rules: PublisherRule[];
};

export type PublisherRuleConfig = {
  version: number;
  regular: PublisherRuleProfile;
  ai_source: PublisherRuleProfile;
  updated_at: string | null;
  updated_by: string | null;
};

export type PublisherRuleConfigPayload = {
  config: PublisherRuleConfig;
  prompt_text: string;
};

export type BlockbeatsKeyStatus = 'unknown' | 'ok' | 'quota_exhausted' | 'request_failed' | 'missing_key';

export type BlockbeatsKeyConfig = {
  api_key: string;
  status: BlockbeatsKeyStatus;
  last_checked_at: string | null;
  last_success_at: string | null;
  last_quota_error_at: string | null;
  last_error: string | null;
  last_error_payload: Record<string, unknown> | null;
  updated_at: string | null;
  updated_by: string | null;
};

export type PipelineTimingMetric = {
  count: number;
  mean_seconds: number | null;
  median_seconds: number | null;
};

export type PipelineTimingSummary = PipelineTimingMetric & {
  sample_count: number;
  completed_count: number;
  completion_rate: number;
};

export type PipelineTimingStage = PipelineTimingMetric & {
  stage_key: string;
  stage_name: string;
};

export type PipelineTimingFlow = PipelineTimingSummary & {
  flow_key: string;
  flow_name: string;
  by_stage: PipelineTimingStage[];
};

export type PipelineTimingStatus = {
  status: string;
  count: number;
};

export type PipelineTimingWindow = {
  hours: number;
  label: string;
  overall: PipelineTimingSummary;
  by_stage: PipelineTimingStage[];
  by_flow: PipelineTimingFlow[];
  status_breakdown: PipelineTimingStatus[];
};

export type PipelineTimingDashboard = {
  generated_at: string | null;
  windows: PipelineTimingWindow[];
  last_error?: string | null;
};

export type Jin10Settings = {
  enabled: boolean;
  interval_seconds: number;
  endpoint_url: string;
  channel: string | null;
  request_headers: Record<string, string>;
  last_polled_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  updated_at: string | null;
};

export type Account = {
  id: number;
  username: string;
  username_lower: string;
  display_name: string | null;
  write_name: string | null;
  profile_url: string | null;
  enabled: boolean;
  is_ai_source: boolean;
  interval_seconds: number | null;
  seeded_at: string | null;
  last_polled_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
};

export type Attempt = {
  id: number;
  username_lower: string;
  status: string;
  candidate_count: number;
  seeded_count: number;
  new_count: number;
  saved_count: number;
  error: string | null;
  started_at: string;
  finished_at: string;
};

export type NonMainstreamSource = {
  id: number;
  site_key: string;
  display_name: string;
  homepage_url: string;
  capture_method: 'html_request' | 'browser_render';
  pipeline_mode: 'write_flow' | 'alert_only';
  source_group: 'external_media' | 'ai_source' | 'mixed_source';
  discovery_mode: 'direct' | 'telegram_primary_direct_fallback';
  interval_seconds: number | null;
  enabled: boolean;
  seeded_at: string | null;
  last_polled_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
};

export type WhaleWatchAddress = {
  id: number;
  address: string;
  address_lower: string;
  label: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
};

export type WhaleWatchChainState = {
  address_id: number;
  chain_key: string;
  seeded_at: string | null;
  last_polled_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  last_seen_block: number | null;
};

export type WhaleWatchActivity = {
  id: number;
  address_id: number;
  chain_key: string;
  tx_hash: string;
  activity_type: 'transfer' | 'swap';
  direction: 'in' | 'out' | null;
  summary: string;
  telegram_text: string;
  tx_url: string;
  created_at: string;
};

export type WhaleWatchHyperliquidAddress = WhaleWatchAddress;

export type WhaleWatchHyperliquidSettings = {
  single_fill_min_notional_usd: number;
  aggregate_min_notional_usd: number;
  aggregate_window_seconds: number;
  updated_at: string | null;
};

export type WhaleWatchHyperliquidState = {
  address_id: number;
  seeded_at: string | null;
  last_polled_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  last_seen_time: number | null;
};

export type WhaleWatchHyperliquidActivity = {
  id: number;
  address_id: number;
  fill_key: string;
  coin: string;
  direction: 'Open Long' | 'Open Short' | 'Close Long' | 'Close Short';
  notional_usd: string;
  closed_pnl: string;
  summary: string;
  telegram_text: string;
  tx_url: string;
  created_at: string;
};

export type WhaleWatchDashboardPayload = {
  addresses: WhaleWatchAddress[];
  states: WhaleWatchChainState[];
  activities: WhaleWatchActivity[];
  hyperliquidSettings: WhaleWatchHyperliquidSettings;
  hyperliquidAddresses: WhaleWatchHyperliquidAddress[];
  hyperliquidStates: WhaleWatchHyperliquidState[];
  hyperliquidActivities: WhaleWatchHyperliquidActivity[];
};

export type TaskItem = {
  id: number;
  source: string;
  source_item_id: string;
  source_url: string | null;
  title: string | null;
  content: string;
  status: string;
  created_at: string;
  metadata: Record<string, unknown> | null;
  pipeline: TaskPipelineSummary | null;
};

export type TaskPipelineSummary = {
  task_id: number;
  news_type: string | null;
  final_title: string | null;
  final_content: string | null;
  judge_output: Record<string, unknown>;
  prompt_template_key: string | null;
  prompt_version_id: number | null;
  writer_feature_mode_enabled: boolean | null;
  writer_model: string | null;
  writer_output: Record<string, unknown>;
  search_result: Record<string, unknown>;
  judge_completed_at: string | null;
  search_completed_at: string | null;
  write_completed_at: string | null;
  format_completed_at: string | null;
  publisher_channel: string | null;
  publisher_model: string | null;
  publisher_decision: string | null;
  publisher_reason_code: string | null;
  publisher_output: Record<string, unknown>;
  publisher_decided_at: string | null;
  publish_completed_at: string | null;
  push_result: Record<string, unknown>;
  last_error: string | null;
};

export type PromptTemplate = {
  template_key: string;
  display_name: string;
  active_version_id: number | null;
  feature_mode_enabled: boolean;
  feature_mode_text: string;
  updated_at: string;
};

export type PromptVersion = {
  id: number;
  template_key: string;
  version_number: number;
  content: string;
  note: string | null;
  created_at: string;
  published_at: string | null;
};

export type SourceExclusionScope = 'x' | 'competitor' | 'crypto_source' | 'ai_source' | 'mixed_source' | 'jin10';

export type SourceExclusionRuleGroup = {
  id: number;
  rule_key: string;
  name: string;
  description: string;
  scopes: SourceExclusionScope[];
  terms: string[];
  enabled: boolean;
  created_at: string;
  updated_at: string;
};

export type SourceExclusionRuleGroupInput = {
  name: string;
  description: string;
  scopes: SourceExclusionScope[];
  terms: string[];
  enabled: boolean;
};

export type RuntimeRuleEntry = {
  key: string;
  title: string;
  kind: string;
  scopes: string[];
  summary: string;
  content: string;
  source_location: string;
  editable: boolean;
  editable_at?: string;
};

export type RuntimeRuleSection = {
  key: string;
  label: string;
  entries: RuntimeRuleEntry[];
};

export type RuntimeRulesPayload = {
  schema_version: number;
  sections: RuntimeRuleSection[];
};

export type NewsflashSourceSummary = {
  id?: number;
  item_id?: number;
  source: string;
  source_item_id?: string;
  title: string | null;
  published_at: string | null;
  source_url: string | null;
  content?: string | null;
};

export type NewsflashEventSummary = {
  event_id: string;
  representative_title: string | null;
  event_time: string | null;
  first_source: string | null;
  first_published_at: string | null;
  source_count: number;
  competitor_source_count: number;
  has_odaily: boolean;
  status: string;
  needs_review: boolean;
  favorite: boolean;
  note: string;
  sources: NewsflashSourceSummary[];
};

export type NewsflashEventFilter =
  | 'all'
  | 'multi'
  | 'with_odaily'
  | 'high_value'
  | 'competitor_only'
  | 'competitor_consensus_missing'
  | 'odaily_only'
  | 'odaily_late'
  | 'odaily_first'
  | 'favorite';

export type NewsflashEventSourceItem = {
  id: number;
  event_id: string;
  item_id: number;
  source: string;
  source_item_id: string;
  role: string;
  match_method: string;
  similarity: number | null;
  title: string | null;
  content: string;
  source_url: string | null;
  published_at: string | null;
  note: string;
};

export type ConsoleAdmin = {
  email: string;
  created_at: string;
  updated_at: string;
};

export type DashboardPayload = {
  settings: Settings;
  accounts: Account[];
  attempts: Attempt[];
};

export type NonMainstreamDashboardPayload = {
  settings: NonMainstreamSettings;
  sources: NonMainstreamSource[];
};

export type AccountPatch = {
  display_name?: string | null;
  write_name?: string | null;
  interval_seconds?: number | null;
  enabled?: boolean;
  is_ai_source?: boolean;
};

export type NonMainstreamSourcePatch = {
  enabled?: boolean;
};

type AccountCreateInput = {
  username_or_url: string;
  display_name: string | null;
  write_name: string | null;
  interval_seconds: number | null;
  enabled: boolean;
  is_ai_source?: boolean;
};

type WhaleWatchAddressCreateInput = {
  address: string;
  label: string;
  enabled: boolean;
};

type WhaleWatchHyperliquidAddressCreateInput = WhaleWatchAddressCreateInput;

export type WhaleWatchAddressPatch = {
  label?: string;
  enabled?: boolean;
};

export type WhaleWatchHyperliquidAddressPatch = WhaleWatchAddressPatch;

export type WhaleWatchHyperliquidSettingsPatch = {
  single_fill_min_notional_usd: number;
  aggregate_min_notional_usd: number;
  aggregate_window_seconds: number;
};

type NewsflashEventSourceLinkRow = {
  id: number;
  event_id: string;
  item_id: number;
  source: string;
  source_item_id: string;
  role: string;
  match_method: string;
  similarity: number | null;
};

type NewsflashItemRow = {
  id: number;
  title: string | null;
  content: string;
  source_url: string | null;
  published_at: string | null;
};

type TaskRowWithPipeline = Omit<TaskItem, 'pipeline'> & {
  x_task_pipeline?: TaskPipelineSummary | TaskPipelineSummary[] | null;
};

const taskSelectFields = 'id,source,source_item_id,source_url,title,content,status,created_at,metadata';

const taskPipelineSelectFields = [
  'task_id',
  'news_type',
  'final_title',
  'final_content',
  'judge_output',
  'prompt_template_key',
  'prompt_version_id',
  'writer_feature_mode_enabled',
  'writer_model',
  'writer_output',
  'search_result',
  'judge_completed_at',
  'search_completed_at',
  'write_completed_at',
  'format_completed_at',
  'publisher_channel',
  'publisher_model',
  'publisher_decision',
  'publisher_reason_code',
  'publisher_output',
  'publisher_decided_at',
  'publish_completed_at',
  'push_result',
  'last_error',
].join(',');

export const processingTaskSources = [
  'x',
  'non_mainstream_media',
  'mainstream_media',
  'ai_source',
  'external_media_alert',
  'ai_source_alert',
  'blockbeats',
  'panews',
  'jinse',
  'jin10',
] as const;

const defaultSettings: Settings = {
  global_interval_seconds: 30,
  max_concurrency: 2,
  jitter_seconds: 5,
  updated_at: null,
};

const defaultNonMainstreamSettings: NonMainstreamSettings = {
  global_interval_seconds: 60,
  jitter_seconds: 5,
  updated_at: null,
};

const defaultPublisherSettings: PublisherSettings = {
  enabled: true,
  timezone: 'Asia/Shanghai',
  window_start_local: '00:01',
  window_end_local: '07:30',
  updated_at: null,
};

const defaultPublisherChannels: PublisherChannel[] = [
  { channel_key: 'external_media', display_name: 'Crypto信源', enabled: true, updated_at: null },
  { channel_key: 'x', display_name: 'X', enabled: false, updated_at: null },
  { channel_key: 'competitor', display_name: '竞品', enabled: false, updated_at: null },
  { channel_key: 'jin10', display_name: '金十', enabled: false, updated_at: null },
];

const defaultJin10Settings: Jin10Settings = {
  enabled: false,
  interval_seconds: 60,
  endpoint_url: 'https://www.jin10.com/flash_newest.js',
  channel: null,
  request_headers: {
    'x-app-id': 'bVBF4FyRTn5NJF5n',
    'x-version': '1.0.0',
    referer: 'https://www.jin10.com/',
    origin: 'https://www.jin10.com',
    'user-agent':
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
  },
  last_polled_at: null,
  last_success_at: null,
  last_error: null,
  updated_at: null,
};

const usernamePattern = /^[A-Za-z0-9_]{1,15}$/;
const reservedPaths = new Set([
  'home',
  'explore',
  'i',
  'search',
  'messages',
  'notifications',
  'settings',
  'tos',
  'privacy',
  'compose',
]);

let client: SupabaseClient | null = null;

function supabase(): SupabaseClient {
  if (client) {
    return client;
  }
  const url = import.meta.env.VITE_SUPABASE_URL as string | undefined;
  const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined;
  if (!url || !anonKey) {
    throw new Error('缺少 VITE_SUPABASE_URL 或 VITE_SUPABASE_ANON_KEY');
  }
  client = createClient(url, anonKey);
  return client;
}

export async function getCurrentSession(): Promise<Session | null> {
  const { data, error } = await supabase().auth.getSession();
  raise(error);
  return data.session;
}

async function consoleApiPost<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const session = await getCurrentSession();
  if (!session?.access_token) {
    throw new Error('登录状态已失效，请重新登录');
  }
  const baseUrl = String(
    import.meta.env.VITE_CONSOLE_API_BASE_URL || import.meta.env.VITE_EDITOR_PLUGIN_API_BASE_URL || 'https://47.76.243.147.sslip.io',
  ).replace(/\/+$/, '');
  const response = await fetch(`${baseUrl}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${session.access_token}`,
    },
    body: JSON.stringify(body),
  });
  const payload = (await response.json().catch(() => null)) as { ok?: boolean; data?: T; message?: string } | null;
  if (!response.ok || !payload?.ok) {
    throw new Error(payload?.message || `控制台服务请求失败：${response.status}`);
  }
  return payload.data as T;
}

export function onConsoleAuthStateChange(listener: (session: Session | null) => void): () => void {
  const {
    data: { subscription },
  } = supabase().auth.onAuthStateChange((_event, session) => {
    listener(session);
  });
  return () => subscription.unsubscribe();
}

export async function signInWithPassword(email: string, password: string): Promise<void> {
  const { error } = await supabase().auth.signInWithPassword({
    email: email.trim().toLowerCase(),
    password,
  });
  raise(error);
}

export async function signOut(): Promise<void> {
  const { error } = await supabase().auth.signOut();
  raise(error);
}

export async function getCurrentConsoleAdmin(): Promise<ConsoleAdmin | null> {
  const { data, error } = await supabase().from('console_admins').select('email,created_at,updated_at').limit(1).maybeSingle();
  raise(error);
  return (data ?? null) as ConsoleAdmin | null;
}

function nowIso(): string {
  return new Date().toISOString();
}

function normalizeLocalTime(value: string | null | undefined, fallback: string): string {
  const text = (value ?? '').trim();
  if (!text) {
    return fallback;
  }
  return text.slice(0, 5);
}

function normalizePublisherSettings(row: PublisherSettings): PublisherSettings {
  return {
    ...row,
    timezone: row.timezone || defaultPublisherSettings.timezone,
    window_start_local: normalizeLocalTime(row.window_start_local, defaultPublisherSettings.window_start_local),
    window_end_local: normalizeLocalTime(row.window_end_local, defaultPublisherSettings.window_end_local),
  };
}

function assertData<T>(data: T | null, fallbackMessage: string): T {
  if (data === null) {
    throw new Error(fallbackMessage);
  }
  return data;
}

function raise(error: { message: string } | null): void {
  if (error) {
    throw new Error(error.message);
  }
}

function normalizeTaskRow(row: TaskRowWithPipeline): TaskItem {
  const relation = row.x_task_pipeline;
  const pipeline = Array.isArray(relation) ? relation[0] ?? null : relation ?? null;
  const { x_task_pipeline: _pipelineRelation, ...task } = row;
  return {
    ...task,
    pipeline,
  };
}

export function normalizeUsername(value: string): string {
  let raw = value.trim();
  if (!raw) {
    throw new Error('账号不能为空');
  }
  if (raw.startsWith('@')) {
    raw = raw.slice(1);
  }

  let username: string;
  if (!raw.includes('://') && !raw.includes('/')) {
    username = raw;
  } else {
    if (!raw.includes('://')) {
      raw = `https://${raw.replace(/^\/+/, '')}`;
    }
    const parsed = new URL(raw);
    const host = parsed.hostname.toLowerCase();
    if (!['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'].includes(host)) {
      throw new Error('只支持 x.com 或 twitter.com 用户主页');
    }
    const parts = parsed.pathname.split('/').filter(Boolean);
    if (parts.length !== 1) {
      throw new Error('请输入直接的 X 用户主页地址');
    }
    username = parts[0].replace(/^@/, '');
  }

  if (reservedPaths.has(username.toLowerCase()) || !usernamePattern.test(username)) {
    throw new Error(`无效的 X 用户名：${username}`);
  }
  return username;
}

export async function loadDashboard(): Promise<DashboardPayload> {
  const [settings, accounts, attempts] = await Promise.all([
    getSettings(),
    listAccounts(),
    listRecentAttempts(30),
  ]);
  return { settings, accounts, attempts };
}

export async function loadNonMainstreamDashboard(): Promise<NonMainstreamDashboardPayload> {
  const [settings, sources] = await Promise.all([getNonMainstreamSettings(), listNonMainstreamSources()]);
  return { settings, sources };
}

export async function loadWhaleWatchDashboard(): Promise<WhaleWatchDashboardPayload> {
  const [addresses, states, activities, hyperliquidSettings, hyperliquidAddresses, hyperliquidStates, hyperliquidActivities] =
    await Promise.all([
      listWhaleWatchAddresses(),
      listWhaleWatchChainStates(),
      listWhaleWatchActivities(30),
      getWhaleWatchHyperliquidSettings(),
      listWhaleWatchHyperliquidAddresses(),
      listWhaleWatchHyperliquidStates(),
      listWhaleWatchHyperliquidActivities(30),
    ]);
  return { addresses, states, activities, hyperliquidSettings, hyperliquidAddresses, hyperliquidStates, hyperliquidActivities };
}

export async function getSettings(): Promise<Settings> {
  const { data, error } = await supabase()
    .from('x_capture_settings')
    .select('global_interval_seconds,max_concurrency,jitter_seconds,updated_at')
    .eq('singleton_key', 'global')
    .maybeSingle();
  raise(error);
  if (data) {
    return data as Settings;
  }

  const created = await supabase()
    .from('x_capture_settings')
    .upsert(
      {
        singleton_key: 'global',
        global_interval_seconds: defaultSettings.global_interval_seconds,
        max_concurrency: defaultSettings.max_concurrency,
        jitter_seconds: defaultSettings.jitter_seconds,
        updated_at: nowIso(),
      },
      { onConflict: 'singleton_key' },
    )
    .select('global_interval_seconds,max_concurrency,jitter_seconds,updated_at')
    .single();
  raise(created.error);
  return assertData(created.data as Settings | null, '无法初始化全局配置');
}

export async function updateSettings(payload: Omit<Settings, 'updated_at'>): Promise<Settings> {
  const { data, error } = await supabase()
    .from('x_capture_settings')
    .upsert(
      {
        singleton_key: 'global',
        global_interval_seconds: payload.global_interval_seconds,
        max_concurrency: payload.max_concurrency,
        jitter_seconds: payload.jitter_seconds,
        updated_at: nowIso(),
      },
      { onConflict: 'singleton_key' },
    )
    .select('global_interval_seconds,max_concurrency,jitter_seconds,updated_at')
    .single();
  raise(error);
  return assertData(data as Settings | null, '保存全局配置失败');
}

export async function getNonMainstreamSettings(): Promise<NonMainstreamSettings> {
  const { data, error } = await supabase()
    .from('non_mainstream_media_settings')
    .select('global_interval_seconds,jitter_seconds,updated_at')
    .eq('singleton_key', 'global')
    .maybeSingle();
  raise(error);
  if (data) {
    return data as NonMainstreamSettings;
  }

  const created = await supabase()
    .from('non_mainstream_media_settings')
    .upsert(
      {
        singleton_key: 'global',
        global_interval_seconds: defaultNonMainstreamSettings.global_interval_seconds,
        jitter_seconds: defaultNonMainstreamSettings.jitter_seconds,
        updated_at: nowIso(),
      },
      { onConflict: 'singleton_key' },
    )
    .select('global_interval_seconds,jitter_seconds,updated_at')
    .single();
  raise(created.error);
  return assertData(created.data as NonMainstreamSettings | null, '无法初始化非主流媒体全局配置');
}

export async function updateNonMainstreamSettings(
  payload: Omit<NonMainstreamSettings, 'updated_at'>,
): Promise<NonMainstreamSettings> {
  const { data, error } = await supabase()
    .from('non_mainstream_media_settings')
    .upsert(
      {
        singleton_key: 'global',
        global_interval_seconds: payload.global_interval_seconds,
        jitter_seconds: payload.jitter_seconds,
        updated_at: nowIso(),
      },
      { onConflict: 'singleton_key' },
    )
    .select('global_interval_seconds,jitter_seconds,updated_at')
    .single();
  raise(error);
  return assertData(data as NonMainstreamSettings | null, '保存非主流媒体全局配置失败');
}

export async function getPublisherSettings(): Promise<PublisherSettings> {
  const selectFields = 'enabled,timezone,window_start_local,window_end_local,updated_at';
  const { data, error } = await supabase()
    .from('publisher_settings')
    .select(selectFields)
    .eq('singleton_key', 'global')
    .maybeSingle();
  raise(error);
  if (data) {
    return normalizePublisherSettings(data as PublisherSettings);
  }

  const created = await supabase()
    .from('publisher_settings')
    .upsert(
      {
        singleton_key: 'global',
        enabled: defaultPublisherSettings.enabled,
        timezone: defaultPublisherSettings.timezone,
        window_start_local: defaultPublisherSettings.window_start_local,
        window_end_local: defaultPublisherSettings.window_end_local,
        updated_at: nowIso(),
      },
      { onConflict: 'singleton_key' },
    )
    .select(selectFields)
    .single();
  raise(created.error);
  return normalizePublisherSettings(assertData(created.data as PublisherSettings | null, '无法初始化发布者配置'));
}

async function seedPublisherChannels(): Promise<void> {
  const { error } = await supabase()
    .from('publisher_channels')
    .upsert(
      defaultPublisherChannels.map((item) => ({
        channel_key: item.channel_key,
        display_name: item.display_name,
        enabled: item.enabled,
        updated_at: nowIso(),
      })),
      { onConflict: 'channel_key' },
    );
  raise(error);
}

export async function listPublisherChannels(): Promise<PublisherChannel[]> {
  const selectFields = 'channel_key,display_name,enabled,updated_at';
  const { data, error } = await supabase()
    .from('publisher_channels')
    .select(selectFields)
    .order('channel_key', { ascending: true });
  raise(error);
  if ((data ?? []).length > 0) {
    return (data ?? []) as PublisherChannel[];
  }

  await seedPublisherChannels();
  const seeded = await supabase()
    .from('publisher_channels')
    .select(selectFields)
    .order('channel_key', { ascending: true });
  raise(seeded.error);
  return (seeded.data ?? []) as PublisherChannel[];
}

export async function updatePublisherSettings(
  payload: Pick<PublisherSettings, 'enabled' | 'window_start_local' | 'window_end_local'>,
): Promise<PublisherSettings> {
  const { data, error } = await supabase()
    .from('publisher_settings')
    .upsert(
      {
        singleton_key: 'global',
        enabled: payload.enabled,
        timezone: defaultPublisherSettings.timezone,
        window_start_local: payload.window_start_local,
        window_end_local: payload.window_end_local,
        updated_at: nowIso(),
      },
      { onConflict: 'singleton_key' },
    )
    .select('enabled,timezone,window_start_local,window_end_local,updated_at')
    .single();
  raise(error);
  return normalizePublisherSettings(assertData(data as PublisherSettings | null, '保存发布者配置失败'));
}

export async function updatePublisherChannel(channelKey: PublisherChannelKey, enabled: boolean): Promise<PublisherChannel> {
  const { data, error } = await supabase()
    .from('publisher_channels')
    .update({
      enabled,
      updated_at: nowIso(),
    })
    .eq('channel_key', channelKey)
    .select('channel_key,display_name,enabled,updated_at')
    .single();
  raise(error);
  return assertData(data as PublisherChannel | null, '保存发布渠道失败');
}

export async function getPublisherRuleConfig(): Promise<PublisherRuleConfigPayload> {
  return consoleApiPost<PublisherRuleConfigPayload>('/console/publisher-rules/get');
}

async function consoleApiGet<T>(path: string): Promise<T> {
  const session = await getCurrentSession();
  if (!session?.access_token) {
    throw new Error('登录状态已失效，请重新登录');
  }
  const baseUrl = String(
    import.meta.env.VITE_CONSOLE_API_BASE_URL || import.meta.env.VITE_EDITOR_PLUGIN_API_BASE_URL || 'https://47.76.243.147.sslip.io',
  ).replace(/\/+$/, '');
  const response = await fetch(`${baseUrl}${path}`, {
    method: 'GET',
    headers: { Authorization: `Bearer ${session.access_token}` },
  });
  const payload = (await response.json().catch(() => null)) as { ok?: boolean; data?: T; message?: string } | null;
  if (!response.ok || !payload?.ok) {
    throw new Error(payload?.message || `控制台服务请求失败：${response.status}`);
  }
  return payload.data as T;
}

export async function getPipelineTimingDashboard(): Promise<PipelineTimingDashboard> {
  return consoleApiPost<PipelineTimingDashboard>('/console/pipeline-timing/get');
}

export async function getRuntimeRules(): Promise<RuntimeRulesPayload> {
  return consoleApiGet<RuntimeRulesPayload>('/console/runtime-rules/get');
}

export async function savePublisherRuleConfig(config: PublisherRuleConfig): Promise<PublisherRuleConfigPayload> {
  return consoleApiPost<PublisherRuleConfigPayload>('/console/publisher-rules/save', { config });
}

export async function getBlockbeatsKeyConfig(): Promise<BlockbeatsKeyConfig> {
  return consoleApiPost<BlockbeatsKeyConfig>('/console/blockbeats-key/get');
}

export async function saveBlockbeatsKey(apiKey: string): Promise<BlockbeatsKeyConfig> {
  return consoleApiPost<BlockbeatsKeyConfig>('/console/blockbeats-key/save', { api_key: apiKey });
}

export async function getPublisherRuleConfigSnapshot(): Promise<PublisherRuleConfigPayload | null> {
  const { data, error } = await supabase()
    .from('publisher_rule_config')
    .select('config_json,prompt_text')
    .eq('singleton_key', 'global')
    .maybeSingle();
  raise(error);
  if (!data || typeof data !== 'object') {
    return null;
  }
  const row = data as { config_json?: PublisherRuleConfig | null; prompt_text?: string | null };
  if (!row.config_json || typeof row.config_json !== 'object') {
    return null;
  }
  return {
    config: row.config_json,
    prompt_text: row.prompt_text || '',
  };
}

function normalizeJin10Settings(row: Jin10Settings): Jin10Settings {
  return {
    ...defaultJin10Settings,
    ...row,
    request_headers: row.request_headers && typeof row.request_headers === 'object' ? row.request_headers : defaultJin10Settings.request_headers,
  };
}

export async function getJin10Settings(): Promise<Jin10Settings> {
  const selectFields = [
    'enabled',
    'interval_seconds',
    'endpoint_url',
    'channel',
    'request_headers',
    'last_polled_at',
    'last_success_at',
    'last_error',
    'updated_at',
  ].join(',');
  const { data, error } = await supabase().from('jin10_settings').select(selectFields).eq('singleton_key', 'global').maybeSingle();
  raise(error);
  if (data) {
    return normalizeJin10Settings(data as unknown as Jin10Settings);
  }
  const created = await supabase()
    .from('jin10_settings')
    .upsert(
      {
        singleton_key: 'global',
        enabled: defaultJin10Settings.enabled,
        interval_seconds: defaultJin10Settings.interval_seconds,
        endpoint_url: defaultJin10Settings.endpoint_url,
        channel: defaultJin10Settings.channel,
        request_headers: defaultJin10Settings.request_headers,
        updated_at: nowIso(),
      },
      { onConflict: 'singleton_key' },
    )
    .select(selectFields)
    .single();
  raise(created.error);
  return normalizeJin10Settings(assertData(created.data as Jin10Settings | null, '无法初始化金十配置'));
}

export async function updateJin10Settings(
  patch: Pick<Jin10Settings, 'enabled' | 'interval_seconds' | 'endpoint_url' | 'channel' | 'request_headers'>,
): Promise<Jin10Settings> {
  const { data, error } = await supabase()
    .from('jin10_settings')
    .upsert(
      {
        singleton_key: 'global',
        enabled: patch.enabled,
        interval_seconds: patch.interval_seconds,
        endpoint_url: patch.endpoint_url,
        channel: patch.channel?.trim() || null,
        request_headers: patch.request_headers,
        updated_at: nowIso(),
      },
      { onConflict: 'singleton_key' },
    )
    .select(
      [
        'enabled',
        'interval_seconds',
        'endpoint_url',
        'channel',
        'request_headers',
        'last_polled_at',
        'last_success_at',
        'last_error',
        'updated_at',
      ].join(','),
    )
    .single();
  raise(error);
  return normalizeJin10Settings(assertData(data as Jin10Settings | null, '保存金十配置失败'));
}

export async function listAccounts(): Promise<Account[]> {
  const { data, error } = await supabase()
    .from('x_capture_accounts')
    .select(
      [
        'id',
        'username',
        'username_lower',
        'display_name',
        'write_name',
        'profile_url',
        'enabled',
        'is_ai_source',
        'interval_seconds',
        'seeded_at',
        'last_polled_at',
        'last_success_at',
        'last_error',
      ].join(','),
    )
    .order('enabled', { ascending: false })
    .order('username_lower', { ascending: true });
  raise(error);
  return (data ?? []) as unknown as Account[];
}

export async function listNonMainstreamSources(): Promise<NonMainstreamSource[]> {
  const { data, error } = await supabase()
    .from('non_mainstream_media_sources')
    .select(
      [
        'id',
        'site_key',
        'display_name',
        'homepage_url',
        'capture_method',
        'pipeline_mode',
        'source_group',
        'discovery_mode',
        'interval_seconds',
        'enabled',
        'seeded_at',
        'last_polled_at',
        'last_success_at',
        'last_error',
      ].join(','),
    )
    .order('enabled', { ascending: false })
    .order('display_name', { ascending: true });
  raise(error);
  return (data ?? []) as unknown as NonMainstreamSource[];
}

export async function createAccount(input: AccountCreateInput): Promise<Account> {
  const username = normalizeUsername(input.username_or_url);
  const displayName = input.display_name?.trim() || null;
  const writeName = input.write_name?.trim() || null;
  const { data, error } = await supabase()
    .from('x_capture_accounts')
    .upsert(
      {
        username,
        username_lower: username.toLowerCase(),
        display_name: displayName,
        write_name: writeName,
        profile_url: `https://x.com/${username}`,
        enabled: input.enabled,
        is_ai_source: Boolean(input.is_ai_source),
        interval_seconds: input.interval_seconds,
        updated_at: nowIso(),
      },
      { onConflict: 'username_lower' },
    )
    .select('*')
    .single();
  raise(error);
  return assertData(data as Account | null, '保存账号失败');
}

export async function updateAccount(accountId: number, patch: AccountPatch): Promise<Account> {
  const payload: Record<string, string | number | boolean | null> = {
    updated_at: nowIso(),
  };
  if ('display_name' in patch) {
    payload.display_name = patch.display_name?.trim() || null;
  }
  if ('write_name' in patch) {
    payload.write_name = patch.write_name?.trim() || null;
  }
  if ('interval_seconds' in patch) {
    payload.interval_seconds = patch.interval_seconds ?? null;
  }
  if ('enabled' in patch && patch.enabled !== undefined) {
    payload.enabled = patch.enabled;
  }
  if ('is_ai_source' in patch && patch.is_ai_source !== undefined) {
    payload.is_ai_source = patch.is_ai_source;
  }

  const { data, error } = await supabase().from('x_capture_accounts').update(payload).eq('id', accountId).select('*').single();
  raise(error);
  return assertData(data as Account | null, '更新账号失败');
}

export async function updateNonMainstreamSource(
  sourceId: number,
  patch: NonMainstreamSourcePatch,
): Promise<NonMainstreamSource> {
  const payload: Record<string, string | boolean> = {
    updated_at: nowIso(),
  };
  if ('enabled' in patch && patch.enabled !== undefined) {
    payload.enabled = patch.enabled;
  }
  const { data, error } = await supabase()
    .from('non_mainstream_media_sources')
    .update(payload)
    .eq('id', sourceId)
    .select(
      [
        'id',
        'site_key',
        'display_name',
        'homepage_url',
        'capture_method',
        'pipeline_mode',
        'source_group',
        'discovery_mode',
        'interval_seconds',
        'enabled',
        'seeded_at',
        'last_polled_at',
        'last_success_at',
        'last_error',
      ].join(','),
    )
    .single();
  raise(error);
  return assertData(data as NonMainstreamSource | null, '更新非主流媒体站点失败');
}

export async function deleteAccount(accountId: number): Promise<void> {
  const { error } = await supabase().from('x_capture_accounts').delete().eq('id', accountId);
  raise(error);
}

export async function listWhaleWatchAddresses(): Promise<WhaleWatchAddress[]> {
  const { data, error } = await supabase()
    .from('whale_watch_addresses')
    .select('id,address,address_lower,label,enabled,created_at,updated_at')
    .order('enabled', { ascending: false })
    .order('label', { ascending: true });
  raise(error);
  return (data ?? []) as unknown as WhaleWatchAddress[];
}

export async function listWhaleWatchChainStates(): Promise<WhaleWatchChainState[]> {
  const { data, error } = await supabase()
    .from('whale_watch_chain_states')
    .select('address_id,chain_key,seeded_at,last_polled_at,last_success_at,last_error,last_seen_block')
    .order('chain_key', { ascending: true });
  raise(error);
  return (data ?? []) as unknown as WhaleWatchChainState[];
}

export async function listWhaleWatchActivities(limit: number): Promise<WhaleWatchActivity[]> {
  const { data, error } = await supabase()
    .from('whale_watch_activities')
    .select('id,address_id,chain_key,tx_hash,activity_type,direction,summary,telegram_text,tx_url,created_at')
    .order('created_at', { ascending: false })
    .limit(limit);
  raise(error);
  return (data ?? []) as unknown as WhaleWatchActivity[];
}

export async function createWhaleWatchAddress(input: WhaleWatchAddressCreateInput): Promise<WhaleWatchAddress> {
  const address = normalizeEvmAddress(input.address);
  const label = input.label.trim();
  if (!label) {
    throw new Error('自定义标签不能为空');
  }
  const { data, error } = await supabase()
    .from('whale_watch_addresses')
    .upsert(
      {
        address,
        address_lower: address.toLowerCase(),
        label,
        enabled: input.enabled,
        updated_at: nowIso(),
      },
      { onConflict: 'address_lower' },
    )
    .select('id,address,address_lower,label,enabled,created_at,updated_at')
    .single();
  raise(error);
  return assertData(data as WhaleWatchAddress | null, '保存巨鲸地址失败');
}

export async function updateWhaleWatchAddress(
  addressId: number,
  patch: WhaleWatchAddressPatch,
): Promise<WhaleWatchAddress> {
  const payload: Record<string, string | boolean> = {
    updated_at: nowIso(),
  };
  if ('label' in patch && patch.label !== undefined) {
    const label = patch.label.trim();
    if (!label) {
      throw new Error('自定义标签不能为空');
    }
    payload.label = label;
  }
  if ('enabled' in patch && patch.enabled !== undefined) {
    payload.enabled = patch.enabled;
  }
  const { data, error } = await supabase()
    .from('whale_watch_addresses')
    .update(payload)
    .eq('id', addressId)
    .select('id,address,address_lower,label,enabled,created_at,updated_at')
    .single();
  raise(error);
  return assertData(data as WhaleWatchAddress | null, '更新巨鲸地址失败');
}

export async function deleteWhaleWatchAddress(addressId: number): Promise<void> {
  const { error } = await supabase().from('whale_watch_addresses').delete().eq('id', addressId);
  raise(error);
}

export async function listWhaleWatchHyperliquidAddresses(): Promise<WhaleWatchHyperliquidAddress[]> {
  const { data, error } = await supabase()
    .from('whale_watch_hyperliquid_addresses')
    .select('id,address,address_lower,label,enabled,created_at,updated_at')
    .order('enabled', { ascending: false })
    .order('label', { ascending: true });
  raise(error);
  return (data ?? []) as unknown as WhaleWatchHyperliquidAddress[];
}

export async function getWhaleWatchHyperliquidSettings(): Promise<WhaleWatchHyperliquidSettings> {
  const { data, error } = await supabase()
    .from('whale_watch_hyperliquid_settings')
    .select('single_fill_min_notional_usd,aggregate_min_notional_usd,aggregate_window_seconds,updated_at')
    .eq('singleton_key', 'global')
    .maybeSingle();
  raise(error);
  if (data) {
    return data as WhaleWatchHyperliquidSettings;
  }

  const created = await supabase()
    .from('whale_watch_hyperliquid_settings')
    .upsert(
      {
        singleton_key: 'global',
        single_fill_min_notional_usd: 500000,
        aggregate_min_notional_usd: 1000000,
        aggregate_window_seconds: 600,
        updated_at: nowIso(),
      },
      { onConflict: 'singleton_key' },
    )
    .select('single_fill_min_notional_usd,aggregate_min_notional_usd,aggregate_window_seconds,updated_at')
    .single();
  raise(created.error);
  return assertData(created.data as WhaleWatchHyperliquidSettings | null, '无法初始化 Hyperliquid 设置');
}

export async function listWhaleWatchHyperliquidStates(): Promise<WhaleWatchHyperliquidState[]> {
  const { data, error } = await supabase()
    .from('whale_watch_hyperliquid_states')
    .select('address_id,seeded_at,last_polled_at,last_success_at,last_error,last_seen_time')
    .order('address_id', { ascending: true });
  raise(error);
  return (data ?? []) as unknown as WhaleWatchHyperliquidState[];
}

export async function listWhaleWatchHyperliquidActivities(limit: number): Promise<WhaleWatchHyperliquidActivity[]> {
  const { data, error } = await supabase()
    .from('whale_watch_hyperliquid_activities')
    .select('id,address_id,fill_key,coin,direction,notional_usd,closed_pnl,summary,telegram_text,tx_url,created_at')
    .order('created_at', { ascending: false })
    .limit(limit);
  raise(error);
  return (data ?? []) as unknown as WhaleWatchHyperliquidActivity[];
}

export async function createWhaleWatchHyperliquidAddress(
  input: WhaleWatchHyperliquidAddressCreateInput,
): Promise<WhaleWatchHyperliquidAddress> {
  const address = normalizeEvmAddress(input.address);
  const label = input.label.trim();
  if (!label) {
    throw new Error('自定义标签不能为空');
  }
  const { data, error } = await supabase()
    .from('whale_watch_hyperliquid_addresses')
    .upsert(
      {
        address,
        address_lower: address.toLowerCase(),
        label,
        enabled: input.enabled,
        updated_at: nowIso(),
      },
      { onConflict: 'address_lower' },
    )
    .select('id,address,address_lower,label,enabled,created_at,updated_at')
    .single();
  raise(error);
  return assertData(data as WhaleWatchHyperliquidAddress | null, '保存 Hyperliquid 地址失败');
}

export async function updateWhaleWatchHyperliquidAddress(
  addressId: number,
  patch: WhaleWatchHyperliquidAddressPatch,
): Promise<WhaleWatchHyperliquidAddress> {
  const payload: Record<string, string | boolean> = {
    updated_at: nowIso(),
  };
  if ('label' in patch && patch.label !== undefined) {
    const label = patch.label.trim();
    if (!label) {
      throw new Error('自定义标签不能为空');
    }
    payload.label = label;
  }
  if ('enabled' in patch && patch.enabled !== undefined) {
    payload.enabled = patch.enabled;
  }
  const { data, error } = await supabase()
    .from('whale_watch_hyperliquid_addresses')
    .update(payload)
    .eq('id', addressId)
    .select('id,address,address_lower,label,enabled,created_at,updated_at')
    .single();
  raise(error);
  return assertData(data as WhaleWatchHyperliquidAddress | null, '更新 Hyperliquid 地址失败');
}

export async function deleteWhaleWatchHyperliquidAddress(addressId: number): Promise<void> {
  const { error } = await supabase().from('whale_watch_hyperliquid_addresses').delete().eq('id', addressId);
  raise(error);
}

export async function updateWhaleWatchHyperliquidSettings(
  patch: WhaleWatchHyperliquidSettingsPatch,
): Promise<WhaleWatchHyperliquidSettings> {
  const { data, error } = await supabase()
    .from('whale_watch_hyperliquid_settings')
    .upsert(
      {
        singleton_key: 'global',
        single_fill_min_notional_usd: patch.single_fill_min_notional_usd,
        aggregate_min_notional_usd: patch.aggregate_min_notional_usd,
        aggregate_window_seconds: patch.aggregate_window_seconds,
        updated_at: nowIso(),
      },
      { onConflict: 'singleton_key' },
    )
    .select('single_fill_min_notional_usd,aggregate_min_notional_usd,aggregate_window_seconds,updated_at')
    .single();
  raise(error);
  return assertData(data as WhaleWatchHyperliquidSettings | null, '保存 Hyperliquid 设置失败');
}

export function normalizeEvmAddress(value: string): string {
  const address = value.trim();
  if (!/^0x[0-9a-fA-F]{40}$/.test(address)) {
    throw new Error('请输入合法的 EVM 地址');
  }
  return address;
}

async function listRecentAttempts(limit: number): Promise<Attempt[]> {
  const { data, error } = await supabase()
    .from('x_capture_attempts')
    .select(
      [
        'id',
        'username_lower',
        'status',
        'candidate_count',
        'seeded_count',
        'new_count',
        'saved_count',
        'error',
        'started_at',
        'finished_at',
      ].join(','),
    )
    .order('started_at', { ascending: false })
    .limit(limit);
  raise(error);
  return (data ?? []) as unknown as Attempt[];
}

export async function listRecentJin10Tasks(limit = 30): Promise<TaskItem[]> {
  return listRecentTasksBySources(['jin10'], limit);
}

export async function listRecentTasksBySources(sources: readonly string[], limit = 80): Promise<TaskItem[]> {
  const normalizedLimit = Math.min(Math.max(Math.trunc(limit), 1), 80);
  const targetSources = sources.length > 0 ? [...sources] : [...processingTaskSources];
  const fetchSourceTasks = async (source: string) => {
    const { data, error } = await supabase()
      .from('tasks')
      .select(`${taskSelectFields},x_task_pipeline(${taskPipelineSelectFields})`)
      .eq('source', source)
      .order('created_at', { ascending: false })
      .limit(normalizedLimit);
    raise(error);
    return ((data ?? []) as unknown as TaskRowWithPipeline[]).map(normalizeTaskRow);
  };

  const groups = await Promise.all(targetSources.map(fetchSourceTasks));
  return groups
    .flat()
    .sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at));
}

export async function listPromptTemplates(): Promise<PromptTemplate[]> {
  const { data, error } = await supabase()
    .from('prompt_templates')
    .select('template_key,display_name,active_version_id,feature_mode_enabled,feature_mode_text,updated_at')
    .order('template_key', { ascending: true });
  raise(error);
  return (data ?? []) as unknown as PromptTemplate[];
}

export async function listPromptVersions(templateKey: string): Promise<PromptVersion[]> {
  const { data, error } = await supabase()
    .from('prompt_template_versions')
    .select('id,template_key,version_number,content,note,created_at,published_at')
    .eq('template_key', templateKey)
    .is('deleted_at', null)
    .order('version_number', { ascending: false });
  raise(error);
  return (data ?? []) as unknown as PromptVersion[];
}

export async function createPromptVersion(
  templateKey: string,
  content: string,
  note: string | null,
): Promise<PromptVersion> {
  const { data: latestVersionRow, error: latestVersionError } = await supabase()
    .from('prompt_template_versions')
    .select('version_number')
    .eq('template_key', templateKey)
    .order('version_number', { ascending: false })
    .limit(1)
    .maybeSingle();
  raise(latestVersionError);
  const nextVersion = Number(latestVersionRow?.version_number ?? 0) + 1;
  const { data, error } = await supabase()
    .from('prompt_template_versions')
    .insert({
      template_key: templateKey,
      version_number: nextVersion,
      content,
      note,
    })
    .select('id,template_key,version_number,content,note,created_at,published_at')
    .single();
  raise(error);
  return assertData(data as PromptVersion | null, '创建 Prompt 版本失败');
}

export async function publishPromptVersion(templateKey: string, versionId: number): Promise<PromptTemplate> {
  const published = await supabase()
    .from('prompt_template_versions')
    .update({ published_at: nowIso() })
    .eq('id', versionId);
  raise(published.error);

  const { data, error } = await supabase()
    .from('prompt_templates')
    .update({
      active_version_id: versionId,
      updated_at: nowIso(),
    })
    .eq('template_key', templateKey)
    .select('template_key,display_name,active_version_id,feature_mode_enabled,feature_mode_text,updated_at')
    .single();
  raise(error);
  return assertData(data as PromptTemplate | null, '发布 Prompt 版本失败');
}

export async function updatePromptTemplateFeatureMode(
  templateKey: string,
  enabled: boolean,
  featureModeText: string,
): Promise<PromptTemplate> {
  const { data, error } = await supabase()
    .from('prompt_templates')
    .update({
      feature_mode_enabled: enabled,
      feature_mode_text: featureModeText,
      updated_at: nowIso(),
    })
    .eq('template_key', templateKey)
    .select('template_key,display_name,active_version_id,feature_mode_enabled,feature_mode_text,updated_at')
    .single();
  raise(error);
  return assertData(data as PromptTemplate | null, 'failed to update prompt feature mode');
}

export async function deletePromptVersion(templateKey: string, versionId: number): Promise<void> {
  const [templates, versions] = await Promise.all([listPromptTemplates(), listPromptVersions(templateKey)]);
  const template = templates.find((item) => item.template_key === templateKey);
  const targetVersion = versions.find((item) => item.id === versionId);
  if (!template || !targetVersion) {
    throw new Error('prompt version not found');
  }
  if (versions.length <= 1) {
    throw new Error('at least one prompt version must remain');
  }

  if (template.active_version_id === versionId) {
    const fallback = versions.find((item) => item.id !== versionId);
    if (!fallback) {
      throw new Error('cannot delete the active prompt version');
    }
    const { error: updateError } = await supabase()
      .from('prompt_templates')
      .update({
        active_version_id: fallback.id,
        updated_at: nowIso(),
      })
      .eq('template_key', templateKey);
    raise(updateError);
  }

  const { error } = await supabase()
    .from('prompt_template_versions')
    .update({
      deleted_at: nowIso(),
    })
    .eq('id', versionId);
  raise(error);
}

export function normalizeSourceExclusionTerm(value: string): string {
  return value.normalize('NFKC').trim().replace(/\s+/g, ' ').toLocaleLowerCase();
}

export function normalizeSourceExclusionTerms(values: string[]): string[] {
  return values
    .map((term) => term.trim())
    .filter(Boolean)
    .filter((term, index, all) => {
      const normalized = normalizeSourceExclusionTerm(term);
      return normalized && all.findIndex((item) => normalizeSourceExclusionTerm(item) === normalized) === index;
    });
}

export async function listSourceExclusionRuleGroups(): Promise<SourceExclusionRuleGroup[]> {
  const { data, error } = await supabase()
    .from('source_exclusion_rule_groups')
    .select('id,rule_key,name,description,scopes,terms,enabled,created_at,updated_at')
    .order('enabled', { ascending: false })
    .order('name', { ascending: true });
  raise(error);
  return (data ?? []) as unknown as SourceExclusionRuleGroup[];
}

export async function createSourceExclusionRuleGroup(
  input: SourceExclusionRuleGroupInput,
): Promise<SourceExclusionRuleGroup> {
  const { data, error } = await supabase()
    .from('source_exclusion_rule_groups')
    .insert({
      rule_key: crypto.randomUUID(),
      ...input,
      terms: normalizeSourceExclusionTerms(input.terms),
      updated_at: nowIso(),
    })
    .select('id,rule_key,name,description,scopes,terms,enabled,created_at,updated_at')
    .single();
  raise(error);
  return assertData(data as SourceExclusionRuleGroup | null, '创建排除规则组失败');
}

export async function updateSourceExclusionRuleGroup(
  id: number,
  input: SourceExclusionRuleGroupInput,
): Promise<SourceExclusionRuleGroup> {
  const payload = {
    ...input,
    name: input.name.trim(),
    description: input.description.trim(),
    terms: normalizeSourceExclusionTerms(input.terms),
    updated_at: nowIso(),
  };
  const { data, error } = await supabase()
    .from('source_exclusion_rule_groups')
    .update(payload)
    .eq('id', id)
    .select('id,rule_key,name,description,scopes,terms,enabled,created_at,updated_at')
    .single();
  raise(error);
  return assertData(data as SourceExclusionRuleGroup | null, '更新排除规则组失败');
}

export async function deleteSourceExclusionRuleGroup(id: number): Promise<void> {
  const { error } = await supabase().from('source_exclusion_rule_groups').delete().eq('id', id);
  raise(error);
}

export async function listNewsflashEvents(
  filter: NewsflashEventFilter = 'all',
  limit = 100,
  offset = 0,
): Promise<NewsflashEventSummary[]> {
  let query = supabase()
    .from('newsflash_event_summary')
    .select(
      [
        'event_id',
        'representative_title',
        'event_time',
        'first_source',
        'first_published_at',
        'source_count',
        'competitor_source_count',
        'has_odaily',
        'status',
        'needs_review',
        'favorite',
        'note',
        'sources',
      ].join(','),
    )
    .order('event_time', { ascending: false, nullsFirst: false })
    .range(offset, offset + limit - 1);

  if (filter === 'multi') {
    query = query.gte('source_count', 2);
  } else if (filter === 'with_odaily') {
    query = query.eq('has_odaily', true);
  } else if (filter === 'high_value') {
    query = query.eq('has_odaily', true).gte('source_count', 2);
  } else if (filter === 'competitor_only') {
    query = query.eq('has_odaily', false).gte('competitor_source_count', 1);
  } else if (filter === 'competitor_consensus_missing') {
    query = query.eq('has_odaily', false).gte('competitor_source_count', 2);
  } else if (filter === 'odaily_only') {
    query = query.eq('has_odaily', true).eq('source_count', 1);
  } else if (filter === 'odaily_late') {
    query = query.eq('has_odaily', true).neq('first_source', 'odaily');
  } else if (filter === 'odaily_first') {
    query = query.eq('has_odaily', true).eq('first_source', 'odaily');
  } else if (filter === 'favorite') {
    query = query.eq('favorite', true);
  }

  const { data, error } = await query;
  raise(error);
  return ((data ?? []) as unknown as NewsflashEventSummary[]).map((event) => ({
    ...event,
    note: event.note ?? '',
    sources: Array.isArray(event.sources) ? event.sources : [],
  }));
}

export async function listNewsflashEventSources(eventId: string): Promise<NewsflashEventSourceItem[]> {
  const { data, error } = await supabase()
    .from('newsflash_event_sources')
    .select(
      [
        'id',
        'event_id',
        'item_id',
        'source',
        'source_item_id',
        'role',
        'match_method',
        'similarity',
      ].join(','),
    )
    .eq('event_id', eventId)
    .order('role', { ascending: true })
    .order('id', { ascending: true });
  raise(error);
  const rows = (data ?? []) as unknown as NewsflashEventSourceLinkRow[];
  const itemIds = rows.map((row) => row.item_id);
  let itemsById = new Map<number, NewsflashItemRow>();
  let notesByItem = new Map<number, string>();
  if (itemIds.length > 0) {
    const [items, notes] = await Promise.all([
      supabase().from('newsflash_items').select('id,title,content,source_url,published_at').in('id', itemIds),
      supabase().from('newsflash_item_notes').select('item_id,note').in('item_id', itemIds),
    ]);
    raise(items.error);
    raise(notes.error);
    itemsById = new Map(((items.data ?? []) as NewsflashItemRow[]).map((item) => [item.id, item]));
    notesByItem = new Map(((notes.data ?? []) as { item_id: number; note: string }[]).map((row) => [row.item_id, row.note]));
  }
  return rows.map((row) => ({
    id: row.id,
    event_id: row.event_id,
    item_id: row.item_id,
    source: row.source,
    source_item_id: row.source_item_id,
    role: row.role,
    match_method: row.match_method,
    similarity: row.similarity,
    title: itemsById.get(row.item_id)?.title ?? null,
    content: itemsById.get(row.item_id)?.content ?? '',
    source_url: itemsById.get(row.item_id)?.source_url ?? null,
    published_at: itemsById.get(row.item_id)?.published_at ?? null,
    note: notesByItem.get(row.item_id) ?? '',
  }));
}

export async function setNewsflashEventFavorite(eventId: string, favorite: boolean): Promise<void> {
  if (favorite) {
    const { error } = await supabase()
      .from('newsflash_event_favorites')
      .upsert({ event_id: eventId, favorite: true, updated_at: nowIso() }, { onConflict: 'event_id' });
    raise(error);
    return;
  }
  const { error } = await supabase().from('newsflash_event_favorites').delete().eq('event_id', eventId);
  raise(error);
}

export async function saveNewsflashItemNote(itemId: number, note: string): Promise<void> {
  const trimmed = note.trim();
  if (!trimmed) {
    const { error } = await supabase().from('newsflash_item_notes').delete().eq('item_id', itemId);
    raise(error);
    return;
  }
  const { error } = await supabase()
    .from('newsflash_item_notes')
    .upsert({ item_id: itemId, note: trimmed, updated_at: nowIso() }, { onConflict: 'item_id' });
  raise(error);
}

export async function saveNewsflashEventNote(eventId: string, note: string): Promise<void> {
  const trimmed = note.trim();
  if (!trimmed) {
    const { error } = await supabase().from('newsflash_event_notes').delete().eq('event_id', eventId);
    raise(error);
    return;
  }
  const { error } = await supabase()
    .from('newsflash_event_notes')
    .upsert({ event_id: eventId, note: trimmed, updated_at: nowIso() }, { onConflict: 'event_id' });
  raise(error);
}
