# Supabase 优化调研

## 目的

这份文档用于记录 Supabase CPU、memory、disk 占用较高时的调研方向、讨论结论和待拍板事项。

当前阶段只做方向梳理和只读诊断规划；未确认前不直接修改生产数据库结构、不清理生产数据、不调整 worker 行为。

## 已拍板约束

- Chrome 插件信息流轮询频率暂时不变。
- `editor_plugin_feed` 可以在不改变插件展示范围的前提下优化内部取数方式：各来源先分别筛选最近 2 小时并限制候选数量，再合并排序。
- 运行日志类数据初步保留 7 天：`x_capture_attempts`、`editor_plugin_generation_logs`、`whale_watch_activities`、`whale_watch_hyperliquid_activities`、worker heartbeat / alert 记录。
- 插件 `接受 / 拒绝` 反馈记录初步保留 90 天。
- 插件“已看”记录不作为业务留存目标，只保留 `接受 / 拒绝` 反馈。
- `raw_payload`、冗余 `metadata`、AI 原始输出等大字段不作为业务留存目标；落地清理前必须先确认对应任务已处理完，避免删除仍在当前流程使用的上下文。

## 白话词汇表

- CPU 高：数据库正在花很多力气算查询、排序、合并结果、检查权限或处理大量写入。
- memory 高：数据库为了连接、排序、缓存热数据或执行复杂查询占用了较多内存。
- disk 高：数据库表、索引、日志、历史数据、JSON 原始载荷或系统文件占用了较多磁盘。
- 表：数据库里存一类数据的地方，例如 `tasks` 存任务，`newsflash_items` 存竞品/快讯条目。
- 索引：数据库的目录。没有合适索引时，数据库可能要把整张表翻一遍。
- 扫大表：查询为了找少量结果，却读了很多历史行。
- 最近窗口：业务只关心最近一段时间的数据，例如插件信息流只展示最近 2 小时。
- feed / 信息流：Chrome 插件侧边栏里显示的卡片列表，不是浏览器缓存，也不是 X feed。

## 当前主要方向

### 1. 插件信息流查询

涉及：

- Chrome 插件侧边栏的 `信息流` 页。
- 插件轻服务接口 `POST /plugin/feed/items`。
- 数据库函数 `editor_plugin_feed(p_limit)`。

当前业务含义：

- Chrome 插件会定时请求 `/plugin/feed/items`。
- 插件轻服务收到请求后，调用数据库函数 `editor_plugin_feed`。
- `editor_plugin_feed` 从多张业务表里取最近 2 小时内可以展示成卡片的内容。
- 返回结果再由插件前端渲染到高频区、AI区、低频区。

`editor_plugin_feed` 当前聚合的内容：

- `新快讯`：来自 `tasks` 和 `x_task_pipeline`。
- `审核者`：来自 `auditor_checks`。
- `此前消息`：来自 `writer3_contexts`。
- `巨鲸`：来自 `whale_watch_activities` 和 `whale_watch_hyperliquid_activities`。

所以这里的 feed 是“插件侧边栏卡片列表”。它不是 Chrome 插件去取网页上的内容，也不是只取 Chrome 本地某个范围；真正取数发生在 Supabase/Postgres 数据库里。

潜在问题：

- 插件每刷新一次，数据库都要把几类来源合并成统一卡片。
- 如果某些来源表已经很大，而查询条件没有很好利用索引，就可能为了找最近 2 小时的数据读很多历史数据。
- 如果有多个编辑同时开着插件，请求会叠加。

可讨论优化：

- 保持插件轮询频率不变，但优化数据库函数内部取数方式。
- 先让每个来源各自只取“最近 2 小时 + 有展示价值 + 数量上限”的少量候选，再把这些候选合并排序。
- 对应补充更贴合这些筛选条件的索引。
- 如仍有压力，再讨论插件轻服务短缓存，但这属于另一个拍板项。

当前讨论结论：

- 接受“各来源先限量取最近窗口，再合并”的优化方向。
- 不通过降低 Chrome 插件轮询频率来解决这一项。

“先各源 LIMIT 最近窗口，再 UNION”是什么意思：

当前可以理解成：

