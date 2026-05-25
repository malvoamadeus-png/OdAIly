# OdAIly

OdAIly is a Python worker that generates market briefs and sends them to the
Push Data API with `isPublish=false` and `isPush=false`.

## Structure

```text
backend/   Python worker and business packages
frontend/  Reserved for future UI
data/      Runtime data, configs, raw quotes, processed briefs, exports
docs/      Project docs, module specs, and prompt templates
tests/     Unit and integration tests
```

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp data/config/market_brief.example.json data/config/market_brief.json
cp data/config/gate_tradfi.example.json data/config/gate_tradfi.json
python backend/src/main.py doctor
python backend/src/main.py run-once --task us-market --kind close --dry-run --force
python backend/src/main.py run-once --task gate-tradfi --kind morning --dry-run --force
```

On Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
Copy-Item data\config\market_brief.example.json data\config\market_brief.json
Copy-Item data\config\gate_tradfi.example.json data\config\gate_tradfi.json
python backend\src\main.py doctor
python backend\src\main.py run-once --task us-market --kind close --dry-run --force
python backend\src\main.py run-once --task gate-tradfi --kind morning --dry-run --force
```

## Tasks And Schedules

- `us-market close`: 09:00 Asia/Shanghai.
- `us-market premarket`: manual `run-once` only; it is not scheduled.
- `us-market open`: 09:31 America/New_York.
- `gate-tradfi morning`: 09:00 Asia/Shanghai.
- `gate-tradfi open`: 09:31 America/New_York.

Calendar rules:

- `gate-tradfi` runs on Beijing business weekdays only. `morning` and `open`
  both skip automatically on Saturday and Sunday in `Asia/Shanghai`.
- `us-market` follows the U.S. market calendar. Automatic `open` and
  next-morning `close` both skip on New York weekends and NYSE market
  holidays. This means the automatic MSX cycle starts on Monday evening
  Beijing time and ends with the Saturday-morning Beijing close brief.
- `us-market premarket` logic is still available for manual `run-once`, but it
  is intentionally not part of the scheduled worker.
- `--force` bypasses these calendar skips for one manual run.

If Yahoo returns no valid market data, or Gate returns no valid title candidate
data, the worker records a skipped result and does not call the Push Data API.

## Send Behavior

The default config uses `dry_run=true`. To send to the API:

```bash
python backend/src/main.py run-once --task us-market --kind open --send
python backend/src/main.py run-once --task gate-tradfi --kind open --send
```

Every real request sends:

```json
{
  "title": "...",
  "content": "...",
  "isPublish": false,
  "isPush": false
}
```

X processing requests also include `sourceUrl` from the original X post. MSX
and Gate generated briefs do not send `sourceUrl`, and no flow sends
`imageUrl` unless it is explicitly added later.

## X Capture Console

The first X capture module stores raw public X/Twitter posts into Postgres
`tasks` with `status='pending'`. It uses `SUPABASE_DB_URL` or `DATABASE_URL`
for direct Postgres access in the backend worker. The browser console is a
Vite static app that connects to Supabase with `VITE_SUPABASE_URL` and
`VITE_SUPABASE_ANON_KEY`, then authenticates operators with Supabase Auth
email/password before any console table access is allowed.

```powershell
pip install -r backend\requirements.txt
python backend\src\main.py x-init-db
python backend\src\main.py x-capture-worker
```

If you initialize Supabase manually, run `supabase/x_capture_schema.sql` once
in the Supabase SQL Editor before deploying the frontend.

For frontend development and Vercel deployment:

```powershell
cd frontend
npm install
npm run dev
```

Bootstrap a console admin after schema initialization:

```powershell
python backend\src\main.py console-grant-admin --email your-admin@example.com
```

The deployed frontend writes `x_capture_settings` and `x_capture_accounts`
directly through Supabase, but only for authenticated emails present in
`console_admins`. The worker listens on Postgres `x_capture_config_changed`
notifications and does not expose a console port.

## Collectors And Processing Pipeline

The pipeline uses collectors plus stage workers. Collector-X stores raw X posts
in `tasks`. Collector-Competitor stores BlockBeats, PANews, and Jinse items in
`tasks`, while Odaily newsflashes are saved as reference material and do not
enter the writing queue.

Publishing freshness is controlled by `PROCESSING_FRESHNESS_WINDOW_SECONDS`
(default `600`). Collector-X and Collector-Competitor skip stale source items
when writing `tasks`; the processing workers also expire stale or unknown-time
tasks before AI, Push API, or Telegram calls. Competitor/Odaily event replay
data still enters `newsflash_*` so the event console can backfill downtime.

X tasks run through judge -> search -> write -> format/publish. Competitor
tasks run through search -> judge -> write -> format/publish. The search stage
uses DashScope embeddings and suppresses duplicate Odaily or in-flight events
before writing. Competitor URLs stay internal and are not sent to the backend.

