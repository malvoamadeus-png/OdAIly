# 控制台-Gate行情播报

## 职责与边界

`Gate行情播报` 是独立行情监控与发布模块，业务实现包为
`backend/packages/gate_market_broadcast/`。它与既有的 `gate-tradfi`
定时综合简报没有任务、配置、数据库或 worker 关系，也不进入
`tasks`、判断者、搜索者、编写者1或发布者状态机。

模块只复用三个全系统基础能力：

- Gate TradFi HTTP 行情能力的接口模式，但使用独立客户端实现。
- 编写者2的确定性 `format_brief` 格式整理函数；不进入
  `format_publish` 阶段，也不写 `x_task_pipeline`。
- 通用 `PushClient` 和 Telegram 客户端。

## 标的与播报步长

运行时配置唯一来源是 Linux 本地
`data/runtime/gate_market.sqlite`：

| 代码 | 中文名 | 播报步长 | 单位 |
|---|---|---:|---|
| `EUSTX50` | 欧洲斯托克50 | 100 | 点 |
| `UK100` | 英国富时100 | 150 | 点 |
| `GER40` | 德国DAX 40 | 400 | 点 |
| `XBRUSD` | 布伦特原油 | 2 | 美元/桶 |
| `USDJPY` | 美元兑日元 | 0.5 | 无 |
| `USDCNH` | 美元兑人民币 | 0.010 | 无 |
| `XAUUSD` | 黄金 | 50 | 美元/盎司 |
| `XAGUSD` | 白银 | 3 | 美元/盎司 |

代码内的同值配置只用于首次建库。建库后 worker 和控制台都读取
SQLite，不再从旧 Gate JSON 配置读取。

## 触发状态机

- 默认每60秒读取一次 Gate TradFi ticker 的 `last_price`；缺少
  `last_price` 时才使用 bid/ask 中间价。
- 价格网格永远以0为原点，网格线是播报步长的整数倍。
- 快照从网格线一侧跨到另一侧即视为触发，不要求报价精确等于网格线。
- 单次快照跨越多条网格线时只生成一条，使用运动方向上的最远网格线；
  本次跨过的全部网格线都进入已触发状态。
- 一条网格线触发后，价格必须到达它的任一相邻网格线，才重新具备
  返回触发资格。仅在网格区间内部反复靠近不触发。
- 首次启动、阈值修改后首次报价和 worker 长时间
  中断恢复都只建立基线，不补发历史突破。
- `backend` 与 `live` 只决定推送接口的发布参数；两种模式共享同一份
  标的触发状态。切换模式不会重新建立行情基线，也不会让同一网格线
  绕过防重复规则。旧版按模式保存的状态在初始化时合并，已锁定网格线
  取并集。

## 24小时与休市口径

模块保留最近48小时分钟价格：

- 能在目标时刻 `当前时间-24h` 之前60分钟内找到报价时，使用该报价
  作为24小时基准，并用滚动24小时样本计算最高价和最低价。
- 找不到时优先使用 Gate ticker 的上一交易时段收盘价，文案改为
  `较上一交易时段收盘上涨/下跌`，高低价使用当前交易时段数据。
- 上一交易时段收盘也不可用时，使用本交易时段开盘价，文案改为
  `开盘以来上涨/下跌`。
- 现价等于参考价，或涨跌幅四舍五入到一位小数后为 `0.0%` 时，
  本次网格状态仍被消耗，但不生成文本、不调用发布接口。

文案分类：

- 现价高于参考价，且距窗口最高价不超过一个播报步长：`上涨突破`。
- 现价高于参考价，且距窗口最高价超过一个播报步长：`短时回调`。
- 现价低于参考价，且距窗口最低价不超过一个播报步长：`下跌至`。
- 现价低于参考价，且距窗口最低价超过一个播报步长：`短时反弹`。

## 文本与编写者2

模板由 SQLite `templates` 表保存，控制台只读展示。标题末尾使用：

- `24小时上涨X.X%`
- `24小时下跌X.X%`

正文使用：

- `24小时涨幅X.X%`
- `24小时跌幅X.X%`

