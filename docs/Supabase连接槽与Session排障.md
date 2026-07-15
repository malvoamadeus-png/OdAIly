# Supabase 连接槽与 Session 排障

## 这份文档解决什么问题

只要看到这些词，都先读这份文档：

- `connection failed`
- `max clients reached`
- `EMAXCONNSESSION`
- `ECHECKOUTTIMEOUT`
- `pool_size`
- `session mode`
- `Supavisor`
- `connection pooler`
- `idle in transaction`
- 插件登录、刷新信息流、生成快讯时提示数据库连接失败

这里说的“连接槽”不是业务表里的 session，也不是插件自己的 `editor_plugin_sessions` 登录态。它指 Supabase 连接池里能同时占用的数据库连接名额。

## 官方名字和口语说法

Supabase 当前的连接池组件叫 `Supavisor`，Dashboard 里通常叫 `Connection Pooler`。

本项目里用户口语说的：

- `连接槽`
- `session 槽`
- `15 上限那个东西`
- `pool_size: 15`

大多是在说 Supavisor 的 `session mode` 连接池名额。

官方文档里需要注意几个词：

- `Session mode`：一个客户端连接进来，通常会占住一个后端数据库连接，直到这个客户端断开。
- `Transaction mode`：连接可以按事务复用，适合很多短请求共享连接。
- `Pool Size`：池子最多能打开多少个后端数据库连接。
- `Max Client Connections`：pooler 允许多少客户端连进来，和 `Pool Size` 不是同一个概念。

参考：

- Supabase Connection management: https://supabase.com/docs/guides/database/connection-management
- Supavisor FAQ: https://supabase.com/docs/guides/troubleshooting/supavisor-faq-YyP5tI
- Supabase connection pool settings: https://supabase.com/docs/guides/troubleshooting/how-do-i-update-connection-pool-settings-in-my-dashboard-wAxTJ_

## 这次事故的原始症状

Chrome 插件里出现过类似错误：

```text
connection to server at "...pooler.supabase.com", port 5432 failed:
FATAL: (EMAXCONNSESSION) max clients reached in session mode
max clients are limited to pool_size: 15
```

含义很直白：我们连的是 Supabase pooler 的 `5432` session mode，连接池最多只给 `15` 个 session 槽，当时已经被占满了。

用户看到的是插件不能登录、不能刷新信息流、不能生成快讯。但根因不一定是插件本身。插件轻服务、worker、手工脚本、数据库监听连接、卡住的事务，只要都连同一个 Supabase session pooler，就会一起抢这 15 个槽。

## 一个 Chrome 用户会不会占一个槽

一般不是。

更准确地说：

- 用户打开一个 Chrome，不等于长期占一个 Supabase session pooler 槽。
- 浏览器直连 Supabase Auth / PostgREST 走的是 HTTPS API，不是本项目后端 `.env` 里的 psycopg 直连。
- 当前插件主要通过 `editor-plugin-api-server` 轻服务访问数据库；浏览器请求轻服务，轻服务再短连接 Supabase。
- 如果轻服务每个请求都正常打开、用完、关闭数据库连接，就不会一个用户长期占一个槽。
- 如果服务端代码保留长连接、卡在 `idle in transaction`、长期 `LISTEN`，或者多个 worker 常驻监听，才会长期占槽。

所以不要把“有多少人打开 Chrome”简单等同于“占多少连接槽”。真正要看的是服务器、Supabase pooler 和 Postgres 里实际有多少连接还活着。

## 本次前因

切换前，自动快讯链路把 Supabase 当作任务传送带：

```text
tasks.status
-> 多个 worker claim
-> locked_by / locked_until
-> LISTEN/NOTIFY 唤醒下一阶段
```

长期运行的阶段 worker 大概包括：

- `x_process_judge_crypto`
- `x_process_judge_ai`
- `x_process_search`
- `x_process_write`
- `x_process_format_publish`
- `x_process_publish`
- `external_media_alert_domain_judge`
- `external_media_alert_search`
- `external_media_alert_notify`

