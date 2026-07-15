# 本地优先与 Supabase 档案库架构

## 文档状态

本文记录已经接受的长期架构方向：插件信息流本地化、主生产链路本地优先、Supabase 从实时依赖降级为异步档案库。

这是一份目标架构和迁移方向文档。当前代码已经完成插件轻服务请求路径本地化，并已让核心业务 worker 直接写本地 feed store；后台 syncer 仍低频同步 Supabase，作为回填、补偿和反馈归档路径。排障时仍要以当前实现文档和实际代码为准。

## 背景

Supabase session pooler 的 `pool_size` 有固定上限。插件刷新、轻服务、worker、监督者、手工脚本和卡住事务如果都同步连接同一个 session pooler，就会在短时间内互相争抢连接槽，触发 `EMAXCONNSESSION`、`ECHECKOUTTIMEOUT` 或 `max clients reached`。

短期 guardrail 可以减少泄漏和长事务，例如连接超时、`idle_in_transaction_session_timeout`、固定 `application_name`、启动路径不跑 DDL。但这类修补不能从根上解决“值班插件展示”和“生产主链路推进”被 Supabase session pooler 短时状态拖住的问题。

长期目标是让 Linux 本机运行资产成为实时工作面，Supabase 只承担异步档案、控制台查询、历史复盘和备份角色。

## 核心结论

- 插件信息流刷新只读 Linux 本地 feed store，不实时查 Supabase，也不触发 `editor_plugin_feed` 聚合查询。
- 采集、处理、审核、巨鲸、Writer3 等生产事件优先写 Linux 本地库或本地队列。
- Supabase 保留为异步档案库、控制台查询源、历史复盘源和备份目标。
- Supabase 短时不可用、session pooler 满、远端 SQL 变慢或远端 DDL 等锁，不应阻断插件信息流展示和主生产链路推进。
- 插件侧的“最近消息、分区展示、刷新、低频区/高频区/AI区”应由本地读模型提供，而不是每次刷新时跨多张 Supabase 业务表聚合。

## 目标数据流

```text
worker -> Linux local store -> plugin
```

生产 worker 把可展示事件、状态变化和业务结果先写入 Linux 本地 store。插件轻服务读取本地 feed store，生成和现有 `/plugin/feed/items` 兼容的卡片结构。

```text
Linux local store -> async syncer -> Supabase
```

本地 store 中需要留档、复盘或控制台查询的数据，由异步 syncer 写入 Supabase。syncer 失败时应按幂等键重试，不反向阻断本地主链路。

```text
plugin feedback -> local feedback queue -> async syncer -> Supabase
```

插件的 `接受 / 拒绝` 反馈先写本地反馈队列，立即影响插件本地状态；再由异步 syncer 写入 Supabase，用于长期统计和复盘。

## 分层目标

### 第一阶段：插件信息流本地化

目标是优先解决插件刷新触发 Supabase 连接池压力的问题。

计划边界：

- `/plugin/feed/items` 改为读取 Linux 本地 feed store。
- `/plugin/feed/state` 改为读取本地反馈状态和本地已处理状态。
- `/plugin/feed/feedback` 先写本地反馈队列，再异步同步到 Supabase。
- 本地 feed store 必须能覆盖当前信息流的五类展示：`新快讯`、`Crypto信源标题提醒`、`审核者`、`此前消息`、`巨鲸`。
- 插件刷新失败时，优先暴露本地轻服务或本地 store 问题，而不是把 Supabase pooler 状态作为第一故障面。

这一阶段完成后，插件常规刷新不应再调用 `editor_plugin_feed`，也不应因为 Supabase session pooler 满而无法展示已有本地消息。当前代码已完成这一阶段的请求路径本地化，并已让核心 feed 类型由 worker 直接写本地 store；后台 syncer 仍保留为回填补偿。

### 第二阶段：主生产链路本地优先

目标是减少采集和处理阶段对 Supabase 的同步依赖，让生产推进以 Linux 本地队列和本地库为主。

计划边界：

- 收集者先写本地队列或本地业务库，再进入处理流程。
- 判断者、搜索者、编写者、发布者、审核者、Writer3、巨鲸等事件先落本地运行资产。
- 对生产推进必需的状态，以本地状态为准；Supabase 写入失败只影响归档和控制台延迟。
- 本地队列必须具备幂等键、失败重试、卡住任务回收和可观测心跳。

当前仓库已经有 `local_pipeline` 和 `data/runtime/local_pipeline.sqlite` 作为本地流水线基础，但这不等于所有生产读写都已经完成本地优先改造。后续实现需要逐步收口所有仍同步依赖 Supabase 的关键路径。

### 第三阶段：Supabase 异步归档与补偿同步

目标是在不影响本地主链路的前提下保留控制台、复盘和长期留存能力。

计划边界：

- syncer 从本地 outbox 或变更表读取待同步事件。
- 写 Supabase 使用幂等键和 upsert，避免重复同步造成重复卡片或重复反馈。
- Supabase 不可用时，syncer 退避重试并暴露告警，不阻断插件和生产主链路。
- 控制台短期仍可继续读 Supabase，但必须接受与插件本地视图存在短暂延迟。
- 长期可以按模块逐步把控制台关键读模型也迁移为本地 API 或本地快照，但这不是第一阶段目标。

## 必须保持的业务契约

### 信息流卡片字段

