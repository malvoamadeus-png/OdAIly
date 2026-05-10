# OdAIly

OdAIly is a Python worker that generates market briefs and sends them to the
Push Data API with `isPublish=false` and `isPush=false`.

## Structure

```text
backend/   Python worker and business packages
frontend/  Reserved for future UI
data/      Runtime data, configs, raw quotes, processed briefs, exports
docs/      Project docs and archived source requirements
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

Weekend runs are skipped unless `--force` is passed. If Yahoo returns no valid
market data, or Gate returns no valid title candidate data, the worker records a
skipped result and does not call the Push Data API.

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
`VITE_SUPABASE_ANON_KEY`.

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

The deployed frontend writes `x_capture_settings` and `x_capture_accounts`
directly through Supabase. The worker listens on Postgres
`x_capture_config_changed` notifications and does not expose a console port.

## X And Competitor Processing Pipeline

The processing pipeline consumes X and competitor `tasks`. X tasks run through
judge -> search -> write -> format/publish. Competitor tasks from BlockBeats,
PANews, and Jinse run through search -> judge -> write -> format/publish.
Odaily newsflashes are saved as reference material only and do not enter
`tasks`. The search stage uses DashScope embeddings and suppresses duplicate
Odaily or in-flight events before writing. Competitor URLs stay internal and
are not sent to the backend or Telegram.

The competitor monitor also writes Odaily and competitor newsflashes into the
long-lived `newsflash_*` event tables for console review. Embeddings stay in the
server-local SQLite search cache; Supabase only stores source items, event
membership, favorites, and notes.

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

Required runtime env:

```text
OPENAI_API_KEY=
DASHSCOPE_API_KEY=
BLOCKBEATS_API_KEY=
X_CAPTURE_ATTEMPT_RETENTION_DAYS=3
X_PROCESS_OPENAI_BASE_URL=https://api.openai.com/v1
X_PROCESS_OPENAI_API_STYLE=responses
X_PROCESS_JUDGE_MODEL=gpt-5.4-mini
X_PROCESS_WRITER_MODEL=gpt-5.5
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
```

`X_PROCESS_OPENAI_BASE_URL` may point to an OpenAI-compatible relay, usually
ending in `/v1`. Keep `X_PROCESS_OPENAI_API_STYLE=responses` when the relay
supports `/v1/responses`; use `chat_completions` when it only supports
`/v1/chat/completions`.

The Vite console also includes Prompt editing and publishing. Publishing a
prompt version updates `prompt_templates.active_version_id`; workers listen for
`prompt_config_changed` and refresh prompt cache.

After deploying the competitor event console, verify the database and workers:

```powershell
python backend\src\main.py competitor-init-db
python backend\src\main.py competitor-monitor-worker --once
python backend\src\main.py x-process-worker --stage search --once
```

## Linux Service

Use `/opt/OdAIly` for deployment and `deploy/odaily-worker.service` as the
systemd template. Keep `data/config/market_brief.json` and `.env` out of Git.