这些阶段如果都开数据库 `LISTEN`，每个常驻 worker 基本会长期保留一个监听连接。粗略算下来，单这两条链路就可能占掉约 `9` 个长期槽。

同时系统还有：

- X 收集器
- Crypto信源 / AI信源 / 混合信源收集器
- 竞品收集器
- 插件轻服务
- 审核者
- Writer3
- 巨鲸监控
- 监督者
- 手工排障脚本
- Supabase 自身服务连接

在 `pool_size: 15` 的 session mode 下，这个组合非常容易顶满。

## 本次已经做的改造

2026-06-15 已部署本地流水线改造。

核心变化：

- Supabase 不再当任务传送带。
- 新任务先写 `tasks` 作为档案，再提交到服务器本地 SQLite 队列。
- 本地服务 `odaily-local-pipeline.service` 监听 `127.0.0.1:8776`。
- 本地队列文件位于 `data/runtime/local_pipeline.sqlite`。
- `local_pipeline` 在一个进程里顺序调用判断、查重、编写、发布或提醒。
- `local_pipeline` 启动时会把锁定超过 `30` 分钟的本地 `running` job 回收到可重试状态，避免历史僵尸 job 长期残留。
- `GET /health` 除了队列计数外，还应返回 `worker_alive`、`worker_restarts` 和 `last_worker_error`，用于识别“主进程还活着，但后台 worker 线程已经退出”的假活状态。
- Supabase 只存原始记录、阶段结果、最终状态和失败原因。
- 旧 `tasks` notify trigger / function 已删除。
- 旧分阶段 worker 已停用并 disabled。
- 切换前未完成旧任务已标记 `legacy_skipped`。

生产切换时的事实记录：

- 旧 `legacy_skipped` 数量：`17`
- `odaily-local-pipeline.service` 已启用
- 旧 6 个 `odaily-x-process@...` inactive / disabled
- 旧 3 个 `odaily-external-media-alert@...` inactive / disabled
- `/health` 曾返回：`{"ok": true, "queue": {"succeeded": 16}}`

## 本次部署时额外发现的问题

部署 schema 初始化时，DDL 被一个旧事务挡住：

```text
state = idle in transaction
query = SELECT * FROM editor_plugin_feed(...)
age 约 3 小时
```

这个连接虽然看起来没有 CPU，但它开着事务不结束，会持有锁，导致 `CREATE TABLE / CREATE FUNCTION / CREATE OR REPLACE FUNCTION` 等 DDL 等锁，最终触发 statement timeout。

当时处理方式：

- 查 `pg_stat_activity`
- 确认阻塞关系
- 终止那个长时间 `idle in transaction` 连接
- 重新执行 schema 初始化和切换命令

这件事说明：连接槽问题不只是“连接数量多”，也包括“连接状态坏”。尤其是 `idle in transaction`，要优先处理。

## 2026-06-23 新增事故：连接槽满叠加运行期 DDL

这次同时出现两个报警面：

- Telegram 报警：`X 抓取无成功记录`，对象是 `x_capture_attempts`，详情是最近 10 分钟没有 `success attempt`。
- 插件报错：`EMAXCONNSESSION max clients reached in session mode`，提示 `pool_size: 15` 已满。

生产检查到的关键现象：

- Supabase session pooler 一度拒绝新的诊断连接，手工诊断脚本也复现 `EMAXCONNSESSION`。
- `odaily-x-capture.service` 和 `odaily-pipeline-supervisor.service` 反复因 `statement timeout` 退出并由 systemd 重启。
- `pipeline-supervisor` 重启次数曾达到数百次，`x-capture` 也有大量重启。
- `pg_stat_activity` 中出现多条 `wait_event_type = Lock`，阻塞链里有 `CREATE TABLE IF NOT EXISTS pipeline_worker_heartbeats ...`。
- `editor_plugin_feed` 曾出现长 SQL 超时和 `BrokenPipeError`，说明插件信息流查询也可能在高压时放大连接占用。
- 当插件轻服务或后台 syncer 调用 `editor_plugin_feed` / `editor_plugin_state` / 反馈 RPC 时，需要显式提交，确保 `request.jwt.claims` 所在事务及时结束，避免读请求长期停在 `idle in transaction`。当前插件刷新请求路径已本地化，不应再同步调用这些 RPC。
- 对 `x_capture_attempts` 做最近成功记录查询时，即使用 `EXISTS` 也可能触发 `statement timeout`；这张表当时不能作为监督者的首选健康信号。

