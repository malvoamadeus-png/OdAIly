# 收集者-X

## 职责

收集者-X 负责根据控制台配置抓取 X/Twitter 账号的公开内容，将可处理的原始内容写入 `tasks` 表，供后续判断者、搜索者和编写者处理。

当前代码实现使用 `backend/packages/x_capture` 和 `x-capture-worker` 命令，本轮只补齐文档，不改代码命名。

## 配置来源

控制台通过 Supabase 维护：

- `x_capture_settings`：全局抓取频率、并发数、抖动时间。
- `x_capture_accounts`：账号、展示名、单账号频率、启停状态、最近抓取状态。

worker 启动时主动加载配置，运行中监听 `x_capture_config_changed` 通知并刷新内存配置，不需要重启服务。

## 抓取行为

收集者-X 负责：

- 按账号频率和全局并发限制调度抓取。
- 对多账号抓取加入抖动，避免所有账号同时请求。
- 解析 X 用户名或主页 URL。
- 使用来源侧 ID 去重，避免同一推文重复入库。
- 按来源原发布时间做入口时效过滤，默认只让 10 分钟内的内容进入 `tasks`。
- 对过期或缺少发布时间的推文仍标记 seen，避免服务重启后反复尝试同一条旧内容。
- 写入抓取 attempt，记录候选数量、入库数量、错误和耗时。
- 将新内容写入 `tasks`，初始状态为 `pending`。

收集者-X 不负责：

- 判断内容是否值得发布。
- 判断快讯类型。
- 判断是否与 Odaily 已发布内容重复。
- 生成快讯文本。
- 推送后台。

## 时效过滤

收集者-X 的 10 分钟过滤是发布流水线入口规则，不是事件复盘规则。

- 判断基准使用 X 来源原始 `created_at`，不使用任务入库时间。
- 默认窗口由 `PROCESSING_FRESHNESS_WINDOW_SECONDS=600` 控制。
- 超过窗口或缺少来源时间的推文不写入 `tasks`。
- 被过滤推文仍写入 seen 集合，并在 `x_capture_attempts.metadata` 中记录 `freshness_window_seconds`、`ignored_stale_count` 和 `ignored_stale_tweet_ids`。
- 如果未来 X 也进入事件复盘，应单独写事件表，不复用 `tasks` 是否入库作为事件复盘条件。

## 写入数据

X 内容写入 `tasks` 时，至少保留：

- `source`：固定为 `x`。
- `source_item_id`：X 来源侧唯一 ID。
- `source_url`：原始 X 链接。
- `title`：可为空。
- `content`：抓取到的正文。
- `published_at`：X 来源原始发布时间。
- `raw_payload`：原始响应中的关键字段。
- `status`：初始为 `pending`。

抓取记录写入 `x_capture_attempts`，用于控制台观察抓取是否正常工作。

## 流程位置

```text
控制台 X 配置
  -> 收集者-X 抓取账号内容
  -> tasks(status=pending)
  -> 判断者
  -> 搜索者
  -> 编写者1
  -> 编写者2
  -> 后台审核
```

`tasks` 新任务和状态变化会触发后续处理 worker 的队列通知。

## 运行命令

初始化 X 抓取表：

```powershell
python backend\src\main.py x-init-db
```

运行收集者-X：

```powershell
python backend\src\main.py x-capture-worker
```

## 相关文档

- `控制台-X抓取.md`：账号和频率配置。
- `判断者.md`：X 内容预筛和路由。
- `搜索者.md`：查重和 in-flight 去重。
