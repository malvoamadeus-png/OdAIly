# Codex Linux Access

This file is for future Codex sessions. It explains how to connect from this
Windows development machine to the production Linux server.

## Connection Info

- Codex MCP server name: `ssh-prod`
- Host: `47.76.243.147`
- User: `root`
- Local private key path: `C:\Users\Windows\.ssh\id_ed25519`
- Remote project directory: `/opt/Jibai`
- Worker service: `jibai-public-worker`
- Worker environment file: `/etc/jibai/public-worker.env`

Do not paste private key contents, Supabase connection strings, OpenAI keys, or
other environment variable values into chat or committed docs. Refer only to the
local key path.

## Preferred MCP Access

New Codex sessions usually read the global local MCP config from:

```powershell
C:\Users\Windows\.codex\config.toml
```

If `ssh-prod` is exposed as an MCP tool in the current session, ask Codex:

```text
Use ssh-prod to connect to the server, cd to /opt/Jibai, and check worker status.
```

The global MCP config should be created with:

```powershell
codex mcp add ssh-prod -- npx -y ssh-mcp -- --host=47.76.243.147 --user=root --key=C:\Users\Windows\.ssh\id_ed25519
```

Important: `ssh-mcp` uses `--key`, not `--privateKeyPath`. Existing Codex
sessions may not hot-load new MCP servers; restart Codex or open a new session
if `ssh-prod` is not available.

## Direct SSH Fallback

If the session does not expose `ssh-prod`, use plain SSH from PowerShell:

```powershell
ssh -i C:\Users\Windows\.ssh\id_ed25519 -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new root@47.76.243.147 "hostname; date; cd /opt/Jibai && git status --short"
```

Run the public worker doctor with the production env loaded:

```powershell
ssh -i C:\Users\Windows\.ssh\id_ed25519 -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new root@47.76.243.147 "cd /opt/Jibai && source .venv/bin/activate && set -a && source /etc/jibai/public-worker.env && set +a && python backend/src/main.py public-worker-doctor"
```

## Common Remote Commands

Check worker status:

```bash
systemctl status jibai-public-worker --no-pager -l
```

Read recent worker logs:

```bash
journalctl -u jibai-public-worker -n 120 --no-pager
```

Restart worker:

```bash
systemctl restart jibai-public-worker
```

Read only non-secret worker settings:

```bash
grep -E '^(PUBLIC_WORKER_CRAWL_TIMES|PUBLIC_WORKER_ACCOUNT_DELAY_SECONDS|PUBLIC_WORKER_POLL_SECONDS|PUBLIC_WORKER_PAGE_WAIT_SECONDS|PUBLIC_WORKER_NITTER_INSTANCES|PUBLIC_WORKER_MARKET_DATA_DAYS)=' /etc/jibai/public-worker.env
```

Enqueue one global public X crawl:

```bash
cd /opt/Jibai
source .venv/bin/activate
set -a
source /etc/jibai/public-worker.env
set +a
python backend/src/main.py public-enqueue-scheduled
```

Run worker diagnostics:

```bash
cd /opt/Jibai
source .venv/bin/activate
set -a
source /etc/jibai/public-worker.env
set +a
python backend/src/main.py public-worker-doctor
```

## Reliable PowerShell Multiline SSH

For complex remote scripts, encode the script as base64 before sending it to
remote bash. This avoids PowerShell expanding `$host`, pipes, quotes, or line
continuations incorrectly.

```powershell
$script = @'
set -e
cd /opt/Jibai
source .venv/bin/activate
python backend/src/main.py public-worker-doctor
'@
$script = ($script -replace "`r`n", "`n") + "`n"
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($script))
ssh -i C:\Users\Windows\.ssh\id_ed25519 -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new root@47.76.243.147 "bash -lc 'echo $b64 | base64 -d | bash'"
```

## New Machine Or New Windows User

Codex does not automatically know this server on another machine, another
Windows user, or another Codex config directory. Set it up again:

1. Ensure the local private key exists, for example `C:\Users\Windows\.ssh\id_ed25519`.
2. Confirm plain SSH can connect to the server.
3. Run `codex mcp add ssh-prod ...` again.
4. Restart Codex or open a new session so MCP config reloads.

Consider adding an SSH alias in `C:\Users\Windows\.ssh\config`:

```sshconfig
Host jibai-prod
  HostName 47.76.243.147
  User root
  IdentityFile C:\Users\Windows\.ssh\id_ed25519
```

Then MCP can use the alias:

```powershell
codex mcp add ssh-prod -- npx -y ssh-mcp -- --host=jibai-prod --user=root --key=C:\Users\Windows\.ssh\id_ed25519
```

If the host IP or key changes later, update SSH config first.