```text
把新快讯、审核者、此前消息、巨鲸这些来源倒进一个大篮子，
再统一排序、分区、挑出要显示的卡片。
```

建议方向可以理解成：

```text
每个来源先自己从最近 2 小时里挑一小篮子，
例如新快讯最多取 80 条、审核者最多取 40 条、此前消息最多取 40 条、巨鲸最多取 40 条，
然后再把这些小篮子合并排序。
```

这样做的目标是在放宽补看窗口的同时控制候选规模；插件看最近 2 小时的信息流，但每个来源仍先限量再合并。目标是让数据库少翻历史数据、少排序无关数据。

### 2. 竞品监控与事件复盘数据

涉及：

- `competitor_monitor`
- `newsflash_items`
- `newsflash_events`
- `newsflash_event_sources`

潜在问题：

- 竞品数据会持续写入和更新。
- 事件聚合需要查最近条目、活跃事件、来源关联。
- 如果历史越来越多，CPU 和磁盘都会增长。

可讨论优化：

- 明确竞品原始条目的保留周期。
- 明确事件复盘是否需要长期保留全部原文和 raw payload。
- 检查最近事件查询是否需要补索引。

### 3. 大表和历史数据保留

可能增长较快的表：

- `tasks`
- `x_task_pipeline`
- `newsflash_items`
- `newsflash_events`
- `newsflash_event_sources`
- `odaily_reference_items`
- `auditor_checks`
- `writer3_contexts`
- `editor_plugin_receipts`
- `editor_plugin_feedbacks`
- `editor_plugin_generation_logs`
- `x_seen_tweets`
- `x_capture_attempts`
- `whale_watch_activities`
- `whale_watch_hyperliquid_activities`

可讨论问题：

- 哪些表是正式业务资产，必须长期保留？
- 哪些表只是运行日志，可以保留 30 天、90 天或 180 天？
- 哪些 JSON 原始载荷可以压缩、裁剪或迁移出主链路？

当前讨论结论：

- `x_capture_attempts`、`editor_plugin_generation_logs`、`whale_watch_activities`、`whale_watch_hyperliquid_activities`、worker heartbeat / alert 记录先按运行日志看待，保留 7 天。
- `editor_plugin_feedbacks` 是插件 `接受 / 拒绝` 反馈，先保留 90 天。
- `editor_plugin_receipts` 是插件“已看”记录，不作为业务留存目标；后续可清理或停止写入，只保留 `接受 / 拒绝` 反馈。
- 带 `raw_payload`、`metadata`、AI 原文/输出的字段不是单独一种业务数据，而是很多表里的大字段；当前业务不关心长期留存这些字段，后续可在任务处理完成后清空或裁剪。

清理边界：

- 对仍在 `pending`、处理中、待发布、待审核等未完成状态的记录，不应提前清空可能被 worker 使用的 `metadata` 或 AI 输出。
- 对已完成、已丢弃、已失败且超过保留期的历史记录，可优先清理 `raw_payload`、AI 原始输出和非必要 `metadata`。
- 对运行日志类表，如果整表已经按 7 天保留删除，则不需要再单独清理其中的大字段。

落地原则：

- 运行日志类数据超过保留期后优先整条删除，例如 `x_capture_attempts`、`editor_plugin_generation_logs`、巨鲸历史活动、heartbeat / alert 历史。
- 插件“已看”记录 `editor_plugin_receipts` 不需要业务留存，可整条删除；后续也可讨论停止写入。
- 正式业务表不优先整条删除，只在任务完成或超过安全窗口后清理大字段，例如 `raw_payload`、AI 原始输出、非必要 `metadata`。
- 清理前先做只读空间统计，确认最大表和最大字段方向，避免先优化小头。

### 4. worker 连接、锁和心跳

涉及：

- `x_capture`
- `non_mainstream_media`
- `competitor_monitor`
- `x_processing`
- `external_media_alert`
- `auditor`
- `writer3`
- `whale_watch`
- `pipeline_supervisor`

当前架构：

