# Codex Linux Access

本文记录在 Codex Desktop / WSL 中访问生产 Linux 服务器的稳定方式。

## 推荐入口

优先使用 Windows OpenSSH，而不是 WSL 原生 `ssh`：

```bash
/mnt/c/Windows/System32/OpenSSH/ssh.exe jibai-prod
```

或直接指定目标：

```bash
/mnt/c/Windows/System32/OpenSSH/ssh.exe -o BatchMode=yes -o ConnectTimeout=15 root@47.76.243.147
```

原因：当前环境中 WSL 原生 SSH 到 `47.76.243.147:22` 偶尔超时，但 Windows OpenSSH 路径稳定。

## Windows SSH Config

Windows 侧配置文件：

```text
C:\Users\A\.ssh\config
```

当前关键 Host：

```text
Host jibai-prod
  HostName 47.76.243.147
  User root
  IdentityFile C:\Users\A\.ssh\id_ed25519
  IdentitiesOnly yes
```

从 WSL 调 Windows OpenSSH 时读取的是 Windows 侧 key 和 config，不读取 WSL 的 `~/.ssh/config`。

## 常用命令

查看服务器仓库状态：

```bash
/mnt/c/Windows/System32/OpenSSH/ssh.exe jibai-prod "cd /opt/OdAIly && git rev-parse HEAD && git status --short"
```

同步生产代码：

```bash
/mnt/c/Windows/System32/OpenSSH/ssh.exe jibai-prod "cd /opt/OdAIly && git fetch origin && git pull --ff-only origin main"
```

查看服务日志：

```bash
/mnt/c/Windows/System32/OpenSSH/ssh.exe jibai-prod "journalctl -u odaily-non-mainstream-media.service -n 120 --no-pager"
```

## PowerShell Wrapper

在 Windows PowerShell 中执行包含 SQL、JSON、Python 多行字符串或中文的远端命令时，不要把复杂脚本塞进 `ssh "..."` 一行里。PowerShell、本地 SSH、远端 bash 和目标解释器会连续处理引号，容易把 SQL 或 Python 字符串拆坏。

仓库提供两个项目专用 wrapper，默认连接 `root@47.76.243.147` 并进入 `/opt/OdAIly`，通过 base64 + stdin 把脚本发到远端执行：

```powershell
@'
git rev-parse --short HEAD
systemctl is-active odaily-editor-plugin-api.service
journalctl -u odaily-editor-plugin-api.service -n 40 --no-pager
'@ | .\scripts\prod-bash.ps1
```

```powershell
@'
import sqlite3
from pathlib import Path

p = Path("data/runtime/pipeline_timing.sqlite")
conn = sqlite3.connect(p)
print(conn.execute("""
select generated_at, length(payload_json)
from pipeline_timing_snapshots
order by generated_at desc
limit 3
""").fetchall())
conn.close()
'@ | .\scripts\prod-python.ps1
```

可选参数：

```powershell
.\scripts\prod-bash.ps1 -Target root@47.76.243.147 -Workdir /opt/OdAIly
.\scripts\prod-python.ps1 -Python .venv/bin/python
```

## 注意事项

- 生产服务器 `/opt/OdAIly` 只用于部署同步，不作为长期开发工作区。
- repo-tracked 文件必须先在本地提交并推送，再由生产机 `git pull --ff-only`。
- `.env`、`.venv/`、`data/` 等运行资产可以留在服务器，但不纳入 Git。
- 如果 Windows OpenSSH 与 WSL 原生 SSH 结果不一致，以 Windows OpenSSH 路径为准。