这次的放大链路是：

```text
session pooler 槽位紧张
-> 插件 / worker / 监督者短连接互相争抢
-> 长驻服务重启
-> 旧代码在启动路径自动执行 init_schema / DDL
-> DDL 等 relation lock
-> heartbeat / tasks 写入被锁链拖住
-> x_capture 和 pipeline_supervisor statement timeout
-> systemd 再次重启
-> 连接和锁等待继续放大
```

当时止血动作：

- 重启 `odaily-editor-plugin-api.service`，释放插件轻服务里可能残留的连接和请求线程。
- 临时停止 `odaily-x-capture.service` 与 `odaily-pipeline-supervisor.service`，阻止旧代码继续在启动时触发 DDL。
- 用 `pg_cancel_backend(...)` 取消明确来自本轮启动 DDL / 监督者查询的阻塞会话。
- 等锁等待明显下降后，再部署修复代码。

已部署的代码修复：

- `x-capture-worker` 启动时不再执行 `repository.init_schema()`。
- `pipeline-supervisor` 启动时不再执行 `repository.init_schema()`。
- `editor-plugin-api-server` 启动时不再执行插件表、X 表、X processing 表和 prompt seed 初始化。
- `local-pipeline` 启动时不再自动执行远端 schema 初始化。
- 监督者检查 X 抓取健康时，优先看 `pipeline_worker_heartbeats` 里的 `x_capture` 成功心跳。
- 只有没有成功心跳时，监督者才 fallback 查 `x_capture_attempts`；fallback 使用 `EXISTS`，并且查询失败只记日志和继续告警，不让监督者进程崩溃。

部署和恢复事实：

- 修复提交部署到生产后，服务快进到 `64c32fe`。
- `odaily-editor-plugin-api.service`、`odaily-local-pipeline.service`、`odaily-x-capture.service`、`odaily-pipeline-supervisor.service` 均恢复 active。
- `x_capture` 日志恢复为多个账号连续 `status=success`。
- `local_pipeline`、`x_capture`、`competitor_monitor`、`non_mainstream_media`、`whale_watch`、`whale_watch_hyperliquid` 最新心跳均恢复为 `ok`。
- `x_capture_attempts` 最近成功记录查询仍可能超时；这属于 attempts 表的独立性能/维护问题，不能再让它拖垮监督者。

后续遇到同类问题时，不要只重启服务。先判断重启是否会触发启动期 DDL；如果当前代码或分支还没有上述修复，重启可能会制造更多锁等待。

## 当前生产期望状态

正常状态应该是：

- `odaily-local-pipeline.service` active
- `odaily-x-capture.service` active
- `odaily-non-mainstream-media.service` active
- `odaily-competitor-monitor.service` active
- `odaily-pipeline-supervisor.service` active
- `odaily-editor-plugin-api.service` active
- 旧 `odaily-x-process@...` inactive / disabled
- 旧 `odaily-external-media-alert@...` inactive / disabled
- Supabase 里不存在：
  - `trg_tasks_x_queue_notify`
  - `notify_x_task_queue_changed()`
  - `trg_tasks_external_media_alert_queue_notify`
  - `notify_external_media_alert_task_queue_changed()`
