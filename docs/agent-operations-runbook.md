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

部署后按改动范围重启对应服务。AI信源 / 外媒 / X-processing 常用组合：

```bash
/mnt/c/Windows/System32/OpenSSH/ssh.exe jibai-prod '
set -e
cd /opt/OdAIly
. .venv/bin/activate
python backend/src/main.py non-mainstream-media-init-db
python backend/src/main.py x-process-init-db
python backend/src/main.py external-media-alert-init-db
systemctl restart odaily-non-mainstream-media.service
systemctl restart odaily-x-process@judge.service odaily-x-process@search.service odaily-x-process@write.service odaily-x-process@format_publish.service odaily-x-process@publish.service
systemctl restart odaily-external-media-alert@domain_judge.service odaily-external-media-alert@search.service odaily-external-media-alert@notify.service
systemctl restart odaily-pipeline-supervisor.service
systemctl is-active odaily-non-mainstream-media.service odaily-x-process@judge.service odaily-x-process@search.service odaily-x-process@write.service odaily-x-process@format_publish.service odaily-x-process@publish.service odaily-external-media-alert@domain_judge.service odaily-external-media-alert@search.service odaily-external-media-alert@notify.service odaily-pipeline-supervisor.service
'
```

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
