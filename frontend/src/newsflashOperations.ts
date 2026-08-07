import { getCurrentSession } from './xCaptureStore';

export type Person = {
  person_key: string;
  display_name: string;
  duty_enabled: boolean;
  contributor_enabled: boolean;
  active: boolean;
  aliases: string[];
};

export type FirstPublication = {
  status: 'ready' | 'unmatched' | 'insufficient' | 'excluded';
  label: string;
  sources: string[];
  event_id?: string;
  published_at?: string;
};

export type NewsflashRow = {
  source_item_id: string;
  source_url: string | null;
  title: string | null;
  published_at: string | null;
  operator_raw: string | null;
  publisher_kind: string | null;
  publisher_person_key: string | null;
  publisher_person_name: string | null;
  publisher_locked: boolean;
  view_count: number | null;
  is_pushed: boolean | null;
  pushed_at: string | null;
  is_contribution: boolean;
  contributor_person_key: string | null;
  contributor_name: string | null;
  contribution_type: 'regular' | 'night' | 'ppp';
  source_snapshot_at: string | null;
  first_publication: FirstPublication;
};

export type NewsflashPage = {
  items: NewsflashRow[];
  page: number;
  page_size: number;
  total: number;
  pages: number;
};

export type SchedulePayload = {
  month: string;
  days: Array<{ date: string; mode: 'two' | 'three' }>;
  assignments: Array<{ duty_date: string; shift_key: string; person_key: string | null; display_name: string | null }>;
  weeks: Array<{ week_start: string; week_end: string; report_month: string | null; report_month_manual: boolean }>;
  people: Person[];
};

export type SummaryMetric = {
  published_count: number;
  pushed_count: number;
  average_views: number | null;
  pushed_views: number | null;
  view_coverage: { known: number; total: number };
  push_coverage?: { known: number; total: number };
  push_view_coverage?: { known: number; total: number };
};

export type SummaryPayload = {
  period: string;
  weeks: string[];
  rows: Array<SummaryMetric & { date: string; shift_key: string; shift_label: string; person_key: string; person_name: string; is_ai: boolean }>;
  people: Array<SummaryMetric & { person_key: string; person_name: string }>;
  unassigned_count: number;
};

export type ContributionsPayload = {
  week_start: string;
  week_end: string;
  in_progress: boolean;
  groups: Array<{
    person_key: string;
    display_name: string;
    count: number;
    total_views: number;
    average_views: number | null;
    view_coverage: { known: number; total: number };
    items: Array<{
      source_item_id: string;
      source_url: string | null;
      title: string | null;
      published_at: string | null;
      view_count: number | null;
      contribution_type: string;
      contributor_person_key: string;
      first_publication: FirstPublication;
    }>;
  }>;
};

export type EventPage = {
  page: number;
  pages: number;
  total: number;
  page_size: number;
  items: Array<{
    event_id: string;
    representative_title: string | null;
    event_time: string | null;
    first_published_at: string | null;
    first_source_label: string;
    first_sources: string[];
    source_count: number;
    competitor_source_count: number;
    has_odaily: number;
    odaily_published_at: string | null;
    sources: Array<{ source: string; source_item_id: string; source_url: string | null; title: string | null; published_at: string | null }>;
  }>;
};

function baseUrl(): string {
  return String(import.meta.env.VITE_CONSOLE_API_BASE_URL || import.meta.env.VITE_EDITOR_PLUGIN_API_BASE_URL || 'https://47.76.243.147.sslip.io').replace(/\/+$/, '');
}

export async function newsflashOperations<T>(action: string, payload: Record<string, unknown> = {}): Promise<T> {
  const session = await getCurrentSession();
  if (!session?.access_token) throw new Error('登录状态已失效，请重新登录');
  const response = await fetch(`${baseUrl()}/console/newsflash-operations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${session.access_token}` },
    body: JSON.stringify({ action, ...payload }),
  });
  const result = (await response.json().catch(() => null)) as { ok?: boolean; data?: T; message?: string } | null;
  if (!response.ok || !result?.ok) throw new Error(result?.message || `控制台服务请求失败：${response.status}`);
  return result.data as T;
}
