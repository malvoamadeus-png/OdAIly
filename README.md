# OdAIly

OdAIly is a Python worker that generates US market crypto-stock briefs and sends
them to the Push Data API with `isPublish=false`.

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
python backend/src/main.py doctor
python backend/src/main.py run-once --kind close --dry-run --force
```

On Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
Copy-Item data\config\market_brief.example.json data\config\market_brief.json
python backend\src\main.py doctor
python backend\src\main.py run-once --kind close --dry-run --force
```

## Schedules

- `close`: 08:00 Asia/Shanghai, using the latest regular market change.
- `premarket`: 04:05 America/New_York, using Yahoo pre-market change.
- `open`: 09:31 America/New_York, using Yahoo regular market change.

Weekend runs are skipped unless `--force` is passed. If Yahoo returns no valid
market data, the worker records a skipped result and does not call the Push Data
API.

## Send Behavior

The default config uses `dry_run=true`. To send to the API:

```bash
python backend/src/main.py run-once --kind open --send
```

Every real request sends:

```json
{
  "title": "...",
  "content": "...",
  "isPublish": false
}
```

## Linux Service

Use `/opt/OdAIly` for deployment and `deploy/odaily-worker.service` as the
systemd template. Keep `data/config/market_brief.json` and `.env` out of Git.