- 抓取类 worker 仍会短连接写 `tasks`、来源表和 heartbeat。
- `local_pipeline` 使用服务器本地 SQLite 队列推进判断、查重、编写、发布和标题提醒。
- Supabase/Postgres 只保存原始记录、阶段结果、最终状态和失败原因。
- 生产主链路不再使用 `tasks.status + locked_by/locked_until + 多个 worker claim` 做阶段交接。
- 生产主链路不再依赖数据库 `LISTEN/NOTIFY` 唤醒处理 worker，因此不再需要长期占用 `x_process` 六个阶段和 `external_media_alert` 三个阶段的监听连接槽。

仍需关注：

- 短连接是否因为 Supabase session pooler 达到上限而失败。
- systemd 是否误启了旧分阶段 worker 或重复实例。
- 是否存在长事务、连接堆积、锁等待或异常重启。
- `local_pipeline` heartbeat 是否正常。

旧回滚入口：

- `x-process-worker` 和 `external-media-alert-worker` 命令仍保留。
- 旧 worker 的 `LISTEN` 开关默认关闭：
  - `X_PROCESS_ENABLE_NOTIFY_LISTENER=false`
  - `EXTERNAL_MEDIA_ALERT_ENABLE_NOTIFY_LISTENER=false`
- 如果紧急回滚并且确实需要数据库通知唤醒，再显式改成 `true`。

建议检查：

- `systemctl list-units 'odaily-*'`：确认实际运行了哪些服务。
- `systemctl status` / `journalctl`：确认是否频繁重启。
- Supabase `pg_stat_activity`：确认连接数、长事务、`idle in transaction`、锁等待。
- `pipeline_worker_heartbeats`：确认 `local_pipeline` 和收集器心跳是否正常、是否有重复 worker_id。

### 4.1 worker 为什么需要存在

worker 不是只用来监听“配置有没有变”。它们主要承担两类工作：

- 抓取类 worker：按当前配置持续访问外部来源，例如 X 账号、外媒站点、竞品快讯、链上/Hyperliquid 地址；发现新内容后写入数据库任务或活动表。
- 本地流水线 worker：从服务器本地 SQLite 队列领取新 job，调用判断、搜索、编写、发布、提醒等组件，把任务一步步推进，并把阶段结果写回 Supabase。

控制台前端当前不直接调用 Linux 服务器上的抓取程序。控制台只写 Supabase 配置表，例如抓哪些 X 账号、抓哪些外媒站点、是否启用。Linux worker 再读取这些配置并执行。

这里有两件不同的“问”：

- 问外部来源有没有新内容：这是抓取类 worker 的核心工作，通常必须持续做。
- 问数据库里的配置有没有变化：这是为了让控制台改配置后 worker 能生效。当前 X/外媒类 worker 按固定窗口重载配置，默认最多 5 分钟生效一次，不是每秒高频追问。

相关机制区分：

- 配置轮询：低频重新读取配置，例如 X/外媒类 worker 默认最多 5 分钟感知一次配置变更。
- Heartbeat：worker 定期写一条状态到 `pipeline_worker_heartbeats`，用于监督者判断服务是否还活着。
- 本地队列唤醒：收集器写入 `tasks` 后向 `127.0.0.1:8776/pipeline/jobs` 提交 job，`local_pipeline` 立即唤醒本地 worker，不经过 Supabase 通知。
- 数据库 LISTEN：旧分阶段 worker 的回滚机制，生产默认不启用。

可讨论替代方案：

- 保持当前模式：控制台写数据库，worker 定时重载配置。优点是简单、安全，前端不需要直接接触服务器执行权限。
- 配置变更后通过版本号或低频重载让 worker 生效。优点是仍以数据库为边界，不新增公开 Linux 控制接口。
- 新增一个受保护的 Linux 管理 API，由控制台改配置后调用它来触发 worker reload。优点是即时；缺点是要新增鉴权、权限隔离、HTTPS、审计、防误调用和运维入口。

当前倾向：

- 不建议让浏览器前端直接拥有“调用 Linux 程序”的能力。
- 如果要减少配置重载轮询，优先考虑数据库通知或轻量 reload 信号，而不是暴露通用执行接口。

### 5. 索引、Vacuum 和表膨胀

潜在问题：