- `pipeline_worker_heartbeats.component = local_pipeline` 持续更新
- 常驻服务启动路径不自动执行 schema / RPC / 索引初始化；初始化只由显式 `*-init-db` 命令承担
- 插件信息流请求路径读写 `data/runtime/editor_plugin_local.sqlite`；后台 syncer 低频回填 Supabase feed 并异步同步反馈

## 长期治理方向：本地优先，Supabase 档案库

连接槽问题不能只靠增大 `pool_size` 或反复重启服务解决。长期目标是减少插件信息流和主生产链路对 Supabase session pooler 的同步依赖。

目标架构：

- 插件信息流刷新读取 Linux 本地 feed store，不在请求路径实时调用 Supabase/Postgres 聚合查询，也不由刷新请求触发 `editor_plugin_feed`。
- 插件 `接受 / 拒绝` 反馈先写 Linux 本地 feedback queue，再由异步 syncer 写入 Supabase。
- 采集、处理、审核、巨鲸、Writer3 等生产事件优先写 Linux 本地库或本地队列。
- Supabase 只承担异步档案库、控制台查询源、历史复盘源和备份目标。
- Supabase 短时不可用、session pooler 满或远端 SQL 变慢时，不应阻断插件已有消息展示，也不应阻断主生产链路继续推进。

当前第一阶段实现中，插件轻服务请求路径已经本地化；后台 syncer 仍会低频调用 `editor_plugin_feed` 回填本地 feed，并调用反馈 RPC 归档本地反馈。因此，排障时要区分“插件刷新请求”与“后台 syncer”：前者不应再同步占用 Supabase session pooler，后者如果失败应只造成回填或归档延迟。

完整迁移边界见 `docs/本地优先与Supabase档案库架构.md`。该文档描述的是目标架构和后续实现计划；当前代码尚未完成所有 worker 直接写本地 feed store，因此还没有完全移除后台 Supabase RPC 依赖。

## 快速检查命令

### 服务器服务状态

```bash
systemctl list-units 'odaily-*' --all --no-pager --plain
```

重点看：

```bash
systemctl is-active \
  odaily-local-pipeline.service \
  odaily-x-capture.service \
  odaily-non-mainstream-media.service \
  odaily-competitor-monitor.service \
  odaily-pipeline-supervisor.service \
  odaily-editor-plugin-api.service
```

旧 worker 应该是 inactive：

```bash
systemctl is-active \
  odaily-x-process@judge_crypto.service \
  odaily-x-process@judge_ai.service \
  odaily-x-process@search.service \
  odaily-x-process@write.service \
  odaily-x-process@format_publish.service \
  odaily-x-process@publish.service \
  odaily-external-media-alert@domain_judge.service \
  odaily-external-media-alert@search.service \
  odaily-external-media-alert@notify.service || true
```

### 本地流水线健康

```bash
curl -fsS http://127.0.0.1:8776/health
```

正常会看到类似：

```json
{"ok": true, "queue": {"succeeded": 16}}
```

如果有 `failed`，看日志：

```bash
journalctl -u odaily-local-pipeline.service -n 120 --no-pager
```

如果 `/health` 里出现 `worker_alive = false`，或 `last_worker_error` 持续出现 `EMAXCONNSESSION` / `ECHECKOUTTIMEOUT`，优先按下面的连接槽排障步骤查 `pg_stat_activity`，不要只看 systemd 上的 `active`。

### 查数据库连接状态

在服务器 `/opt/OdAIly` 里执行：

```bash
. .venv/bin/activate
python - <<'PY'
import os
from dotenv import load_dotenv
load_dotenv('/opt/OdAIly/.env')
import psycopg
from psycopg.rows import dict_row

with psycopg.connect(os.environ['SUPABASE_DB_URL'], row_factory=dict_row, connect_timeout=10, autocommit=True) as conn:
    rows = conn.execute("""
        SELECT state, wait_event_type, wait_event, count(*)::int AS count
        FROM pg_stat_activity
        WHERE datname = current_database()
        GROUP BY state, wait_event_type, wait_event
        ORDER BY count DESC
    """).fetchall()
    for row in rows:
        print(row)
PY
```

