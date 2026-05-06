import { FormEvent, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  Database,
  Pause,
  Plus,
  RefreshCcw,
  Save,
  Trash2,
  Zap,
} from 'lucide-react';
import {
  createAccount,
  deleteAccount as deleteAccountFromSupabase,
  loadDashboard,
  updateAccount,
  updateSettings,
  type Account,
  type AccountPatch,
  type Attempt,
  type Settings,
  type TaskItem,
} from './xCaptureStore';

const emptySettings: Settings = {
  global_interval_seconds: 30,
  max_concurrency: 2,
  jitter_seconds: 5,
  updated_at: null,
};

function fmtTime(value: string | null | undefined): string {
  if (!value) return '-';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(new Date(value));
}

export function App() {
  const [settings, setSettings] = useState<Settings>(emptySettings);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [attempts, setAttempts] = useState<Attempt[]>([]);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [savingSettings, setSavingSettings] = useState(false);
  const [newAccount, setNewAccount] = useState({
    username_or_url: '',
    display_name: '',
    interval_seconds: '',
  });

  const enabledCount = useMemo(() => accounts.filter((account) => account.enabled).length, [accounts]);
  const latestAttempt = attempts[0];

  function updateSetting<K extends keyof Pick<Settings, 'global_interval_seconds' | 'max_concurrency' | 'jitter_seconds'>>(
    key: K,
    value: string,
  ) {
    setSettings((current) => ({
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

  useEffect(() => {
    loadAll().catch((err: Error) => {
      setError(err.message);
      setLoading(false);
    });
    const timer = window.setInterval(() => {
      loadAll().catch((err: Error) => setError(err.message));
    }, 10000);
    return () => window.clearInterval(timer);
  }, []);

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

  async function deleteAccount(account: Account) {
    setError('');
    try {
      await deleteAccountFromSupabase(account.id);
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brandMark">O</div>
          <div>
            <strong>OdAIly</strong>
            <span>X Capture</span>
          </div>
        </div>
        <nav>
          <a className="active" href="#accounts">
            <Activity size={18} /> 账号
          </a>
          <a href="#tasks">
            <Database size={18} /> 入库
          </a>
        </nav>
      </aside>

      <main className="content">
        <header className="topbar">
          <div>
            <h1>X 抓取控制台</h1>
            <p>{enabledCount} 个启用账号 · 全局 {settings.global_interval_seconds}s</p>
          </div>
          <button className="iconButton" type="button" onClick={() => loadAll()} title="刷新">
            <RefreshCcw size={18} />
          </button>
        </header>

        {(message || error) && (
          <div className={error ? 'notice error' : 'notice'} onAnimationEnd={() => !error && setMessage('')}>
            {error || message}
          </div>
        )}

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
      </main>

      <aside className="rightRail">
        <div className="metric">
          <span>最近抓取</span>
          <strong>{latestAttempt ? latestAttempt.status : '-'}</strong>
          <small>{latestAttempt ? fmtTime(latestAttempt.finished_at) : '-'}</small>
        </div>
        <div className="metric">
          <span>新增</span>
          <strong>{latestAttempt?.new_count ?? 0}</strong>
          <small>保存 {latestAttempt?.saved_count ?? 0}</small>
        </div>
        <div className="attempts">
          <h2>抓取记录</h2>
          {attempts.length === 0 && <div className="emptyState compact">暂无抓取记录。</div>}
          {attempts.slice(0, 12).map((attempt) => (
            <div className="attemptItem" key={attempt.id}>
              <div>
                <strong>@{attempt.username_lower}</strong>
                <span className={attempt.status === 'success' ? 'ok' : 'bad'}>{attempt.status}</span>
              </div>
              <p>
                候选 {attempt.candidate_count} · seed {attempt.seeded_count} · 新 {attempt.new_count}
              </p>
              {attempt.error && <small>{attempt.error}</small>}
            </div>
          ))}
        </div>
      </aside>
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