- 表经常 update/delete 后，数据库内部可能留下无效空间。
- 索引也会占磁盘；无用索引会拖慢写入。
- 缺索引会导致扫大表。

可讨论优化：

- 先用只读统计确认最大表、最大索引、无效行比例、顺序扫描次数。
- 再决定是否建索引、删无用索引或做维护性 vacuum。

## 建议的只读诊断

后续如果要确认真实瓶颈，优先采集：

- 数据库总大小。
- 最大的表和索引。
- 每张表的估算行数、无效行数。
- 当前连接数和是否有长事务。
- `pg_stat_statements` 里总耗时最高、调用次数最高的 SQL。
- 插件信息流函数的执行耗时。
- 竞品监控相关查询的执行耗时。

这些诊断只读，不会修改生产数据。

本地脚本：

```bash
python tools/supabase_readonly_diagnostics.py
```

默认行为：

- 从 `.env` 读取 `SUPABASE_DB_URL` 或 `DATABASE_URL`。
- 设置只读事务模式和查询超时。
- 输出数据库总大小、连接状态、大表、大索引、表活动、可疑大字段、`pg_stat_statements` 热 SQL。
- 不执行删除、更新、建索引、VACUUM 或结构变更。

可选精确统计：

```bash
python tools/supabase_readonly_diagnostics.py --include-counts
```

`--include-counts` 会统计部分保留期候选数量，仍然只读，但可能扫描较大的历史表；优先在低峰期执行。

## 2026-06-11 只读诊断结果摘要

执行方式：

```bash
python tools/supabase_readonly_diagnostics.py --statement-timeout-seconds 15
```

实际通过生产服务器 `.env` 中的 `SUPABASE_DB_URL` 运行，只读查询，不修改数据。

总体观察：

- 数据库总大小约 `300 MB`。如果 Supabase Dashboard 显示磁盘压力明显高于这个数，需要继续拆分查看 Database / WAL / System / Storage，而不是只看业务表。
- 当前连接没有明显长事务；只看到 Supavisor / PostgREST 等常规 idle 连接，没有超过 5 分钟的 active 或 old session。
- 最大业务表是 `x_capture_attempts`，约 `96 MB`，估算 `179335` 行，占当前数据库业务数据的最大头。
- 第二大表是 `newsflash_items`，约 `59 MB`。
- 第三大表是 `tasks`，约 `40 MB`。

`pg_stat_statements` 热点：

- 最高总耗时 SQL 是 `x_capture_attempts` 的最近空跑记录查询：

```sql
SELECT finished_at, metadata
FROM x_capture_attempts
WHERE account_id = $1
  AND status = 'success'
  AND new_count = 0
  AND saved_count = 0
  AND finished_at >= $2
ORDER BY finished_at DESC, id DESC
LIMIT 1
```

统计中约 `6000` 次调用，平均约 `37 ms`，累计约 `222 s`，是当前最明确的 CPU / IO 优化目标。

- `editor_plugin_feed` 也在热 SQL 里，但优先级低于 `x_capture_attempts`：约 `237` 次调用，平均约 `35 ms`，累计约 `8.4 s`。
- `newsflash_items` upsert 调用频繁，约 `10503` 次，累计约 `4.2 s`，同时产生较多 WAL。

保留期候选统计：

- `x_capture_attempts` 当前没有超过 7 天的记录，但 7 天内数据已经约 `96 MB`；因此仅靠 7 天保留期不足以解决它的 CPU 热点，需要索引或进一步降低记录量。
- `editor_plugin_generation_logs` 超过 7 天约 `21` 行，表很小。
- `editor_plugin_receipts` 超过 7 天约 `421` 行，表约 `920 kB`，可清但不是磁盘主因。
- `whale_watch_hyperliquid_activities` 超过 7 天约 `164` 行，表约 `1 MB`，可清但不是主因。
- `pipeline_worker_heartbeats` 超过 7 天约 `303` 行，表约 `3.6 MB`，可清但不是主因。
- `pipeline_alerts` 超过 7 天约 `25` 行，表很小。

第一轮结论：