The current implementation names still use `x_capture` and
`competitor_monitor`. This README uses the business names Collector-X and
Collector-Competitor, but the commands and packages are not renamed in this
change.

Initialize the processing schema and seed prompts from `docs/*.txt`:

```powershell
python backend\src\main.py x-process-init-db
python backend\src\main.py competitor-init-db
```

This command deletes existing X `pending` tasks by default so old backlog does
not flow into the new pipeline. Use `--skip-clear-pending` only when you want
to keep the backlog.

Run each stage as its own worker:

```powershell
python backend\src\main.py competitor-monitor-worker
python backend\src\main.py x-process-worker --stage judge
python backend\src\main.py x-process-worker --stage search
python backend\src\main.py x-process-worker --stage write
python backend\src\main.py x-process-worker --stage format_publish
```

## Whale Watch

The `巨鲸` console tab now contains two monitors:

- `链上`: EVM address monitoring through Blockscout-compatible explorers.
- `Hyperliquid`: open/close fill monitoring through the Hyperliquid `info` API.

The browser writes only address configuration and labels into Supabase. Backend
workers poll the upstream APIs every 60 seconds, dedupe new activity, and send
Telegram alerts. The first poll for an address seeds current state and does
not alert historical activity.

Initialize and run:

```powershell
python backend\src\main.py whale-watch-init-db
python backend\src\main.py whale-watch-worker
python backend\src\main.py whale-watch-hyperliquid-worker
```

Useful runtime env:

```text
WHALE_WATCH_INTERVAL_SECONDS=60
WHALE_WATCH_CHAIN_KEYS=ethereum,base
WHALE_WATCH_REQUEST_TIMEOUT_SECONDS=20
WHALE_WATCH_MAX_ATTEMPTS=3
WHALE_WATCH_BACKOFF_SECONDS=1
WHALE_TELEGRAM_MESSAGE_THREAD_ID=
WHALE_HYPERLIQUID_TELEGRAM_MESSAGE_THREAD_ID=
WHALE_HYPERLIQUID_INTERVAL_SECONDS=60
WHALE_HYPERLIQUID_MIN_NOTIONAL_USD=50000
WHALE_HYPERLIQUID_REQUEST_TIMEOUT_SECONDS=20
WHALE_HYPERLIQUID_MAX_ATTEMPTS=3
WHALE_HYPERLIQUID_BACKOFF_SECONDS=1
```

`WHALE_TELEGRAM_MESSAGE_THREAD_ID` falls back to
`TELEGRAM_MESSAGE_THREAD_ID` when it is not set. The first supported chains
are Ethereum and Base through public Blockscout v2 APIs.
`WHALE_HYPERLIQUID_TELEGRAM_MESSAGE_THREAD_ID` falls back to
`WHALE_TELEGRAM_MESSAGE_THREAD_ID`, then `TELEGRAM_MESSAGE_THREAD_ID`.
Hyperliquid notifications ignore fills with `notional_usd < 50000` by default.

Required runtime env:

```text
OPENAI_API_KEY=
DASHSCOPE_API_KEY=
BLOCKBEATS_API_KEY=
X_CAPTURE_ATTEMPT_RETENTION_DAYS=3
PROCESSING_FRESHNESS_WINDOW_SECONDS=600
X_PROCESS_OPENAI_BASE_URL=https://api.openai.com/v1
X_PROCESS_OPENAI_API_STYLE=responses
COMPETITOR_OPENAI_API_STYLE=
X_PROCESS_JUDGE_MODEL=gpt-5.4-mini
X_PROCESS_WRITER_MODEL=gpt-5.5
X_PROCESS_WRITER_REASONING_EFFORT=high
X_PROCESS_PUSH_ENDPOINT=http://47.113.217.70:8501/push/data
SEARCH_EMBEDDING_MODEL=text-embedding-v4
SEARCH_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
SEARCH_WINDOW_HOURS=6
SEARCH_DUPLICATE_THRESHOLD=0.88
SEARCH_AI_REVIEW_THRESHOLD=0.78
COMPETITOR_EVENT_WINDOW_HOURS=6
COMPETITOR_FETCH_INTERVAL_SECONDS=60
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_MESSAGE_THREAD_ID=
WHALE_TELEGRAM_MESSAGE_THREAD_ID=
WHALE_WATCH_INTERVAL_SECONDS=60
WHALE_WATCH_CHAIN_KEYS=ethereum,base
WHALE_WATCH_REQUEST_TIMEOUT_SECONDS=20
WHALE_WATCH_MAX_ATTEMPTS=3
WHALE_WATCH_BACKOFF_SECONDS=1
WHALE_HYPERLIQUID_TELEGRAM_MESSAGE_THREAD_ID=
WHALE_HYPERLIQUID_INTERVAL_SECONDS=60
WHALE_HYPERLIQUID_MIN_NOTIONAL_USD=50000
WHALE_HYPERLIQUID_REQUEST_TIMEOUT_SECONDS=20
WHALE_HYPERLIQUID_MAX_ATTEMPTS=3
WHALE_HYPERLIQUID_BACKOFF_SECONDS=1
WRITER3_START_AFTER=
WRITER3_HISTORY_DAYS=90
WRITER3_ANALYSIS_MODEL=gpt-5.4-mini
WRITER3_WRITER_MODEL=gpt-5.5
WRITER3_WRITER_REASONING_EFFORT=medium
WRITER3_CANDIDATE_LIMIT=20
WRITER3_CONTEXT_CANDIDATES=5
WRITER3_CURRENT_FRESHNESS_WINDOW_SECONDS=600
WRITER3_TELEGRAM_MESSAGE_THREAD_ID=
```

