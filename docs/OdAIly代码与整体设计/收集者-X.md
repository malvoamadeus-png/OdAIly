# 收集者-X

## 职责

收集者-X 根据控制台配置抓取 X/Twitter 账号公开内容，将符合时效要求的新内容写入 `tasks`，并提交本地流水线 `write_flow` job。

## 配置来源

控制台通过 Supabase 维护：

- `x_capture_settings`
- `x_capture_accounts`

worker 启动时加载配置。运行中不再保留专门的数据库监听连接，而是按固定窗口轮询重载配置，默认最多 5 分钟生效一次，不需要重启服务。

其中 `x_capture_accounts` 里的 `写作名` 用于后续 X 写作链路的发言人名称标准化：

- 收集者-X 继续抓取并保留真实 `author_display_name`、`author_username`
- `写作名` 不回写原始抓取正文，不覆盖原始作者字段
- 收集者-X 入库时会把当时可得的统一作者名缓存为 `tasks.metadata.effective_author_name`
- 收集者-X 入库时会把当时账号的 `AI信源` 标记缓存为 `tasks.metadata.x_account_is_ai_source`
- 后续 `判断者`、`编写者1` 和插件 `快讯生成` 仍会按当前账号配置重新计算 `effective_author_name`，不依赖入库时缓存值固定不变

`x_capture_accounts.is_ai_source` 是账号级 `AI信源` 标记：

- 标记为 `AI信源` 的 X 账号仍按收集者-X 原规则抓取。
- 新内容仍写入 `tasks.source = 'x'`，不进入 `tasks.source = 'ai_source'`。
- 自动化主链路会按当前账号配置把这类任务交给判断者-AI；保留后统一作为 X 常规快讯写作，但发布阶段使用 `AI信源` 发布者规则块。当前 `AI信源` 发布者关闭时，这类任务会在发布阶段直接挂后台，不调用发布者模型。
- Chrome 插件 `信息流` 会把账号对应的新快讯放入 AI区，而不是高频区。

## 入库规则

创建 `tasks` 前会调用统一源头排除匹配器：普通 X 内容检查 `x` 路径，标记为 AI信源的 X 账号同时检查 `x` 与 `ai_source`。X tweet 文本作为标题类文本参与匹配，`match_target = title` 与 `all` 对 X 当前输入等效；不检查 URL。命中后直接 mark seen，不创建任务、不提交本地流水线，也不写命中日志、标题、审计或计数。

每条抓到的 X 内容在通过基础去重与时效检查后，写入：

- `tasks.source = 'x'`
- `tasks.status = 'pending'`

与作者命名相关的入库约束：

- `tasks.metadata` 继续保留原始 `author_display_name`
- `tasks.metadata` 继续保留原始 `author_username`
- `tasks.metadata` 可缓存派生后的 `effective_author_name`
- `写作名` 作为账号配置存在，不要求在任务入库时覆盖原始作者字段
- `tasks.content` 会保留推文里的有效段落内容，但会移除空白段落；如果推文本身有多段，只保留单个 `\n` 作为段间分隔，不保留空行

判断时效使用来源原始发布时间，而不是任务入库时间。

默认时效窗：

- `PROCESSING_FRESHNESS_WINDOW_SECONDS=1200`

## 本地流水线交接

收集者-X 不再依赖 Supabase 的 `tasks.status + worker claim + LISTEN/NOTIFY` 把任务交给多个处理 worker。

当前交接规则：

- 先写 `tasks`，保留 `tasks.source = 'x'` 和 `tasks.status = 'pending'` 作为可观察档案。
- 再向 `LOCAL_PIPELINE_URL` 提交 `write_flow` job。
- 本地流水线 job 唯一键是 `source + source_item_id`，同一条 X 不会重复排队。
- 普通 X 账号执行 `judge_crypto -> search -> write -> format_publish -> publish`。
- 标记为 `AI信源` 的 X 账号执行 `judge_ai -> search -> write -> format_publish -> publish`。
- 只有本地流水线入队成功后，才把该 tweet 标记为 seen。
- 如果入队失败，本轮记录错误，不 mark seen，下一轮会继续重试。

## `x_capture_attempts` 的用途

`x_capture_attempts` 是数据库表，不是内存计数。

它保留的目的有两件事：

- 给控制台展示每个账号最近抓取是否正常
- 给排障提供最近轮询的候选数、入库数、错误和元数据

这张表现在仍保留，但改成采样写入，避免每次空轮询都打数据库。

## `x_capture_attempts` 写入规则

### 一定写入

- `status != success`
- `new_count > 0`
- `saved_count > 0`

### 采样写入

对于“success 且本轮没有新东西”的空轮询：

- 每个账号最多每 10 分钟落一条
- 如果本轮元数据指纹和上一条采样成功记录不同，也会提前写入

当前指纹会综合这些信息：

- `status`
- `candidate_count`
- `seeded_count`
- `new_count`
- `saved_count`
- `error`
- `metadata`

## 为什么仍然保留这张表

原因不是“每轮都必须留痕”，而是：

- 控制台仍需要看到最近抓取情况
- 出错、出新内容、行为变化时仍需要完整落库
- 对纯空轮询则没有必要维持原来的高写频

默认保留期为 `X_CAPTURE_ATTEMPT_RETENTION_DAYS=7` 天。针对空轮询采样检查的高频查询，生产库和 schema 中维护 `idx_x_capture_attempts_noop_recent` 局部索引，避免每次检查都扫描大量历史 attempt。

## 心跳

收集者-X 仍然写 `pipeline_worker_heartbeats`。

当前心跳策略：

- 稳定态最多 60 秒写一次
- 状态切换立即写
- 失败立即写

因此“心跳”现在表示 worker 还在健康轮询，不再等价于“每次轮询都写库”。

## 与监督者的关系

因为 `x_capture_attempts` 改成采样，监督者判定 X 收集是否健康时，不再只看 attempt 表。

当前健康条件是二选一：

- 最近窗口内存在成功 attempt
- 最近窗口内存在成功 heartbeat

这样即使 10 分钟内没有新内容、attempt 没新增，也不会误报服务挂掉。

## 运行命令

```powershell
python backend\src\main.py x-init-db
python backend\src\main.py x-capture-worker
```

`x-capture-worker` 是常驻运行命令，启动时不自动执行 schema 初始化。首次部署或 schema 变更后，必须先单独执行 `x-init-db`，再启动或重启 worker，避免常驻服务重启时在 Supabase 上反复触发 DDL 锁等待。
