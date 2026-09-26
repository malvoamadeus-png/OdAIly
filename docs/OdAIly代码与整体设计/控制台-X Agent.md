# 控制台-X Agent

## 边界

`X Agent` 是控制台一级工作区。它包含热点话题、热点自动快讯、账号目录、市场情绪和项目推介；热点自动快讯只在这里作为与热点话题平级的入口，本模块不改变其既有专项设计。

X Agent 的运行数据位于 `data/runtime/hottopic.sqlite`，但业务上独立于 X 快讯信源：不读取或修改主 SQLite 的 `x_capture_accounts`、`tasks`、`write_flow` 或发布队列。市场情绪和项目推介只生成控制台内部结果，绝不发快讯。

## 账号目录

账号以规范化 X handle 唯一。每个账号只维护三个订阅开关：

- `hot_topic_enabled`
- `market_sentiment_enabled`
- `project_promotion_enabled`

新账号默认打开热点话题。任意开关打开时，账号进入共享采集调度；全部关闭时停止采集。关闭热点话题会标记该账号尚未消费的热点 inbox 为已跳过；关闭市场情绪或项目推介会跳过尚未开始的该模块任务。因此重新打开开关只处理之后采集的新帖。控制台不会保存账号画像、分类标签或人工开关历史，运行中的模型也不会改写人工开关。旧库中的黑名单状态在迁移时转换为普通账号或三个开关都关闭的停止采集账号，不再是目录属性。

账号表服务端分页，单次最多返回 100 行。网页单行开关采用本地乐观更新和单字段短请求，未完成字段暂时禁用；成功后在当前行和总览本地更新，只有当前筛选条件会改变该行是否显示时才延迟刷新当前分页，不等待抓取、模型或全量重算。控制台进程对共享 SQLite 连接串行化读写。批量开关单次最多更新 500 个账号。

## 共享采集与分析

`odaily-hottopic.service` 对一个账号只调用一次 X 抓取。新帖以 `tweet_id` 存入共享 inbox；热点话题使用既有消费位，市场情绪与项目推介各自创建一条唯一的分析任务，因此两个模块不会相互抢占或重复调用。所有 FXTwitter 请求由同一进程节流器错峰，默认相隔 2 秒；收到 429 时暂停共享节流器 5 分钟或遵从更长的 `Retry-After`，对应账号按指数退避。这使 2,538 个账号在上游配额下逐步覆盖，不会用并发轮询反复撞限流。

市场情绪先用确定性信号过滤，再以 `gpt-5.6-luna`、`reasoning_effort=none` 提取大盘、主流 CEX Crypto、美股、指数或 ETF 的标的和五级态度。Luna 失败时改用 `gpt-5.6-terra`、`reasoning_effort=none`。纯价格、新闻转发、链上新币和无态度内容不生成情绪结果；引用帖只取被跟踪账号自己的正文，不把被引用账号的观点归给它。模型失败、超时或非法 JSON 保留在任务错误中。

项目推介采用同一模型路由，提取项目、ticker、链、合约或官网和账号给出的逻辑。调用前要求明确链上/Crypto 信号，或 `$ticker` 与代币语境同时出现；单独的 `token`、`launch`、`protocol`、`points`、`liquidity`、`contract` 或“项目”不会触发模型，因此泛 AI 或公司产品帖不会进入项目推介。合约地址优先于官网作为归并身份；没有稳定身份时以原帖隔离，不按同名项目强行合并。同一账号对同一稳定项目重复相同逻辑时只更新最近时间。主表只展示项目、链或合约、逻辑和最近提及，不显示推介强度或账号/原帖计数；展开后才显示原帖。

X Agent 从引用帖只保留被跟踪账号自己的正文作为模型输入与结果证据；被引用正文不会发送给市场情绪或项目推介提取器。

模型请求在 SQLite 写事务外执行。每轮最多领取 12 个任务，默认两路并发，单任务最多三次 worker 级尝试；5 分钟未完成的 `processing` 任务会带着原有尝试次数回收，避免 worker 重启后永久卡住，即使启动时模型路由缺失。模型路由缺失时，任务会成为可见失败而非无限 pending。失败任务可通过 `x-agent-retry-failed` 有界重排；`pending`、`processing` 和 `failed` 任务的 inbox 原帖均不会被保留清理提前删除。结果页在 SQLite 内按标的或项目归并、计数和分页，不将整个窗口结果载入控制台进程。