- 当前最值得优先处理的不是 worker，也不是插件轮询，而是 `x_capture_attempts` 查询缺少贴合条件的索引。
- 建议新增一个面向“按账号查最近成功空跑记录”的局部索引，方向类似：

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_x_capture_attempts_noop_recent
ON x_capture_attempts (account_id, finished_at DESC, id DESC)
WHERE status = 'success' AND new_count = 0 AND saved_count = 0;
```

- 这个索引属于数据库结构变更，执行前需要单独拍板；如果接受，应同步更新 schema 文档和实际 schema SQL。
- 历史清理策略仍然有价值，但它对当前最大 CPU 热点不是唯一解。

执行结论：

- 已拍板并在生产库执行 `idx_x_capture_attempts_noop_recent` 索引优化。
- 本地 schema 应同步包含该索引，避免后续初始化或 schema 重放遗漏。
- 生产执行结果：索引有效且 ready，大小约 `6976 kB`。
- `EXPLAIN (ANALYZE, BUFFERS)` 验证同形查询已使用 `idx_x_capture_attempts_noop_recent`，示例执行约 `0.069 ms`，命中 `shared hit=4`。

后续落地补充：

- `editor_plugin_feed` 已改为每个来源先按最近 2 小时和候选上限取数，再进入原有合并排序。
- 插件前端不再写入“已看”记录；`editor_plugin_state` 只返回最近反馈状态，已看字段保留为空值兼容旧结构。
- 新增 `maintenance-cleanup` 命令，默认 dry-run；加 `--execute` 才删除日志类数据或清理完成任务的大字段。
- X 抓取 attempt 默认保留期调整为 7 天。

维护命令：

```bash
python backend/src/main.py maintenance-cleanup
```

默认只预览命中数量，不修改数据库。确认后执行：

```bash
python backend/src/main.py maintenance-cleanup --execute
```

可调参数：

- `--retention-days`：运行日志类保留天数，默认 `7`。
- `--feedback-retention-days`：插件 `接受 / 拒绝` 反馈保留天数，默认 `90`。
- `--completed-field-retention-days`：完成任务的大字段清理安全窗口，默认 `7`。

2026-06-11 使用同形只读 SQL 在生产库 dry-run 验证通过，命中数量：

- `editor_plugin_generation_logs`：`21`
- `editor_plugin_receipts`：`428`
- `editor_plugin_feedbacks`：`0`
- `whale_watch_activities`：`0`
- `whale_watch_hyperliquid_activities`：`165`
- `pipeline_alerts`：`25`
- `pipeline_worker_heartbeats`：`303`
- `tasks` 大字段：`8667`
- `odaily_reference_items` 大字段：`8277`
- `newsflash_items` 大字段：`8539`
- `x_task_pipeline` 输出字段：`7819`
- `auditor_checks` 输出字段：`2332`
- `writer3_contexts` 输出字段：`1960`

2026-06-11 已按上述策略在生产库执行一次清理：

- 删除 `editor_plugin_generation_logs`：`21`
- 删除 `editor_plugin_receipts`：`1376`
- 删除 `editor_plugin_feedbacks`：`0`
- 删除 `whale_watch_activities`：`0`
- 删除 `whale_watch_hyperliquid_activities`：`165`
- 删除 `pipeline_alerts`：`25`
- 删除 `pipeline_worker_heartbeats`：`303`
- 清理 `tasks` 大字段：`8667`
- 清理 `odaily_reference_items` 大字段：`8277`
- 清理 `newsflash_items` 大字段：`8540`
- 清理 `x_task_pipeline` 输出字段：`7819`
- 清理 `auditor_checks` 输出字段：`2332`
- 清理 `writer3_contexts` 输出字段：`1960`

注意：Postgres 删除行或清空大字段后，表文件大小通常不会立刻下降；空间会先变为表内可复用空间。需要等待 autovacuum，或后续单独讨论是否执行 `VACUUM` / `VACUUM FULL` / 重建表等维护操作。

## 待讨论清单

- 插件信息流查询如何改写，才能不改变展示结果但减少数据库工作量。
- 哪些历史表可以设定保留周期。
- 竞品复盘数据是否需要长期保留完整原文和 raw payload。
- 是否需要为插件信息流补专用索引。
- 是否需要为 Supabase 增加定期健康检查文档或脚本。