休市降级时，标题和正文都替换为 `较上一交易时段收盘...` 或
`开盘以来...`。标题中的触发价格使用网格线，正文同时展示 Gate 实际
现价。指数、原油和贵金属在标题与正文中都使用完整单位；外汇对不加单位。

生成后的标题和正文调用 `format_brief` 做确定性空格、标点和
`Odaily星球日报讯 ` 前缀整理。这里不创建正式编写者2阶段任务。

## 发布模式

- `backend`：真实调用 Push Data API，但固定
  `isPublish=false,isPush=false`，内容只进入后台。
- `live`：真实调用 Push Data API，固定
  `isPublish=true,isPush=false`。
- 不传 `sourceUrl`。
- 发布接口没有幂等键，因此每个触发事件只请求一次；失败或超时后记录
  `push_failed` 并放弃，不自动重发。
- SQLite 先原子写入事件和已消耗网格状态，再调用发布接口。进程在请求
  前后崩溃都不会自动重发同一事件。

## SQLite 与保留

默认路径为 `data/runtime/gate_market.sqlite`，可通过
`GATE_MARKET_DB_PATH` 覆盖。数据库使用 WAL、30秒 busy timeout 和
短连接事务。

- `settings`：当前模式和轮询周期。
- `symbol_config`：标的、阈值、显示精度和单位。
- `templates`：四类固定标题与正文模板。
- `symbol_state`：每个 `symbol` 只使用一条共享防重复状态；表内
  `mode='shared'` 是兼容旧表结构的固定存储键，不代表发布模式。
- `price_samples`：仅保留最近48小时。
- `trigger_events`：仅保留最近100条诊断结果。
- `alert_state`：Telegram 故障告警去重与恢复状态。

不保存每分钟原始 JSON，行情、触发和配置只写独立本地 SQLite。

## CLI

```powershell
python backend/src/main.py gate-market init-db
python backend/src/main.py gate-market status
python backend/src/main.py gate-market run --once
python backend/src/main.py gate-market run
python backend/src/main.py gate-market set-mode backend
python backend/src/main.py gate-market set-mode live
python backend/src/main.py gate-market set-threshold XBRUSD 2
python backend/src/main.py gate-market backtest --days 90
```

`set-threshold` 会同时清空该标的在两种模式下的旧状态；下一次报价只建立
新基线，不发布。

## 环境变量

- `GATE_MARKET_DB_PATH`
- `GATE_MARKET_API_BASE`
- `GATE_MARKET_REQUEST_TIMEOUT_SECONDS`
- `GATE_MARKET_GATE_MAX_ATTEMPTS`
- `GATE_MARKET_TELEGRAM_MESSAGE_THREAD_ID`
- `GATE_MARKET_ALERT_DEDUP_MINUTES`
- `GATE_MARKET_DISK_FREE_ALERT_MB`
- `ODAILY_PUSH_ENDPOINT`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_MESSAGE_THREAD_ID`
- `TELEGRAM_TIMEOUT_SECONDS`

## 运行与失败处理

生产 worker 使用独立的
`deploy/odaily-gate-market-broadcast.service`。休市标的改为约15分钟
检查一次，并根据 Gate `next_open_time` 在临近开市时恢复分钟轮询。

开市状态连续5分钟无法成功取得 ticker、历史回填失败、发布失败、
SQLite/worker 异常和磁盘剩余空间低于默认500MB时发送 Telegram 系统
告警；同一故障30分钟去重，恢复后发送一次恢复通知。模块不写现有
主 SQLite 监督者心跳。

## 控制台

控制台新增只读一级页面 `Gate行情播报`，通过
`GET /console/gate-market/get` 读取 SQLite 快照。接口继续使用现有
本地操作者 session 校验，但返回的业务数据
完全来自 Linux SQLite。

页面只展示运行模式、轮询周期、8个标的的阈值/现价/状态、四类文本模板
和最近触发结果，不提供修改、启停或发布按钮。页面仅在首次进入或点击
顶部刷新时请求，不自动轮询。
