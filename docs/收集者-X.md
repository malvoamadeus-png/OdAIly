# 收集者-X

## 职责

收集者-X 根据控制台配置抓取 X/Twitter 账号公开内容，将符合时效要求的新内容写入 `tasks`，供后续判断者、搜索者和编写者处理。

## 配置来源

控制台通过 Supabase 维护：

- `x_capture_settings`
- `x_capture_accounts`

worker 启动时加载配置。运行中不再保留专门的数据库监听连接，而是按固定窗口轮询重载配置，默认最多 5 分钟生效一次，不需要重启服务。

其中 `x_capture_accounts` 里的 `写作名` 用于后续 X 写作链路的发言人名称标准化：

- 收集者-X 继续抓取并保留真实 `author_display_name`、`author_username`
- `写作名` 不回写原始抓取正文，不覆盖原始作者字段
- 收集者-X 入库时会把当时可得的统一作者名缓存为 `tasks.metadata.effective_author_name`
- 后续 `判断者`、`编写者1` 和插件 `快讯生成` 仍会按当前账号配置重新计算 `effective_author_name`，不依赖入库时缓存值固定不变

## 入库规则

每条抓到的 X 内容在通过基础去重与时效检查后，写入：

- `tasks.source = 'x'`
- `tasks.status = 'pending'`

与作者命名相关的入库约束：

- `tasks.metadata` 继续保留原始 `author_display_name`
- `tasks.metadata` 继续保留原始 `author_username`
- `tasks.metadata` 可缓存派生后的 `effective_author_name`
- `写作名` 作为账号配置存在，不要求在任务入库时覆盖原始作者字段

判断时效使用来源原始发布时间，而不是任务入库时间。

默认时效窗：

- `PROCESSING_FRESHNESS_WINDOW_SECONDS=600`

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
