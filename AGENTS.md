# AGENTS.md

## 文档维护规则

修改代码时必须同步检查文档。

- 如果修改整体流程、任务状态、worker 边界、跨模块契约或来源链路，更新 `docs/完整程序架构.md`。
- 如果修改单个模块的输入、输出、Prompt、状态、环境变量、命令或失败处理，更新对应细节文档。
- `docs/完整程序架构.md` 只写系统级契约和文档索引，不重复模块细节。
- 模块细节以对应 `docs/*.md` 和实际代码为准。
- 不要在文档中保留已经废弃的 JSON 示例、状态名或分类名。

## 当前文档边界

- 收集者文档：`docs/收集者-X.md`、`docs/收集者-竞品.md`。
- 控制台文档：`docs/控制台.md` 及 `docs/控制台-*.md`。
- 处理阶段文档：`docs/判断者.md`、`docs/搜索者.md`、`docs/编写者1.md`、`docs/编写者2.md`、`docs/编写者3.md`。
- 发布后异步文档：`docs/审核者.md`。
- 监控文档：`docs/监督者.md`。

## 命名说明

文档统一使用“收集者-X”“收集者-竞品”的业务命名。当前代码包、CLI 命令和 systemd service 可能仍保留 `x_capture`、`competitor_monitor` 等实现名；不要只为命名一致而重命名运行代码。

## 代码同步与部署规则

- repo-tracked 代码只允许在本地工作区修改；生产服务器 `/opt/OdAIly` 不作为长期开发环境。
- 代码改动必须先在本地提交并推送到 GitHub，再由服务器执行 `git fetch` 和 `git pull --ff-only` 同步。
- 未经明确授权，不要在服务器上手工覆盖、创建或长期保留 repo-tracked 文件；紧急热修如果发生，必须立即回补到本地仓库并提交到 GitHub。
- 服务器允许长期只保留本地运行资产，例如 `.env`、`.venv/`、`data/raw/`、`data/processed/`、`data/exports/`、`data/config/market_brief.json`；这些文件不纳入 Git。
- 服务器上的 `.codex-backups/`、`.env.codex-*`、临时脚本、调试输出和历史手工副本不应长期留在工作区；完成排障或清理后应移出 repo 工作树或删除。

## AI 可操作范围

- 按 `docs/控制台.md` 等控制台文档说明，AI 可以操作前端。
- 按 `docs/完整程序架构.md` 及各模块文档说明，AI 可以操作后端。
- 按 `docs/控制台.md` 的 Supabase 控制台约定与本机 `.codex-local/README.md` 的环境访问说明，AI 可以操作 Supabase。
