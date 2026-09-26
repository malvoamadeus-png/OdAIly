import { type FormEvent, useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronLeft, ChevronRight, CircleAlert, ExternalLink, Flame, Plus, Power, RefreshCcw, Rocket, Search, TrendingUp } from 'lucide-react';
import { HotTopicPanel } from './HotTopicPanel';
import {
  addXAgentAccount,
  getXAgentDashboard,
  getXAgentMarketSentimentDetail,
  getXAgentProjectPromotionDetail,
  listXAgentAccounts,
  listXAgentMarketSentiment,
  listXAgentProjectPromotion,
  retryXAgentFailedJobs,
  updateXAgentSubscriptions,
  type XAgentAccount,
  type XAgentAccountModule,
  type XAgentDashboard,
  type XAgentMarketSentimentDetailItem,
  type XAgentMarketSentimentItem,
  type XAgentPage,
  type XAgentProjectPromotionDetailItem,
  type XAgentProjectPromotionItem,
  type XAgentSubscriptionPatch,
} from './xCaptureStore';

type XAgentTab = 'hot_topic' | 'auto_newsflash' | 'accounts' | 'market_sentiment' | 'project_promotion';
type SubscriptionField = keyof XAgentSubscriptionPatch;
type EnabledFilter = 'all' | 'enabled' | 'disabled';

const PAGE_SIZE = 50;
const MARKET_WINDOW_OPTIONS = [
  { value: '1h', label: '1 小时' },
  { value: '24h', label: '24 小时' },
  { value: '7d', label: '7 天' },
];
const PROJECT_WINDOW_OPTIONS = [
  { value: '24h', label: '24 小时' },
  { value: '7d', label: '7 天' },
  { value: '30d', label: '30 天' },
];
const SENTIMENT_OPTIONS = ['极度狂热', '偏多/乐观', '中性/分歧', '偏空/谨慎', '极度恐慌'];
const MODULE_FIELD: Record<XAgentAccountModule, SubscriptionField> = {
  hot_topic: 'hotTopicEnabled',
  market_sentiment: 'marketSentimentEnabled',
  project_promotion: 'projectPromotionEnabled',
};
const DASHBOARD_SUBSCRIPTION_FIELD: Record<SubscriptionField, 'hotTopicEnabled' | 'marketSentimentEnabled' | 'projectPromotionEnabled'> = {
  hotTopicEnabled: 'hotTopicEnabled',
  marketSentimentEnabled: 'marketSentimentEnabled',
  projectPromotionEnabled: 'projectPromotionEnabled',
};
const SUBSCRIPTION_FIELDS: SubscriptionField[] = [
  'hotTopicEnabled',
  'marketSentimentEnabled',
  'projectPromotionEnabled',
];

