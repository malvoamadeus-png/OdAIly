# Agent Operations Runbook

本文记录 Codex / WSL 环境执行 GitHub 推送、Linux 生产部署和前端上线核验时优先使用的稳定路径。

## 稳定路径优先级

- GitHub 操作优先走 `ssh.github.com:443`，不要反复尝试普通 `github.com:22`。
- Linux 生产机操作优先从 WSL 调用 Windows OpenSSH：`/mnt/c/Windows/System32/OpenSSH/ssh.exe`。
- 生产代码仍按仓库同步规则部署：本地提交并推送 GitHub 后，服务器 `/opt/OdAIly` 执行 `git fetch` 与 `git pull --ff-only`。
- 不在服务器上手工覆盖或长期保留 repo-tracked 文件。

## GitHub Push

当前 WSL 到 GitHub 22 端口可能超时。推送时使用 GitHub 官方 SSH over HTTPS 端口：

```bash
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_rsa_A_github -o IdentitiesOnly=yes -o HostName=ssh.github.com -o Port=443 -o BatchMode=yes -o ConnectTimeout=15' \
  git push git@github.com:malvoamadeus-png/OdAIly.git main
```

验证远端：

```bash
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_rsa_A_github -o IdentitiesOnly=yes -o HostName=ssh.github.com -o Port=443 -o BatchMode=yes -o ConnectTimeout=15' \
  git ls-remote git@github.com:malvoamadeus-png/OdAIly.git main
```

## Linux Deploy

WSL 原生 `ssh` 到 `47.76.243.147:22` 偶尔超时。优先调用 Windows OpenSSH：

```bash
/mnt/c/Windows/System32/OpenSSH/ssh.exe -o BatchMode=yes -o ConnectTimeout=15 root@47.76.243.147 \
  "cd /opt/OdAIly && git fetch origin && git pull --ff-only origin main && git status --short"
```

Windows SSH 配置位于 `C:\Users\A\.ssh\config`，其中 `jibai-prod` 指向生产机。也可以使用：

```bash
/mnt/c/Windows/System32/OpenSSH/ssh.exe jibai-prod "cd /opt/OdAIly && git status --short"
```

## Backend Restart

部署后按改动范围重启对应服务。自动快讯主链路和标题提醒链路现在由 `odaily-local-pipeline.service` 承载，不再启动旧的分阶段 `x-process` / `external-media-alert` worker。

本地流水线一次切换：

```bash
/mnt/c/Windows/System32/OpenSSH/ssh.exe jibai-prod '
set -e
cd /opt/OdAIly
. .venv/bin/activate
python backend/src/main.py non-mainstream-media-init-db
python backend/src/main.py x-process-init-db --skip-clear-pending
python backend/src/main.py external-media-alert-init-db
python backend/src/main.py local-pipeline-skip-legacy --execute
install -m 0644 deploy/odaily-local-pipeline.service /etc/systemd/system/odaily-local-pipeline.service
systemctl daemon-reload
systemctl disable --now odaily-x-process@judge.service odaily-x-process@judge_crypto.service odaily-x-process@judge_ai.service odaily-x-process@search.service odaily-x-process@write.service odaily-x-process@format_publish.service odaily-x-process@publish.service || true
systemctl disable --now odaily-external-media-alert@domain_judge.service odaily-external-media-alert@search.service odaily-external-media-alert@notify.service || true
systemctl enable --now odaily-local-pipeline.service
systemctl restart odaily-non-mainstream-media.service
systemctl restart odaily-x-capture.service || true
systemctl restart odaily-competitor-monitor.service || true
systemctl restart odaily-pipeline-supervisor.service
systemctl is-active odaily-local-pipeline.service odaily-non-mainstream-media.service odaily-pipeline-supervisor.service
curl -fsS http://127.0.0.1:8776/health
'
```

说明：

- `local-pipeline-skip-legacy --execute` 只标记切换前未完成旧任务为 `legacy_skipped`，不导入本地队列。
- `x-process-init-db --skip-clear-pending` 会初始化/迁移 schema 并删除旧 notify trigger，但不会清掉待处理任务。
- 旧分阶段服务保留代码和 unit 名用于紧急回滚，但生产默认停用。

市场快讯相关改动额外重启：

```bash
/mnt/c/Windows/System32/OpenSSH/ssh.exe jibai-prod '
set -e
cd /opt/OdAIly
systemctl restart odaily-worker.service
systemctl is-active odaily-worker.service
'
```

原因：`odaily-worker.service` 是长驻调度进程，`git pull` 后不会自动加载新的 Python 代码或新的任务常量；涉及 `us-market`、`gate-tradfi`、`market_brief.json` 解释逻辑时，部署后必须重启该服务。

## Frontend Verification

控制台前端由 Vercel 托管，GitHub `main` 更新后通常自动部署。上线核验：

```bash
curl -L --max-time 20 -s https://od-a-ily.vercel.app | sed -n '1,20p'
```

取首页返回的 `/assets/index-*.js` 后检查新文案：

```bash
curl -L --max-time 30 -s https://od-a-ily.vercel.app/assets/index-XXXX.js | rg 'AI信源|已接入AI信源'
```

如果生产页面仍显示旧内容，先等待 Vercel 自动部署完成，再强刷浏览器缓存；不要在服务器上手工复制前端构建产物。