## 模型配置

热点话题、市场情绪和项目推介默认路由均为 `gpt-5.6-luna`，fallback 为 `gpt-5.6-terra`，两次请求都固定 `reasoning_effort=none`。部署配置已为这两个名称加入显式路由，部署后由生产 LiteLLM 提供；这三个内部模块不复用快讯编写模型。

- `X_AGENT_OPENAI_BASE_URL`：可选的兼容 API base URL；为空时依次使用 `ODAILY_LLM_BASE_URL`、`OPENAI_BASE_URL`。
- `X_AGENT_OPENAI_API_KEY`：可选的专用凭据；本机 LiteLLM 路由优先使用 `LITELLM_MASTER_KEY`，然后使用 `ODAILY_LLM_API_KEY` 或 `OPENAI_API_KEY`。
- `X_AGENT_MODEL` / `X_AGENT_FALLBACK_MODEL`：默认 `gpt-5.6-luna` / `gpt-5.6-terra`。
- `X_AGENT_ANALYSIS_WORKERS`：单个 worker 的分析并发，默认 `2`。
- `HOTTOPIC_MIN_REQUEST_INTERVAL_SECONDS`：同一 worker 中 FXTwitter 请求的最小间隔，默认 `2`。
- `HOTTOPIC_RATE_LIMIT_COOLDOWN_SECONDS`：收到 FXTwitter 429 后暂停共享请求节流器的最短秒数，默认 `300`。

## 控制台接口

全部接口要求现有控制台管理员 Bearer session：

- `/console/x-agent/dashboard`
- `/console/x-agent/accounts`
- `/console/x-agent/account`
- `/console/x-agent/subscriptions`
- `/console/x-agent/market-sentiment` 及 `/detail`
- `/console/x-agent/project-promotion` 及 `/detail`
- `/console/x-agent/retry-failed`

市场情绪支持 `1h / 24h / 7d` 窗口，默认 `24h`；项目推介支持 `24h / 7d / 30d`。两个结果页都按服务端分页加载原帖，并提供回到原帖的链接。

## 初始化

本地批量筛选必须显式传入账号 CSV 和一个或多个本地帖子快照。脚本只使用作者自己的 `text`，不把引用帖中的第三方内容归给被跟踪账号。市场情绪的 `include` 必须有三条不同的主流或 CEX 标的原帖，并且每条都含作者自己的交易或态度观点；纯价格、新闻和上所公告不够。项目推介的 `include` 必须有三条不同的明确 Crypto/链上证据原帖，并至少有一条原帖能说明项目逻辑；普通产品发布、抽奖和公告不够。默认先用确定性规则过滤，只向模型发送强候选；弱候选保留为网页待审阅，避免大量低价值调用。脚本按快照最新帖子建立默认 30 天样本窗口，并把窗口写入报告；多个旧采集批次组成的窗口必须标为 `historical_sample`，它不是连续的实时覆盖。

需要补齐当前本地样本时，使用独立的可续跑采集器。它只读取明确指定的既有账号 CSV，写入忽略的本地目录，不读取主 SQLite、X 快讯信源或发布链路。每完成一个账号就更新 `checkpoint.json`；FXTwitter 的限流、超时和被 100 条时间线截断的账号都会在输出中可见，不会被当作无内容或排除：

```bash
.venv/bin/python scripts/x_agent_collect_snapshot.py \
  --accounts /path/to/existing-x-agent-accounts.csv \
  --output-dir data/exports/x_agent_snapshot_YYYYMMDD \
  --days 30 --workers 1 --request-interval 1
```

再将该目录中的 `content_items.json` 作为 `x_agent_local_screen.py --posts` 输入。筛选过程将缓存成功判断，并写入独立进度 JSON；`model_calls` 记录实际 HTTP 请求数，单独显示 Luna、Terra、缓存命中和失败数。初始化完成后使用明确命令把经证据复核的建议导入独立账号目录；之后由管理员在网页逐项审阅和切换开关：

```bash
.venv/bin/python backend/src/main.py x-agent-import-screening \
  --report data/exports/x_agent_local_screening_historical_v8_20260926.json \
  --apply-suggestions
```

命令对新账号默认打开热点话题，并按报告的 `include` 建议打开市场情绪或项目推介。没有 `--apply-suggestions` 时，已存在账号的人工开关不会被覆盖；生产 worker 不会重读报告。
