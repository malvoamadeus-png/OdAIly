# AGENTS.md

## 文档维护规则

修改代码时必须同步检查文档。

- 如果修改整体流程、任务状态、worker 边界、跨模块契约或来源链路，更新 `docs/OdAIly代码与整体设计/完整程序架构.md`。
- 如果修改单个模块的输入、输出、Prompt、状态、环境变量、命令或失败处理，更新对应细节文档。
- 如果问题、改动或排障涉及 Supabase 连接池、session、连接槽、`EMAXCONNSESSION`、`ECHECKOUTTIMEOUT`、`max clients reached`、`pool_size`、`idle in transaction` 或数据库长连接，必须先阅读并遵守本机私有文档 `docs/工具与运维私有/Supabase连接槽与Session排障.md`。
- `docs/OdAIly代码与整体设计/完整程序架构.md` 只写系统级契约和文档索引，不重复模块细节。
- 模块细节以 `docs/OdAIly代码与整体设计/` 中的对应文档和实际代码为准。
- 不要在文档中保留已经废弃的 JSON 示例、状态名或分类名。

## 当前文档边界

- 收集者文档：`docs/OdAIly代码与整体设计/收集者-X.md`、`docs/OdAIly代码与整体设计/收集者-竞品.md`。
- 控制台文档：`docs/OdAIly代码与整体设计/控制台.md` 及 `docs/OdAIly代码与整体设计/控制台-*.md`。
- 处理阶段文档：`docs/OdAIly代码与整体设计/判断者.md`、`docs/OdAIly代码与整体设计/搜索者.md`、`docs/OdAIly代码与整体设计/编写者1.md`、`docs/OdAIly代码与整体设计/编写者2.md`、`docs/OdAIly代码与整体设计/编写者3.md`。
- 发布后异步文档：`docs/OdAIly代码与整体设计/审核者.md`。
- 监控文档：`docs/OdAIly代码与整体设计/监督者.md`。
- Supabase session / 连接槽排障文档：`docs/工具与运维私有/Supabase连接槽与Session排障.md`（本机私有，不提交）。

## 命名说明

文档统一使用“收集者-X”“收集者-竞品”的业务命名。当前代码包、CLI 命令和 systemd service 可能仍保留 `x_capture`、`competitor_monitor` 等实现名；不要只为命名一致而重命名运行代码。

## 代码同步与部署规则

- repo-tracked 代码只允许在本地工作区修改；生产服务器 `/opt/OdAIly` 不作为长期开发环境。
- 代码改动必须先在本地提交并推送到 GitHub，再由服务器执行 `git fetch` 和 `git pull --ff-only` 同步。
- 未经明确授权，不要在服务器上手工覆盖、创建或长期保留 repo-tracked 文件；紧急热修如果发生，必须立即回补到本地仓库并提交到 GitHub。
- 服务器允许长期只保留本地运行资产，例如 `.env`、`.venv/`、`data/raw/`、`data/processed/`、`data/exports/`、`data/config/market_brief.json`；这些文件不纳入 Git。
- 服务器上的 `.codex-backups/`、`.env.codex-*`、临时脚本、调试输出和历史手工副本不应长期留在工作区；完成排障或清理后应移出 repo 工作树或删除。

## 生产服务器 SSH 操作

- 生产服务器 SSH 主机别名为 `jibai-prod`，目录为 `/opt/OdAIly`；Windows SSH 配置位于 `C:\Users\A\.ssh\config`。
- 当前 Codex 通常运行在 WSL 中。生产连接必须使用 Windows OpenSSH 和 Windows SSH 配置：

  ```bash
  /mnt/c/WINDOWS/System32/OpenSSH/ssh.exe -F 'C:/Users/A/.ssh/config' jibai-prod
  ```

- WSL 的 `/usr/bin/ssh` 不要直接读取 `/mnt/c/Users/A/.ssh` 下的私钥。NTFS 挂载会显示过宽权限，OpenSSH 会忽略私钥；Windows SSH 是生产连接的标准入口。
- 本地完成构建、提交并推送后，服务器只执行：

  ```bash
  cd /opt/OdAIly
  git fetch origin
  git pull --ff-only origin main
  ```

- 同步后检查 `git rev-parse --short HEAD`、`git status --short` 和相关 systemd 服务状态。后端代码或服务配置变化时，按对应模块要求重启服务；纯前端变化由 GitHub 连接的前端部署流程负责，不要把服务器目录当作前端发布入口。
- GitHub 的 `core.sshCommand` 只用于 GitHub，不代表生产服务器 SSH 配置；生产连接始终使用 `jibai-prod`。如果 WSL 的 GitHub SSH 连接超时，使用 Windows SSH fallback 推送：

  ```bash
  git -c core.sshCommand="/mnt/c/WINDOWS/System32/OpenSSH/ssh.exe -F C:/Users/A/.ssh/config" push origin main
  ```

## AI 可操作范围

- 按 `docs/OdAIly代码与整体设计/控制台.md` 等控制台文档说明，AI 可以操作前端。
- 按 `docs/OdAIly代码与整体设计/完整程序架构.md` 及各模块文档说明，AI 可以操作后端。
- 当前生产版本唯一活动存储后端是 Linux 本地 SQLite；AI 默认不得读取 `SUPABASE_DB_URL`、`DATABASE_URL`，不得把 Supabase 当作生产状态、心跳、任务或配置的查询入口。
- Supabase 相关文档、旧诊断脚本和 `storage-import-legacy` 只保留给冻结旧库的一次性迁移、灾难回切审计或用户明确要求的历史核查；执行这些操作必须显式说明 legacy 范围，不得改变当前 SQLite 运行路径。