重点看：

- `idle in transaction` 是否存在
- `active` 是否长时间卡住
- `wait_event_type = Lock` 是否很多
- 连接总数是否明显接近上限

### 查长事务和锁等待

```bash
. .venv/bin/activate
python - <<'PY'
import os
from dotenv import load_dotenv
load_dotenv('/opt/OdAIly/.env')
import psycopg
from psycopg.rows import dict_row

with psycopg.connect(os.environ['SUPABASE_DB_URL'], row_factory=dict_row, connect_timeout=10, autocommit=True) as conn:
    rows = conn.execute("""
        SELECT pid, usename, state, wait_event_type, wait_event,
               now() - query_start AS age,
               left(regexp_replace(query, '\\s+', ' ', 'g'), 220) AS query
        FROM pg_stat_activity
        WHERE datname = current_database()
        ORDER BY query_start NULLS LAST
        LIMIT 50
    """).fetchall()
    for row in rows:
        print(row)
PY
```

如果看到几个小时的 `idle in transaction`，尤其是插件、手工脚本或控制台相关查询，要优先判断是否可以终止。

### 查阻塞链

```bash
. .venv/bin/activate
python - <<'PY'
import os
from dotenv import load_dotenv
load_dotenv('/opt/OdAIly/.env')
import psycopg
from psycopg.rows import dict_row

with psycopg.connect(os.environ['SUPABASE_DB_URL'], row_factory=dict_row, connect_timeout=10, autocommit=True) as conn:
    rows = conn.execute("""
        SELECT blocked.pid AS blocked_pid,
               blocked.wait_event_type,
               now() - blocked.query_start AS blocked_age,
               left(regexp_replace(blocked.query, '\\s+', ' ', 'g'), 240) AS blocked_query,
               blocker.pid AS blocker_pid,
               blocker.state AS blocker_state,
               now() - blocker.query_start AS blocker_age,
               left(regexp_replace(blocker.query, '\\s+', ' ', 'g'), 240) AS blocker_query
        FROM pg_stat_activity blocked
        JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS bp(pid) ON true
        JOIN pg_stat_activity blocker ON blocker.pid = bp.pid
        WHERE blocked.datname = current_database()
        ORDER BY blocked.query_start
        LIMIT 30
    """).fetchall()
    for row in rows:
        print(row)
PY
```

如果看到 `CREATE TABLE IF NOT EXISTS ...`、`CREATE INDEX ...` 或 `CREATE OR REPLACE FUNCTION ...` 挡住普通 heartbeat / tasks 写入，先停掉触发 DDL 的服务或脚本，再处理 blocker。

### 查旧 LISTEN 是否还在

```bash
. .venv/bin/activate
python - <<'PY'
import os
from dotenv import load_dotenv
load_dotenv('/opt/OdAIly/.env')
import psycopg
from psycopg.rows import dict_row

with psycopg.connect(os.environ['SUPABASE_DB_URL'], row_factory=dict_row, connect_timeout=10, autocommit=True) as conn:
    rows = conn.execute("""
        SELECT pid, state, left(query, 160) AS query
        FROM pg_stat_activity
        WHERE query ILIKE '%x_task_queue_changed%'
           OR query ILIKE '%external_media_alert_task_queue_changed%'
    """).fetchall()
    for row in rows:
        print(row)
PY
```

正常不应该有旧 worker 的长期 `LISTEN x_task_queue_changed` 或 `LISTEN external_media_alert_task_queue_changed`。

## 什么时候可以终止连接

可以优先考虑终止：

- 超过 10 分钟的 `idle in transaction`
- 已确认来自旧脚本、旧 worker、排障命令的连接
- 挡住 DDL 的连接
- 明确属于已停用服务的连接

谨慎处理：

- 正在 `active` 且运行时间很短的业务查询
- Supabase Auth / Storage / PostgREST 内部连接
- 不确定来源的连接

终止命令示例：