本地 feed store 对插件暴露的字段必须兼容现有 `/plugin/feed/items` 返回结构：

```text
feed_item_id
feed_kind
lane
priority
title
summary
badges[]
status_label
status_tone
occurred_at
source_url
detail_url
action_schema
meta_json
```

字段可以在后续新增，但不能无迁移地删除或改名。插件前端不应因为数据来源从 Supabase RPC 改为本地 store 而重写展示语义。

### `feed_kind`

现有类型语义保持不变：

```text
newsflash
external_media_alert
auditor_alert
writer3_context
whale_onchain
whale_hyperliquid
```

新增类型必须先更新 `docs/信息流插件.md` 和对应生产模块文档。

### `lane`

现有分区语义保持不变：

```text
high
ai
low
```

- `high`：普通新快讯、Crypto信源标题提醒、审核者。
- `ai`：AI信源全文和标记为 `AI信源` 的 X 账号新快讯。
- `low`：此前消息、巨鲸。

本地化只改变读模型来源，不改变分区语义。

### 反馈动作

`接受 / 拒绝` 语义保持不变：

- `审核者` 的反馈表示编辑是否认可审核者提醒成立。
- `此前消息` 的反馈表示编辑是否认可此前消息质量。
- 反馈不直接撤稿、不直接改正文、不直接修改主发布状态。
- 反馈必须记录编辑身份、动作时间和原始卡片标识。

### 插件鉴权

插件登录和权限短期仍可由 Linux 轻服务承接。鉴权可以继续复用 Supabase Auth 用户和 `editor_plugin_users` 白名单，但插件信息流刷新不应因此重新依赖 Supabase 聚合查询。

如果后续连登录也要完全本地化，需要另行设计本地用户缓存、密码校验、撤权同步和失效策略；这不属于第一阶段 feed 本地化的必要条件。

### 控制台延迟

控制台短期仍可继续读 Supabase。由于 Supabase 变成异步档案库，控制台看到的数据可以相对插件本地视图存在短暂延迟。

这个延迟是目标架构允许的结果，不应为了让控制台零延迟而重新让生产主链路同步等待 Supabase。

## 当前实现与目标实现的边界

当前实现包含这些事实：

- 插件浏览器侧调用 `editor-plugin-api-server`。
- `editor-plugin-api-server` 的信息流请求路径当前读写 `data/runtime/editor_plugin_local.sqlite`。
- 新快讯、Crypto信源标题提醒、审核者、Writer3 此前消息、链上巨鲸和 Hyperliquid 巨鲸当前已由对应 worker 直接写入本地 feed store。
- 后台 syncer 当前仍会调用 `editor_plugin_feed` 回填本地 feed，并调用 `editor_plugin_submit_feedback` 异步归档本地反馈。
- `editor_plugin_feed` 仍在 Supabase/Postgres 侧聚合多张业务表，但不再由插件刷新请求同步触发。
- 主写作链路已经通过 `local_pipeline` 减少了旧 `tasks.status + LISTEN/NOTIFY + 多 worker claim` 的阶段交接依赖。
- Supabase 当前仍保存原始记录、阶段结果、最终状态、失败原因和插件反馈等复盘数据。

目标实现是：

- 插件常规信息流刷新读取 Linux 本地 feed store。
- 插件反馈先进入 Linux 本地 feedback queue。
- worker 生产事件优先写 Linux 本地队列或本地业务库。
- Supabase 写入变成异步归档和补偿同步。

因此，后续排障必须区分“插件请求路径和核心 feed 产出已经本地化”和“后台 syncer / 其他未迁移路径仍可能使用 Supabase”。不能把本文的最终目标描述当成所有生产写入都已经本地优先的事实。

## 实现原则

- 本地 store 是实时工作面的主读源，Supabase 是异步档案库。
- 本地写入必须先于远端归档；远端失败不能回滚本地已完成事件。
- 同步必须幂等，所有事件、卡片和反馈都要有稳定业务键。
- 插件接口保持兼容，优先减少前端重写面。
- 不为了“实时”使用 Postgres session 长连接、`LISTEN/NOTIFY` 或跨多表实时聚合。
- 不在长驻服务启动路径执行远端 DDL、RPC 初始化或索引初始化。
- 本地运行资产位于服务器本地，不纳入 Git；repo 只保存代码、schema 初始化命令和文档。

## 验收口径

第一阶段完成的最低验收口径：

- 插件 `/plugin/feed/items` 常规刷新不再调用 `editor_plugin_feed`。
- Supabase session pooler 满时，插件仍能展示已经写入本地 feed store 的最近消息。
- 插件 `接受 / 拒绝` 可以先写本地队列并在本地状态中立即体现。
- Supabase 恢复后，syncer 能把本地 feed 事件和反馈补偿同步到 Supabase。
- 文档和日志能明确区分本地 store 故障、syncer 故障和 Supabase 归档故障。

第二阶段完成的最低验收口径：

- 采集和处理推进不再因为 Supabase session pooler 短时满而整体停止。
- Supabase 写入失败只产生归档延迟和告警，不影响本地队列继续推进。
- 本地队列和本地业务库具备足够的健康检查、积压指标和失败重试可见性。

第三阶段完成的最低验收口径：

- Supabase 中的档案数据可用于控制台查询、历史复盘和备份恢复。
- syncer 可以从本地 outbox 安全补偿历史失败写入。
- 控制台对短暂延迟有明确提示或可接受的产品约束。