`X_PROCESS_OPENAI_BASE_URL` may point to an OpenAI-compatible relay, usually
ending in `/v1`. Keep `X_PROCESS_OPENAI_API_STYLE=responses` when the relay
supports `/v1/responses`; use `chat_completions` when it only supports
`/v1/chat/completions`. Set `COMPETITOR_OPENAI_API_STYLE` only when the
competitor event-review endpoint needs a different style from the rest of the
X-processing workers. `X_PROCESS_WRITER_REASONING_EFFORT` controls the
reasoning effort used by 编写者1 and now defaults to `high`.

The Vite console also includes Prompt editing and publishing. Publishing a
prompt version updates `prompt_templates.active_version_id`; workers listen for
`prompt_config_changed` and refresh prompt cache.

After deploying the competitor event console, verify the database and workers:

```powershell
python backend\src\main.py competitor-init-db
python backend\src\main.py competitor-monitor-worker --once
python backend\src\main.py x-process-worker --stage search --once
python backend\src\main.py writer3-init-db
python backend\src\main.py writer3-backfill-odaily --days 90
python backend\src\main.py writer3-sync-index --days 90
python backend\src\main.py writer3-worker --once
python backend\src\main.py writer3-reset-task --task-id 123
```

## Linux Service

Use `/opt/OdAIly` for deployment. Repo-tracked service files live under
`deploy/`, including:

- `deploy/odaily-worker.service`
- `deploy/odaily-competitor-monitor.service`
- `deploy/odaily-non-mainstream-media.service`
- `deploy/odaily-external-media-alert@.service`
- `deploy/odaily-whale-watch.service`
- `deploy/odaily-whale-watch-hyperliquid.service`
- `deploy/odaily-pipeline-supervisor.service`

Production sync rules:

1. Develop and test in the local checkout first.
2. Commit and push repo-tracked changes to GitHub.
3. Update the server from `/opt/OdAIly` with:

   ```bash
   git fetch origin
   git pull --ff-only origin main
   ```

4. Verify the worktree is clean:

   ```bash
   git status --short
   ```

5. Verify running services and recent logs:

   ```bash
   systemctl is-active odaily-worker.service odaily-x-process@judge.service odaily-x-process@search.service odaily-x-process@write.service odaily-x-process@format_publish.service odaily-competitor-monitor.service odaily-x-capture.service odaily-non-mainstream-media.service odaily-external-media-alert@domain_judge.service odaily-external-media-alert@search.service odaily-external-media-alert@notify.service odaily-whale-watch.service odaily-whale-watch-hyperliquid.service odaily-pipeline-supervisor.service
   journalctl -u odaily-worker.service -u odaily-non-mainstream-media.service -u odaily-external-media-alert@domain_judge.service -u odaily-external-media-alert@search.service -u odaily-external-media-alert@notify.service -u odaily-whale-watch.service -u odaily-whale-watch-hyperliquid.service -u odaily-pipeline-supervisor.service -n 50 --no-pager
   ```

Production servers are not a long-term editing environment for repo-tracked
files. If a temporary hotfix is applied on the server during an incident, copy
it back into the local checkout and commit it to GitHub immediately; otherwise
the next clean pull will overwrite it.

Keep these local-only runtime assets on the server and out of Git:

- `.env`
- `.venv/`
- `data/raw/`
- `data/processed/`
- `data/exports/`
- `data/config/market_brief.json`

Clean these worktree pollutants instead of keeping them beside the repo:

- `.codex-backups/`
- `.env.codex-*`
- ad hoc debug scripts and one-off restore files
- temporary copied docs or experiment directories that are not tracked by Git

## Documentation

- `docs/完整程序架构.md`: system contract and documentation index.
- `docs/收集者-X.md`: X/Twitter collection and task ingestion.
- `docs/收集者-竞品.md`: competitor and Odaily reference collection.
- `docs/控制台.md`: console module index.
- `docs/判断者.md`: route and discard rules.
- `docs/搜索者.md`: duplicate detection and embedding strategy.
- `docs/编写者1.md`: AI draft generation and prompt version tracing.
- `docs/编写者2.md`: deterministic formatting and push behavior.
- `docs/监督者.md`: pipeline health checks and alerts.
