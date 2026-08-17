export type Session = {
  access_token: string;
  expires_at: number;
  user: { id: string | null; email: string; user_metadata?: { display_name?: string } };
};

type ApiError = { message: string } | null;
type ApiResult<T> = { data: T | null; error: ApiError };
type Filter = { op: string; column: string; value: unknown };
const SESSION_KEY = 'odaily.local.session';
const listeners = new Set<(event: string, session: Session | null) => void>();

function baseUrl(): string {
  return String(import.meta.env.VITE_CONSOLE_API_BASE_URL || import.meta.env.VITE_EDITOR_PLUGIN_API_BASE_URL || 'https://api.odaily.uk').replace(/\/+$/, '');
}

function loadSession(): Session | null {
  const raw = localStorage.getItem(SESSION_KEY);
  if (!raw) return null;
  try {
    const session = JSON.parse(raw) as Session;
    if (!session.access_token || session.expires_at <= Math.floor(Date.now() / 1000)) {
      localStorage.removeItem(SESSION_KEY);
      return null;
    }
    return session;
  } catch {
    localStorage.removeItem(SESSION_KEY);
    return null;
  }
}

async function request<T>(path: string, body: unknown, token?: string): Promise<ApiResult<T>> {
  try {
    const response = await fetch(`${baseUrl()}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify(body),
    });
    const payload = (await response.json()) as { ok?: boolean; data?: T; message?: string };
    if (!response.ok || !payload.ok) return { data: null, error: { message: payload.message || `HTTP ${response.status}` } };
    return { data: (payload.data ?? null) as T | null, error: null };
  } catch (error) {
    return { data: null, error: { message: error instanceof Error ? error.message : String(error) } };
  }
}

class LocalQueryBuilder implements PromiseLike<ApiResult<any>> {
  private operation = 'select'; private selected = '*'; private filters: Filter[] = [];
  private orders: Array<{ column: string; ascending: boolean; nulls_first?: boolean }> = [];
  private rowLimit: number | null = null; private offset = 0; private data: unknown = null;
  private onConflict = ''; private resultMode: 'many' | 'single' | 'maybeSingle' = 'many';
  constructor(private table: string) {}
  select(columns = '*'): this { this.selected = columns; return this; }
  insert(data: unknown): this { this.operation = 'insert'; this.data = data; return this; }
  update(data: unknown): this { this.operation = 'update'; this.data = data; return this; }
  upsert(data: unknown, options?: { onConflict?: string }): this { this.operation = 'upsert'; this.data = data; this.onConflict = options?.onConflict || ''; return this; }
  delete(): this { this.operation = 'delete'; return this; }
  eq(column: string, value: unknown): this { this.filters.push({ op: 'eq', column, value }); return this; }
  neq(column: string, value: unknown): this { this.filters.push({ op: 'neq', column, value }); return this; }
  gte(column: string, value: unknown): this { this.filters.push({ op: 'gte', column, value }); return this; }
  lte(column: string, value: unknown): this { this.filters.push({ op: 'lte', column, value }); return this; }
  gt(column: string, value: unknown): this { this.filters.push({ op: 'gt', column, value }); return this; }
  lt(column: string, value: unknown): this { this.filters.push({ op: 'lt', column, value }); return this; }
  is(column: string, value: unknown): this { this.filters.push({ op: 'is', column, value }); return this; }
  in(column: string, value: unknown[]): this { this.filters.push({ op: 'in', column, value }); return this; }
  order(column: string, options?: { ascending?: boolean; nullsFirst?: boolean }): this { this.orders.push({ column, ascending: options?.ascending !== false, nulls_first: options?.nullsFirst }); return this; }
  limit(value: number): this { this.rowLimit = value; return this; }
  range(from: number, to: number): this { this.offset = from; this.rowLimit = Math.max(0, to - from + 1); return this; }
  single(): this { this.resultMode = 'single'; this.rowLimit = 1; return this; }
  maybeSingle(): this { this.resultMode = 'maybeSingle'; this.rowLimit = 1; return this; }
  private async execute(): Promise<ApiResult<any>> {
    const session = loadSession();
    if (!session) return { data: null, error: { message: '登录状态已失效，请重新登录' } };
    const result = await request<unknown[]>('/console/data', { table: this.table, operation: this.operation, select: this.selected, filters: this.filters, orders: this.orders, limit: this.rowLimit, offset: this.offset, data: this.data, on_conflict: this.onConflict }, session.access_token);
    if (result.error) return result;
    const rows = result.data || [];
    if (this.resultMode === 'single') return rows.length === 1 ? { data: rows[0], error: null } : { data: null, error: { message: `Expected one row, got ${rows.length}` } };
    if (this.resultMode === 'maybeSingle') return rows.length <= 1 ? { data: rows[0] ?? null, error: null } : { data: null, error: { message: `Expected at most one row, got ${rows.length}` } };
    return { data: rows, error: null };
  }
  then<TResult1 = ApiResult<any>, TResult2 = never>(onfulfilled?: ((value: ApiResult<any>) => TResult1 | PromiseLike<TResult1>) | null, onrejected?: ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null): PromiseLike<TResult1 | TResult2> { return this.execute().then(onfulfilled, onrejected); }
}

export type LocalApiClient = ReturnType<typeof createLocalClient>;
export function createLocalClient() {
  return {
    auth: {
      async getSession() { return { data: { session: loadSession() }, error: null }; },
      onAuthStateChange(listener: (event: string, session: Session | null) => void) { listeners.add(listener); return { data: { subscription: { unsubscribe: () => listeners.delete(listener) } } }; },
      async signInWithPassword(credentials: { email: string; password: string }) { const result = await request<Session>('/plugin/auth/login', credentials); if (!result.error && result.data) { localStorage.setItem(SESSION_KEY, JSON.stringify(result.data)); listeners.forEach((listener) => listener('SIGNED_IN', result.data)); } return { error: result.error }; },
      async signOut() { const session = loadSession(); if (session) await request('/plugin/auth/logout', {}, session.access_token); localStorage.removeItem(SESSION_KEY); listeners.forEach((listener) => listener('SIGNED_OUT', null)); return { error: null }; },
    },
    from(table: string) { return new LocalQueryBuilder(table); },
  };
}