```sql
SELECT pg_terminate_backend(<pid>);
```

不要批量乱杀。先看 `query`、`state`、`age`、`wait_event`，再动手。

## 编码和运维规则

### 代码规则

- 长驻 worker 启动时不要自动跑 DDL。
- DDL 只放在显式 `*-init-db` 命令里。
- 数据库连接用完必须关闭，优先使用 `with psycopg.connect(...) as conn`。
- 后端共享连接参数默认开启 `POSTGRES_CONNECT_TIMEOUT_SECONDS=10`、`POSTGRES_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS=60000`，避免连接无限卡住或事务意外挂成长期 `idle in transaction`。
- 各常驻服务连接都要带固定 `application_name`，这样排查 `pg_stat_activity` 时能直接看出是 `x_capture`、插件轻服务还是其他 worker 在占槽。
- 长驻 HTTP 服务不要保留一个全局 psycopg 连接不放。
- 不要在事务里等待外部 HTTP、AI、Telegram、抓网页。
- 需要常驻缓存时，缓存数据，不缓存打开的数据库事务。
- 使用 Supabase session pooler 时，默认把每次 DB 操作当作短连接。

### 服务规则

- 不要重新启用旧 `odaily-x-process@...` 和 `odaily-external-media-alert@...`，除非明确回滚。
- 如果紧急回滚旧 worker，先确认 `X_PROCESS_ENABLE_NOTIFY_LISTENER=false` 和 `EXTERNAL_MEDIA_ALERT_ENABLE_NOTIFY_LISTENER=false`，避免旧 worker 一启动就占长期监听槽。
- 旧 `x-process-worker` 命令现在也不再在启动时自动执行 `init_schema()`；回滚分阶段 worker 之前，先显式跑一次 `x-process-init-db`，不要靠重启服务顺带迁移。
- 新任务交接走 `odaily-local-pipeline.service`，不是数据库 `LISTEN/NOTIFY`。
- `data/runtime/` 是服务器本地运行资产，不进 Git。

### 排障规则

- 先查实际连接，再猜原因。
- 先停新增连接来源，再做 DDL。
- 先处理 `idle in transaction`，再考虑扩容或改 pool size。
- 不要只看业务服务数量，还要看 Supabase 内部连接和插件轻服务连接。
- 不要把“一个 Chrome 用户”直接等同于“一个 session 槽”。

## 常见误解

### 误解 1：连接槽满了就是用户太多

不一定。更常见的是服务端长连接、监听连接、空闲事务或重复 worker。

### 误解 2：只要提高 pool_size 就好了

不一定。Supabase 文档提醒，pooler 占太多连接会挤压 Auth、Storage、PostgREST 等其他服务的连接空间。先修连接泄漏和长期占用，再讨论扩容。

### 误解 3：worker 心跳就是长期连接

不是。heartbeat 是定期写一条状态。正常写完就关连接。长期占槽的是没有关闭的连接、监听连接或卡住事务。

### 误解 4：`editor_plugin_sessions` 和 pooler session 是一回事

不是。`editor_plugin_sessions` 是插件登录态表。Pooler session 是数据库连接池里的连接会话。

## 后续如果再出 session 问题

处理顺序：

1. 截图或复制完整错误，尤其是 `EMAXCONNSESSION / ECHECKOUTTIMEOUT / pool_size`。
2. 查 `systemctl list-units 'odaily-*'`，确认旧阶段 worker 没被重新启用。
3. 查 `curl http://127.0.0.1:8776/health`，确认本地流水线正常。
4. 查 `pg_stat_activity`，看连接总数、`idle in transaction`、锁等待和旧 LISTEN。
5. 必要时终止明确异常的长事务。
6. 查 `pipeline_worker_heartbeats` 最新状态。
7. 再决定是否需要改代码、改服务、改 Supabase pool size 或升级计算规格。

不要一上来就重启所有服务。重启可能短时间制造更多连接，也可能掩盖真正的长事务或泄漏来源。
