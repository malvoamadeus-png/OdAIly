import { createClient, type SupabaseClient } from '@supabase/supabase-js';

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

export type Account = {
  id: number;
  username: string;
  username_lower: string;
  display_name: string | null;
  profile_url: string | null;
  enabled: boolean;
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
  enabled: boolean;
  seeded_at: string | null;
  last_polled_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
};

export type TaskItem = {
  id: number;
  source_item_id: string;
  source_url: string | null;
  title: string | null;
  content: string;
  status: string;
  created_at: string;
};

export type PromptTemplate = {
  template_key: string;
  display_name: string;
  active_version_id: number | null;
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

export type CompetitorFilterKeyword = {
  id: number;
  term: string;
  term_normalized: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
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

export type DashboardPayload = {
  settings: Settings;
  accounts: Account[];
  attempts: Attempt[];
  tasks: TaskItem[];
};

export type NonMainstreamDashboardPayload = {
  settings: NonMainstreamSettings;
  sources: NonMainstreamSource[];
};

export type AccountPatch = {
  display_name?: string | null;
  interval_seconds?: number | null;
  enabled?: boolean;
};

export type NonMainstreamSourcePatch = {
  enabled?: boolean;
};

type AccountCreateInput = {
  username_or_url: string;
  display_name: string | null;
  interval_seconds: number | null;
  enabled: boolean;
};

export type CompetitorKeywordPatch = {
  enabled?: boolean;
  term?: string;
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

function nowIso(): string {
  return new Date().toISOString();
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
  const [settings, accounts, attempts, tasks] = await Promise.all([
    getSettings(),
    listAccounts(),
    listRecentAttempts(30),
    listRecentTasks(20),
  ]);
  return { settings, accounts, attempts, tasks };
}

export async function loadNonMainstreamDashboard(): Promise<NonMainstreamDashboardPayload> {
  const [settings, sources] = await Promise.all([getNonMainstreamSettings(), listNonMainstreamSources()]);
  return { settings, sources };
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

export async function listAccounts(): Promise<Account[]> {
  const { data, error } = await supabase()
    .from('x_capture_accounts')
    .select(
      [
        'id',
        'username',
        'username_lower',
        'display_name',
        'profile_url',
        'enabled',
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
  const { data, error } = await supabase()
    .from('x_capture_accounts')
    .upsert(
      {
        username,
        username_lower: username.toLowerCase(),
        display_name: displayName,
        profile_url: `https://x.com/${username}`,
        enabled: input.enabled,
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
  if ('interval_seconds' in patch) {
    payload.interval_seconds = patch.interval_seconds ?? null;
  }
  if ('enabled' in patch && patch.enabled !== undefined) {
    payload.enabled = patch.enabled;
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

async function listRecentTasks(limit: number): Promise<TaskItem[]> {
  const { data, error } = await supabase()
    .from('tasks')
    .select('id,source_item_id,source_url,title,content,status,created_at')
    .eq('source', 'x')
    .order('created_at', { ascending: false })
    .limit(limit);
  raise(error);
  return (data ?? []) as TaskItem[];
}

export async function listPromptTemplates(): Promise<PromptTemplate[]> {
  const { data, error } = await supabase()
    .from('prompt_templates')
    .select('template_key,display_name,active_version_id,updated_at')
    .order('template_key', { ascending: true });
  raise(error);
  return (data ?? []) as unknown as PromptTemplate[];
}

export async function listPromptVersions(templateKey: string): Promise<PromptVersion[]> {
  const { data, error } = await supabase()
    .from('prompt_template_versions')
    .select('id,template_key,version_number,content,note,created_at,published_at')
    .eq('template_key', templateKey)
    .order('version_number', { ascending: false });
  raise(error);
  return (data ?? []) as unknown as PromptVersion[];
}

export async function createPromptVersion(
  templateKey: string,
  content: string,
  note: string | null,
): Promise<PromptVersion> {
  const versions = await listPromptVersions(templateKey);
  const nextVersion = versions.reduce((max, item) => Math.max(max, item.version_number), 0) + 1;
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
    .select('template_key,display_name,active_version_id,updated_at')
    .single();
  raise(error);
  return assertData(data as PromptTemplate | null, '发布 Prompt 版本失败');
}

export function normalizeCompetitorKeyword(value: string): string {
  return value.trim().replace(/\s+/g, ' ').toLowerCase();
}

export async function listCompetitorFilterKeywords(): Promise<CompetitorFilterKeyword[]> {
  const { data, error } = await supabase()
    .from('competitor_filter_keywords')
    .select('id,term,term_normalized,enabled,created_at,updated_at')
    .order('enabled', { ascending: false })
    .order('term', { ascending: true });
  raise(error);
  return (data ?? []) as unknown as CompetitorFilterKeyword[];
}

export async function createCompetitorFilterKeywords(rawText: string): Promise<CompetitorFilterKeyword[]> {
  const rows = rawText
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((term) => ({ term, term_normalized: normalizeCompetitorKeyword(term), enabled: true, updated_at: nowIso() }))
    .filter((row, index, all) => row.term_normalized && all.findIndex((item) => item.term_normalized === row.term_normalized) === index);

  if (rows.length === 0) {
    return [];
  }

  const { data, error } = await supabase()
    .from('competitor_filter_keywords')
    .upsert(rows, { onConflict: 'term_normalized' })
    .select('id,term,term_normalized,enabled,created_at,updated_at');
  raise(error);
  return (data ?? []) as unknown as CompetitorFilterKeyword[];
}

export async function updateCompetitorFilterKeyword(
  id: number,
  patch: CompetitorKeywordPatch,
): Promise<CompetitorFilterKeyword> {
  const payload: Record<string, string | boolean> = {
    updated_at: nowIso(),
  };
  if ('enabled' in patch && patch.enabled !== undefined) {
    payload.enabled = patch.enabled;
  }
  if ('term' in patch && patch.term !== undefined) {
    payload.term = patch.term.trim();
    payload.term_normalized = normalizeCompetitorKeyword(patch.term);
  }
  const { data, error } = await supabase()
    .from('competitor_filter_keywords')
    .update(payload)
    .eq('id', id)
    .select('id,term,term_normalized,enabled,created_at,updated_at')
    .single();
  raise(error);
  return assertData(data as CompetitorFilterKeyword | null, '更新排除词失败');
}

export async function deleteCompetitorFilterKeyword(id: number): Promise<void> {
  const { error } = await supabase().from('competitor_filter_keywords').delete().eq('id', id);
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