function formatTime(value: string | null | undefined): string {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

function accountUrl(account: XAgentAccount): string {
  return account.profileUrl || `https://x.com/${account.screenName}`;
}

function combineErrors(account: XAgentAccount): string | null {
  return account.lastAnalysisError || account.lastError || null;
}

function subscriptionKey(screenName: string, field: SubscriptionField): string {
  return `${screenName}:${field}`;
}

function mergeSubscriptionResponse(
  current: XAgentAccount,
  updated: XAgentAccount,
  changedField: SubscriptionField,
): XAgentAccount {
  const merged = { ...current, ...updated };
  SUBSCRIPTION_FIELDS.forEach((field) => {
    if (field !== changedField) merged[field] = current[field];
  });
  return merged;
}

function pageCount(total: number): number {
  return Math.max(1, Math.ceil(total / PAGE_SIZE));
}

function pageNumber(offset: number): number {
  return Math.floor(offset / PAGE_SIZE) + 1;
}

function PageControls({
  offset,
  total,
  disabled,
  onChange,
}: {
  offset: number;
  total: number;
  disabled: boolean;
  onChange: (offset: number) => void;
}) {
  const page = pageNumber(offset);
  const pages = pageCount(total);
  return (
    <div className="xAgentPager">
      <span>{total.toLocaleString()} 条 · 第 {page} / {pages} 页</span>
      <div>
        <button className="iconButton" type="button" title="上一页" aria-label="上一页" disabled={disabled || offset === 0} onClick={() => onChange(Math.max(0, offset - PAGE_SIZE))}><ChevronLeft size={17} /></button>
        <button className="iconButton" type="button" title="下一页" aria-label="下一页" disabled={disabled || offset + PAGE_SIZE >= total} onClick={() => onChange(offset + PAGE_SIZE)}><ChevronRight size={17} /></button>
      </div>
    </div>
  );
}

function AccountSubscriptionToggle({
  account,
  field,
  label,
  pending,
  onChange,
}: {
  account: XAgentAccount;
  field: SubscriptionField;
  label: string;
  pending: boolean;
  onChange: (account: XAgentAccount, field: SubscriptionField, nextValue: boolean) => void;
}) {
  return (
    <label className="xAgentSubscriptionToggle">
      <input
        type="checkbox"
        aria-label={`@${account.screenName} 的${label}订阅`}
        aria-busy={pending}
        checked={Boolean(account[field])}
        disabled={pending}
        onChange={(event) => onChange(account, field, event.target.checked)}
      />
      <span>{label}</span>
    </label>
  );
}

function MarketDetail({
  detail,
  loading,
  error,
  offset,
  onOffsetChange,
}: {
  detail: XAgentPage<XAgentMarketSentimentDetailItem> | null;
  loading: boolean;
  error: string;
  offset: number;
  onOffsetChange: (offset: number) => void;
}) {
  if (loading) return <div className="xAgentDetailLoading">正在读取原帖…</div>;
  if (error) return <div className="notice error xAgentDetailNotice"><CircleAlert size={16} /> {error}</div>;
  if (!detail?.items.length) return <div className="xAgentDetailLoading">当前窗口暂无原帖。</div>;
  return (
    <div className="xAgentPostList">
      {detail.items.map((item) => (
        <article className="xAgentPostItem" key={item.tweetId}>
          <div className="xAgentPostMeta"><strong>@{item.account}</strong><span>{item.sentiment}</span><time>{formatTime(item.postedAt)}</time>{item.sourceUrl && <a href={item.sourceUrl} target="_blank" rel="noreferrer" title="打开原帖" aria-label={`打开 ${item.account} 的原帖`}><ExternalLink size={15} /></a>}</div>
          <p>{item.sourceText}</p>
          {item.reason && <footer><span>{item.reason}</span></footer>}
        </article>
      ))}
      <PageControls offset={offset} total={detail.total} disabled={loading} onChange={onOffsetChange} />
    </div>
  );
}

function ProjectDetail({
  detail,
  loading,
  error,
  offset,
  onOffsetChange,
}: {
  detail: XAgentPage<XAgentProjectPromotionDetailItem> | null;
  loading: boolean;
  error: string;
  offset: number;
  onOffsetChange: (offset: number) => void;
}) {
  if (loading) return <div className="xAgentDetailLoading">正在读取原帖…</div>;
  if (error) return <div className="notice error xAgentDetailNotice"><CircleAlert size={16} /> {error}</div>;
  if (!detail?.items.length) return <div className="xAgentDetailLoading">当前窗口暂无原帖。</div>;
  return (
    <div className="xAgentPostList">
      {detail.items.map((item) => (
        <article className="xAgentPostItem" key={item.id}>
          <div className="xAgentPostMeta"><strong>@{item.account}</strong><time>{formatTime(item.lastMentionedAt)}</time>{item.sourceUrl && <a href={item.sourceUrl} target="_blank" rel="noreferrer" title="打开原帖" aria-label={`打开 ${item.account} 的原帖`}><ExternalLink size={15} /></a>}</div>
          <p>{item.sourceText}</p>
          {item.logic && <footer><span>{item.logic}</span></footer>}
        </article>
      ))}
      <PageControls offset={offset} total={detail.total} disabled={loading} onChange={onOffsetChange} />
    </div>
  );
}

export function XAgentPanel({ refreshToken = 0 }: { refreshToken?: number }) {
  const [tab, setTab] = useState<XAgentTab>('accounts');
  const [hotTopicRefreshToken, setHotTopicRefreshToken] = useState(0);
  const [dashboard, setDashboard] = useState<XAgentDashboard | null>(null);
  const [dashboardError, setDashboardError] = useState('');

  const [accounts, setAccounts] = useState<XAgentAccount[]>([]);
  const [accountsTotal, setAccountsTotal] = useState(0);
  const [accountsLoading, setAccountsLoading] = useState(false);
  const [accountsError, setAccountsError] = useState('');
  const [accountQuery, setAccountQuery] = useState('');
  const [accountModule, setAccountModule] = useState<'all' | XAgentAccountModule>('all');
  const [accountEnabled, setAccountEnabled] = useState<EnabledFilter>('all');
  const [accountOffset, setAccountOffset] = useState(0);
  const [accountReload, setAccountReload] = useState(0);
  const [batchSaving, setBatchSaving] = useState<SubscriptionField | null>(null);
  const [retryingFailed, setRetryingFailed] = useState(false);
  const [retryFeedback, setRetryFeedback] = useState('');
  const [retryFeedbackError, setRetryFeedbackError] = useState(false);
  const [newScreenName, setNewScreenName] = useState('');
  const [newDisplayName, setNewDisplayName] = useState('');
  const [addingAccount, setAddingAccount] = useState(false);

  const [marketItems, setMarketItems] = useState<XAgentMarketSentimentItem[]>([]);
  const [marketTotal, setMarketTotal] = useState(0);
  const [marketLoading, setMarketLoading] = useState(false);
  const [marketError, setMarketError] = useState('');
  const [marketWindow, setMarketWindow] = useState('24h');
  const [marketQuery, setMarketQuery] = useState('');
  const [marketSentiment, setMarketSentiment] = useState('all');
  const [marketOffset, setMarketOffset] = useState(0);
  const [marketReload, setMarketReload] = useState(0);
  const [marketSelectedKey, setMarketSelectedKey] = useState<string | null>(null);
  const [marketDetail, setMarketDetail] = useState<XAgentPage<XAgentMarketSentimentDetailItem> | null>(null);
  const [marketDetailLoading, setMarketDetailLoading] = useState(false);
  const [marketDetailError, setMarketDetailError] = useState('');
  const [marketDetailOffset, setMarketDetailOffset] = useState(0);

  const [projectItems, setProjectItems] = useState<XAgentProjectPromotionItem[]>([]);
  const [projectTotal, setProjectTotal] = useState(0);
  const [projectLoading, setProjectLoading] = useState(false);
  const [projectError, setProjectError] = useState('');
  const [projectWindow, setProjectWindow] = useState('24h');
  const [projectQuery, setProjectQuery] = useState('');
  const [projectOffset, setProjectOffset] = useState(0);
  const [projectReload, setProjectReload] = useState(0);
  const [projectSelectedKey, setProjectSelectedKey] = useState<string | null>(null);
  const [projectDetail, setProjectDetail] = useState<XAgentPage<XAgentProjectPromotionDetailItem> | null>(null);
  const [projectDetailLoading, setProjectDetailLoading] = useState(false);
  const [projectDetailError, setProjectDetailError] = useState('');
  const [projectDetailOffset, setProjectDetailOffset] = useState(0);

  const [pendingSubscriptionKeys, setPendingSubscriptionKeys] = useState<Set<string>>(() => new Set());
  const pendingSubscriptionKeysRef = useRef(new Set<string>());
  const subscriptionVersions = useRef(new Map<string, number>());
  const dashboardRequestVersion = useRef(0);
  const accountRequestVersion = useRef(0);
  const accountRefreshTimer = useRef<number | null>(null);
  const lastExternalRefreshToken = useRef(refreshToken);
  const mounted = useRef(true);

  async function refreshDashboard() {
    const requestVersion = dashboardRequestVersion.current + 1;
    dashboardRequestVersion.current = requestVersion;
    try {
      const nextDashboard = await getXAgentDashboard();
      if (mounted.current && dashboardRequestVersion.current === requestVersion) {
        setDashboard(nextDashboard);
        setDashboardError('');
      }
    } catch (cause) {
      if (mounted.current && dashboardRequestVersion.current === requestVersion) {
        setDashboardError(cause instanceof Error ? cause.message : 'X Agent 状态暂不可用');
      }
    }
  }

  function invalidateAccountPage() {
    accountRequestVersion.current += 1;
    setAccountsLoading(false);
  }

  function refreshFilteredAccountPageSoon(field: SubscriptionField) {
    if (accountEnabled === 'all' || accountModule === 'all' || MODULE_FIELD[accountModule] !== field) return;
    invalidateAccountPage();
    if (!mounted.current) return;
    if (accountRefreshTimer.current !== null) window.clearTimeout(accountRefreshTimer.current);
    accountRefreshTimer.current = window.setTimeout(() => {
      accountRefreshTimer.current = null;
      if (mounted.current && pendingSubscriptionKeysRef.current.size === 0) {
        setAccountReload((value) => value + 1);
      }
    }, 120);
  }

  function updateDashboardSubscriptionCount(field: SubscriptionField, delta: number) {
    if (delta === 0) return;
    dashboardRequestVersion.current += 1;
    const dashboardField = DASHBOARD_SUBSCRIPTION_FIELD[field];
    setDashboard((current) => {
      if (!current) return current;
      return {
        ...current,
        accounts: {
          ...current.accounts,
          [dashboardField]: Math.max(0, current.accounts[dashboardField] + delta),
        },
      };
    });
  }

  useEffect(() => {
    void refreshDashboard();
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (accountRefreshTimer.current !== null) window.clearTimeout(accountRefreshTimer.current);
    };
  }, []);

  useEffect(() => {
    if (lastExternalRefreshToken.current === refreshToken) return;
    lastExternalRefreshToken.current = refreshToken;
    refreshActiveTab();
  }, [refreshToken]);

  useEffect(() => {
    if (tab !== 'accounts') return;
    let active = true;
    const requestVersion = accountRequestVersion.current + 1;
    accountRequestVersion.current = requestVersion;
    const timer = window.setTimeout(() => {
      if (!active || accountRequestVersion.current !== requestVersion) return;
      setAccountsLoading(true);
      void listXAgentAccounts({
        query: accountQuery.trim(),
        module: accountModule === 'all' ? undefined : accountModule,
        enabled: accountEnabled === 'all' ? undefined : accountEnabled === 'enabled',
        offset: accountOffset,
        limit: PAGE_SIZE,
      }).then((page) => {
        if (!active || accountRequestVersion.current !== requestVersion) return;
        if (page.total > 0 && accountOffset >= page.total) {
          setAccountOffset(Math.floor((page.total - 1) / PAGE_SIZE) * PAGE_SIZE);
          return;
        }
        setAccounts(page.items);
        setAccountsTotal(page.total);
        setAccountsError('');
      }).catch((cause) => {
        if (active && accountRequestVersion.current === requestVersion) {
          setAccountsError(cause instanceof Error ? cause.message : '无法读取 X Agent 账号');
        }
      }).finally(() => {
        if (active && accountRequestVersion.current === requestVersion) setAccountsLoading(false);
      });
    }, 250);
    return () => { active = false; window.clearTimeout(timer); };
  }, [tab, accountQuery, accountModule, accountEnabled, accountOffset, accountReload]);

  useEffect(() => {
    if (tab !== 'market_sentiment') return;
    let active = true;
    const timer = window.setTimeout(() => {
      setMarketLoading(true);
      void listXAgentMarketSentiment({
        window: marketWindow,
        query: marketQuery.trim(),
        sentiment: marketSentiment === 'all' ? undefined : marketSentiment,
        offset: marketOffset,
        limit: PAGE_SIZE,
      }).then((page) => {
        if (!active) return;
        setMarketItems(page.items);
        setMarketTotal(page.total);
        setMarketError('');
      }).catch((cause) => {
        if (active) setMarketError(cause instanceof Error ? cause.message : '无法读取市场情绪');
      }).finally(() => {
        if (active) setMarketLoading(false);
      });
    }, 250);
    return () => { active = false; window.clearTimeout(timer); };
  }, [tab, marketWindow, marketQuery, marketSentiment, marketOffset, marketReload]);

  useEffect(() => {
    if (tab !== 'project_promotion') return;
    let active = true;
    const timer = window.setTimeout(() => {
      setProjectLoading(true);
      void listXAgentProjectPromotion({
        window: projectWindow,
        query: projectQuery.trim(),
        offset: projectOffset,
        limit: PAGE_SIZE,
      }).then((page) => {
        if (!active) return;
        setProjectItems(page.items);
        setProjectTotal(page.total);
        setProjectError('');
      }).catch((cause) => {
        if (active) setProjectError(cause instanceof Error ? cause.message : '无法读取项目推介');
      }).finally(() => {
        if (active) setProjectLoading(false);
      });
    }, 250);
    return () => { active = false; window.clearTimeout(timer); };
  }, [tab, projectWindow, projectQuery, projectOffset, projectReload]);

  useEffect(() => {
    if (tab !== 'market_sentiment' || !marketSelectedKey) {
      setMarketDetail(null);
      return;
    }
    let active = true;
    setMarketDetailLoading(true);
    setMarketDetail(null);
    setMarketDetailError('');
    void getXAgentMarketSentimentDetail({ instrumentKey: marketSelectedKey, window: marketWindow, offset: marketDetailOffset, limit: PAGE_SIZE })
      .then((page) => {
        if (!active) return;
        setMarketDetail(page);
        setMarketDetailError('');
      })
      .catch((cause) => {
        if (active) setMarketDetailError(cause instanceof Error ? cause.message : '无法读取原帖');
      })
      .finally(() => { if (active) setMarketDetailLoading(false); });
    return () => { active = false; };
  }, [tab, marketSelectedKey, marketWindow, marketDetailOffset]);

  useEffect(() => {
    if (tab !== 'project_promotion' || !projectSelectedKey) {
      setProjectDetail(null);
      return;
    }
    let active = true;
    setProjectDetailLoading(true);
    setProjectDetail(null);
    setProjectDetailError('');
    void getXAgentProjectPromotionDetail({ identityKey: projectSelectedKey, window: projectWindow, offset: projectDetailOffset, limit: PAGE_SIZE })
      .then((page) => {
        if (!active) return;
        setProjectDetail(page);
        setProjectDetailError('');
      })
      .catch((cause) => {
        if (active) setProjectDetailError(cause instanceof Error ? cause.message : '无法读取原帖');
      })
      .finally(() => { if (active) setProjectDetailLoading(false); });
    return () => { active = false; };
  }, [tab, projectSelectedKey, projectWindow, projectDetailOffset]);

  function setSubscriptionPending(key: string, pending: boolean) {
    if (pending) pendingSubscriptionKeysRef.current.add(key);
    else pendingSubscriptionKeysRef.current.delete(key);
    setPendingSubscriptionKeys((current) => {
      const next = new Set(current);
      if (pending) next.add(key);
      else next.delete(key);
      return next;
    });
  }

  function saveSubscriptionChange(account: XAgentAccount, field: SubscriptionField, nextValue: boolean) {
    if (batchSaving !== null) return;
    const key = subscriptionKey(account.screenName, field);
    const previousValue = Boolean(account[field]);
    const nextVersion = (subscriptionVersions.current.get(key) || 0) + 1;
    subscriptionVersions.current.set(key, nextVersion);
    invalidateAccountPage();
    setAccounts((current) => current.map((item) => item.screenName === account.screenName ? { ...item, [field]: nextValue } : item));
    setSubscriptionPending(key, true);
    const patch: XAgentSubscriptionPatch = { [field]: nextValue };
    void updateXAgentSubscriptions([account.screenName], patch).then((result) => {
      if (subscriptionVersions.current.get(key) !== nextVersion || !mounted.current) return;
      const updated = result.items.find((item) => item.screenName === account.screenName);
      if (updated) {
        setAccounts((current) => current.map((item) => item.screenName === account.screenName
          ? mergeSubscriptionResponse(item, updated, field)
          : item));
      }
      updateDashboardSubscriptionCount(field, previousValue === nextValue ? 0 : nextValue ? 1 : -1);
      setSubscriptionPending(key, false);
      refreshFilteredAccountPageSoon(field);
    }).catch((cause) => {
      if (subscriptionVersions.current.get(key) !== nextVersion || !mounted.current) return;
      setAccounts((current) => current.map((item) => item.screenName === account.screenName ? { ...item, [field]: previousValue } : item));
      setAccountsError(cause instanceof Error ? cause.message : '订阅开关保存失败');
      setSubscriptionPending(key, false);
      refreshFilteredAccountPageSoon(field);
    });
  }

  async function applyBatchSubscription(field: SubscriptionField, nextValue: boolean) {
    if (!accounts.length || batchSaving || pendingSubscriptionKeys.size > 0) return;
    const screenNames = accounts.map((account) => account.screenName);
    const previousValues = new Map(accounts.map((account) => [account.screenName, Boolean(account[field])]));
    const keys = screenNames.map((screenName) => subscriptionKey(screenName, field));
    invalidateAccountPage();
    keys.forEach((key) => {
      subscriptionVersions.current.set(key, (subscriptionVersions.current.get(key) || 0) + 1);
    });
    setAccounts((current) => current.map((account) => ({ ...account, [field]: nextValue })));
    setAccountsError('');
    setBatchSaving(field);
    try {
      const result = await updateXAgentSubscriptions(screenNames, { [field]: nextValue });
      if (!mounted.current) return;
      const updatedByName = new Map(result.items.map((item) => [item.screenName, item]));
      setAccounts((current) => current.map((account) => {
        const updated = updatedByName.get(account.screenName);
        return updated ? { ...account, ...updated } : account;
      }));
      const changedCount = screenNames.reduce((count, screenName) => count + (previousValues.get(screenName) === nextValue ? 0 : nextValue ? 1 : -1), 0);
      updateDashboardSubscriptionCount(field, changedCount);
      setAccountsError('');
      refreshFilteredAccountPageSoon(field);
    } catch (cause) {
      if (!mounted.current) return;
      setAccounts((current) => current.map((account) => previousValues.has(account.screenName)
        ? { ...account, [field]: previousValues.get(account.screenName) }
        : account));
      setAccountsError(cause instanceof Error ? cause.message : '当前页订阅开关保存失败');
      refreshFilteredAccountPageSoon(field);
    } finally {
      if (mounted.current) setBatchSaving(null);
    }
  }

  async function retryFailedJobs() {
    if (retryingFailed) return;
    const module = tab === 'market_sentiment'
      ? 'market_sentiment'
      : tab === 'project_promotion'
        ? 'project_promotion'
        : '';
    setRetryingFailed(true);
    setRetryFeedback('');
    setRetryFeedbackError(false);
    try {
      const result = await retryXAgentFailedJobs(module, 100);
      if (!mounted.current) return;
      setRetryFeedback(result.requeued > 0
        ? `已将 ${result.requeued.toLocaleString()} 个失败任务重新加入队列。`
        : '当前范围内没有可重试的失败任务。');
      void refreshDashboard();
      if (tab === 'accounts') setAccountReload((value) => value + 1);
      if (tab === 'market_sentiment') setMarketReload((value) => value + 1);
      if (tab === 'project_promotion') setProjectReload((value) => value + 1);
    } catch (cause) {
      if (!mounted.current) return;
      setRetryFeedback(cause instanceof Error ? cause.message : '失败任务重新入队失败');
      setRetryFeedbackError(true);
    } finally {
      if (mounted.current) setRetryingFailed(false);
    }
  }

  async function addAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const screenName = newScreenName.trim();
    if (!screenName) return;
    setAddingAccount(true);
    try {
      await addXAgentAccount(screenName, newDisplayName.trim());
      if (!mounted.current) return;
      setNewScreenName('');
      setNewDisplayName('');
      setAccountOffset(0);
      setAccountReload((value) => value + 1);
      void refreshDashboard();
    } catch (cause) {
      if (mounted.current) setAccountsError(cause instanceof Error ? cause.message : '添加账号失败');
    } finally {
      if (mounted.current) setAddingAccount(false);
    }
  }

  function refreshActiveTab() {
    void refreshDashboard();
    if (tab === 'hot_topic') setHotTopicRefreshToken((value) => value + 1);
    if (tab === 'accounts') setAccountReload((value) => value + 1);
    if (tab === 'market_sentiment') setMarketReload((value) => value + 1);
    if (tab === 'project_promotion') setProjectReload((value) => value + 1);
  }

  function selectTab(nextTab: XAgentTab) {
    setTab(nextTab);
  }

  function openSubscribedAccounts(module: Extract<XAgentAccountModule, 'market_sentiment' | 'project_promotion'>) {
    setAccountQuery('');
    setAccountModule(module);
    setAccountEnabled('enabled');
    setAccountOffset(0);
    setAccounts([]);
    setAccountsTotal(0);
    setTab('accounts');
  }

  function toggleMarketDetail(instrumentKey: string) {
    setMarketSelectedKey((current) => current === instrumentKey ? null : instrumentKey);
    setMarketDetailOffset(0);
  }

  function toggleProjectDetail(identityKey: string) {
    setProjectSelectedKey((current) => current === identityKey ? null : identityKey);
    setProjectDetailOffset(0);
  }

  const batchDisabled = accountsLoading || accounts.length === 0 || batchSaving !== null || pendingSubscriptionKeys.size > 0;
  const showsAnalysisControls = tab === 'accounts' || tab === 'market_sentiment' || tab === 'project_promotion';

  return (
    <section className="xAgentLayout">
      <div className="xAgentTabBar" role="tablist" aria-label="X Agent 模块">
        {([
          ['hot_topic', '热点话题'],
          ['auto_newsflash', '热点自动快讯'],
          ['accounts', '账号目录'],
          ['market_sentiment', '市场情绪'],
          ['project_promotion', '项目推介'],
        ] as Array<[XAgentTab, string]>).map(([key, label]) => (
          <button key={key} className={tab === key ? 'active' : ''} type="button" role="tab" aria-selected={tab === key} onClick={() => selectTab(key)}>{label}</button>
        ))}
        <button className="iconButton xAgentRefresh" type="button" title="刷新当前模块" aria-label="刷新当前模块" onClick={refreshActiveTab}><RefreshCcw size={17} /></button>
      </div>

      {showsAnalysisControls && <div className="xAgentUtilityBar">
        <button className="secondaryButton compact" type="button" disabled={retryingFailed} onClick={() => void retryFailedJobs()}><RefreshCcw size={15} /> {retryingFailed ? '正在重试' : '重试失败任务'}</button>
        {retryFeedback && <span className={retryFeedbackError ? 'xAgentRetryFeedback error' : 'xAgentRetryFeedback'} role={retryFeedbackError ? 'alert' : 'status'}>{retryFeedback}</span>}
      </div>}

      {dashboardError && <div className="notice error"><CircleAlert size={17} /> {dashboardError}</div>}
      {dashboard && showsAnalysisControls && <div className="xAgentSummary">
        <Metric label="账号" value={dashboard.accounts.total} />
        <Metric label="热点话题" value={dashboard.accounts.hotTopicEnabled} />
        <Metric label="情绪订阅账号" value={dashboard.accounts.marketSentimentEnabled} onClick={() => openSubscribedAccounts('market_sentiment')} />
        <Metric label="推介订阅账号" value={dashboard.accounts.projectPromotionEnabled} onClick={() => openSubscribedAccounts('project_promotion')} />
        <Metric label="待处理" value={dashboard.jobs.pending} />
        <Metric label="失败" value={dashboard.jobs.failed + dashboard.accounts.errors} warning={dashboard.jobs.failed + dashboard.accounts.errors > 0} />
      </div>}

      {tab === 'hot_topic' && <HotTopicPanel key={hotTopicRefreshToken} hotTopicAccountCount={dashboard?.accounts.hotTopicEnabled} />}

      {tab === 'auto_newsflash' && <div className="xAgentPlaceholder">由既有专项任务承接</div>}

      {tab === 'accounts' && <section className="xAgentTableSection">
        <div className="sectionHeader">
          <div><h2>账号目录</h2><span>每次采集供已订阅模块共享使用</span></div>
          <span>{accountsLoading ? '加载中' : `${accountsTotal.toLocaleString()} 个账号`}</span>
        </div>
        <form className="xAgentAddAccount" onSubmit={addAccount}>
          <input value={newScreenName} onChange={(event) => setNewScreenName(event.target.value)} placeholder="@username 或 username" aria-label="X 用户名" />
          <input value={newDisplayName} onChange={(event) => setNewDisplayName(event.target.value)} placeholder="显示名，可选" aria-label="显示名" />
          <button className="primaryButton" type="submit" disabled={addingAccount || !newScreenName.trim()}><Plus size={17} /> 添加</button>
        </form>
        <div className="xAgentFilters">
          <label><Search size={16} /><input value={accountQuery} onChange={(event) => { setAccountQuery(event.target.value); setAccountOffset(0); }} placeholder="搜索账号" /></label>
          <div className="segmentedControl" aria-label="模块筛选">
            {([
              ['all', '全部'],
              ['hot_topic', '热点话题'],
              ['market_sentiment', '市场情绪'],
              ['project_promotion', '项目推介'],
            ] as Array<['all' | XAgentAccountModule, string]>).map(([value, label]) => <button key={value} className={accountModule === value ? 'active' : ''} type="button" onClick={() => { setAccountModule(value); setAccountOffset(0); }}>{label}</button>)}
          </div>
          <div className="segmentedControl" aria-label="订阅状态筛选">
            {([
              ['all', '全部'],
              ['enabled', '已打开'],
              ['disabled', '未打开'],
            ] as Array<[EnabledFilter, string]>).map(([value, label]) => <button key={value} className={accountEnabled === value ? 'active' : ''} type="button" onClick={() => { setAccountEnabled(value); setAccountOffset(0); }}>{label}</button>)}
          </div>
        </div>
        <div className="xAgentBatchActions">
          <span>当前页 {accounts.length.toLocaleString()} 个账号</span>
          <button className="secondaryButton compact" type="button" disabled={batchDisabled} onClick={() => void applyBatchSubscription('hotTopicEnabled', true)}><Flame size={15} /> 开启热点</button>
          <button className="secondaryButton compact" type="button" disabled={batchDisabled} onClick={() => void applyBatchSubscription('marketSentimentEnabled', true)}><TrendingUp size={15} /> 开启情绪</button>
          <button className="secondaryButton compact" type="button" disabled={batchDisabled} onClick={() => void applyBatchSubscription('projectPromotionEnabled', true)}><Rocket size={15} /> 开启推介</button>
          {accountModule !== 'all' && <button className="secondaryButton compact danger" type="button" disabled={batchDisabled} onClick={() => void applyBatchSubscription(MODULE_FIELD[accountModule], false)}><Power size={15} /> 全页关闭</button>}
        </div>
        {accountsError && <div className="notice error xAgentInlineNotice"><CircleAlert size={17} /> {accountsError}</div>}
        <div className="xAgentTableWrap" role="table">
          <div className="xAgentAccountHead" role="row"><span>账号</span><span>热点话题</span><span>市场情绪</span><span>项目推介</span><span>最近采集</span><span>最近分析</span></div>
          {!accountsLoading && accounts.length === 0 && <div className="emptyState">没有匹配的账号。</div>}
          {accounts.map((account) => {
            const accountError = combineErrors(account);
            return <div className="xAgentAccountRow" key={account.screenName} role="row">
              <div className="xAgentAccountIdentity"><a href={accountUrl(account)} target="_blank" rel="noreferrer">@{account.screenName}</a>{account.displayName && <span>{account.displayName}</span>}{accountError && <small title={accountError}><CircleAlert size={13} /> {accountError}</small>}</div>
              <AccountSubscriptionToggle account={account} field="hotTopicEnabled" label="热点话题" pending={batchSaving !== null || pendingSubscriptionKeys.has(subscriptionKey(account.screenName, 'hotTopicEnabled'))} onChange={saveSubscriptionChange} />
              <AccountSubscriptionToggle account={account} field="marketSentimentEnabled" label="市场情绪" pending={batchSaving !== null || pendingSubscriptionKeys.has(subscriptionKey(account.screenName, 'marketSentimentEnabled'))} onChange={saveSubscriptionChange} />
              <AccountSubscriptionToggle account={account} field="projectPromotionEnabled" label="项目推介" pending={batchSaving !== null || pendingSubscriptionKeys.has(subscriptionKey(account.screenName, 'projectPromotionEnabled'))} onChange={saveSubscriptionChange} />
              <span>{formatTime(account.lastSuccessAt || account.lastPolledAt)}</span>
              <span>{formatTime(account.lastAnalyzedAt)}</span>
            </div>;
          })}
        </div>
        <PageControls offset={accountOffset} total={accountsTotal} disabled={accountsLoading} onChange={setAccountOffset} />
      </section>}

      {tab === 'market_sentiment' && <section className="xAgentTableSection">
        <div className="sectionHeader"><div><h2>市场情绪</h2><span>标的与态度的内部观察</span></div><span>{marketLoading ? '加载中' : `${marketTotal.toLocaleString()} 个标的`}</span></div>
        <div className="xAgentFilters xAgentResultFilters">
          <label><Search size={16} /><input value={marketQuery} onChange={(event) => { setMarketQuery(event.target.value); setMarketOffset(0); setMarketSelectedKey(null); }} placeholder="搜索标的" /></label>
          <select value={marketWindow} onChange={(event) => { setMarketWindow(event.target.value); setMarketOffset(0); setMarketSelectedKey(null); }}>{MARKET_WINDOW_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select>
          <select value={marketSentiment} onChange={(event) => { setMarketSentiment(event.target.value); setMarketOffset(0); setMarketSelectedKey(null); }}><option value="all">全部态度</option>{SENTIMENT_OPTIONS.map((value) => <option key={value} value={value}>{value}</option>)}</select>
        </div>
        {marketError && <div className="notice error xAgentInlineNotice"><CircleAlert size={17} /> {marketError}</div>}
        <div className="xAgentTableWrap" role="table">
          <div className="xAgentMarketHead" role="row"><span>标的</span><span>范围</span><span>态度</span><span>汇总说明</span><span>最近提及</span></div>
          {!marketLoading && marketItems.length === 0 && <div className="emptyState xAgentResultEmpty"><span>当前窗口暂无运行时观察。</span><button className="secondaryButton compact" type="button" onClick={() => openSubscribedAccounts('market_sentiment')}>查看 {dashboard?.accounts.marketSentimentEnabled ?? 0} 个订阅账号</button></div>}
          {marketItems.map((item) => <div key={item.instrumentKey} className="xAgentResultGroup">
            <div className={marketSelectedKey === item.instrumentKey ? 'xAgentMarketRow active' : 'xAgentMarketRow'} role="row">
              <button className="xAgentDetailTrigger" type="button" aria-expanded={marketSelectedKey === item.instrumentKey} onClick={() => toggleMarketDetail(item.instrumentKey)}><ChevronDown size={16} className={marketSelectedKey === item.instrumentKey ? 'rotated' : ''} /><span><strong>{item.instrumentName}</strong>{item.ticker && <small>{item.ticker}</small>}</span></button>
              <span>{item.scope || '-'}</span><span className="xAgentSentimentPill">{item.sentiment || '-'}</span><span className="xAgentReason">{item.reason || '-'}</span><span>{formatTime(item.latestAt)}</span>
            </div>
            {marketSelectedKey === item.instrumentKey && <MarketDetail detail={marketDetail} loading={marketDetailLoading} error={marketDetailError} offset={marketDetailOffset} onOffsetChange={setMarketDetailOffset} />}
          </div>)}
        </div>
        <PageControls offset={marketOffset} total={marketTotal} disabled={marketLoading} onChange={(offset) => { setMarketOffset(offset); setMarketSelectedKey(null); }} />
      </section>}

      {tab === 'project_promotion' && <section className="xAgentTableSection">
        <div className="sectionHeader"><div><h2>项目推介</h2><span>项目与账号表达的逻辑</span></div><span>{projectLoading ? '加载中' : `${projectTotal.toLocaleString()} 个项目`}</span></div>
        <div className="xAgentFilters xAgentResultFilters">
          <label><Search size={16} /><input value={projectQuery} onChange={(event) => { setProjectQuery(event.target.value); setProjectOffset(0); setProjectSelectedKey(null); }} placeholder="搜索项目或代币" /></label>
          <select value={projectWindow} onChange={(event) => { setProjectWindow(event.target.value); setProjectOffset(0); setProjectSelectedKey(null); }}>{PROJECT_WINDOW_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select>
        </div>
        {projectError && <div className="notice error xAgentInlineNotice"><CircleAlert size={17} /> {projectError}</div>}
        <div className="xAgentTableWrap" role="table">
          <div className="xAgentProjectHead" role="row"><span>项目</span><span>链 / 合约</span><span>逻辑</span><span>最近提及</span></div>
          {!projectLoading && projectItems.length === 0 && <div className="emptyState xAgentResultEmpty"><span>当前窗口暂无运行时观察。</span><button className="secondaryButton compact" type="button" onClick={() => openSubscribedAccounts('project_promotion')}>查看 {dashboard?.accounts.projectPromotionEnabled ?? 0} 个订阅账号</button></div>}
          {projectItems.map((item) => <div key={item.identityKey} className="xAgentResultGroup">
            <div className={projectSelectedKey === item.identityKey ? 'xAgentProjectRow active' : 'xAgentProjectRow'} role="row">
              <button className="xAgentDetailTrigger" type="button" aria-expanded={projectSelectedKey === item.identityKey} onClick={() => toggleProjectDetail(item.identityKey)}><ChevronDown size={16} className={projectSelectedKey === item.identityKey ? 'rotated' : ''} /><span><strong>{item.projectName}</strong>{item.ticker && <small>{item.ticker}</small>}</span></button>
              <span className="xAgentContract" title={item.contractAddress || ''}>{[item.chainName, item.contractAddress].filter(Boolean).join(' · ') || '-'}</span><span className="xAgentReason">{item.logic || '-'}</span><span>{formatTime(item.lastMentionedAt)}</span>
            </div>
            {projectSelectedKey === item.identityKey && <ProjectDetail detail={projectDetail} loading={projectDetailLoading} error={projectDetailError} offset={projectDetailOffset} onOffsetChange={setProjectDetailOffset} />}
          </div>)}
        </div>
        <PageControls offset={projectOffset} total={projectTotal} disabled={projectLoading} onChange={(offset) => { setProjectOffset(offset); setProjectSelectedKey(null); }} />
      </section>}
    </section>
  );
}

function Metric({ label, value, warning = false, onClick }: { label: string; value: number; warning?: boolean; onClick?: () => void }) {
  const className = warning ? 'xAgentMetric warning' : 'xAgentMetric';
  const content = <><span>{label}</span><strong>{value.toLocaleString()}</strong></>;
  if (onClick) return <button className={`${className} interactive`} type="button" onClick={onClick} title={`查看${label}`}>{content}</button>;
  return <div className={className}>{content}</div>;
}
