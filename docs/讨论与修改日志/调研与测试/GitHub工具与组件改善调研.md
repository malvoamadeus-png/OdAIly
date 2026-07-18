# GitHub 工具与组件改善调研

## 目的

本文记录一次围绕 OdAIly 的开源工具探索。当前结论只作为后续讨论、探针和 A/B 测试的候选方向，不表示已经决定替换现有组件。

调研重点不是“系统缺什么”，而是寻找能放大现有能力的成熟组件：

- 提升 LLM 调用可观测性。
- 把 Prompt、模型和规则调整变成可回归测试的流程。
- 改善外部站点正文提取与发现稳定性。
- 为查重、历史检索和此前消息召回准备更清晰的向量检索底座。
- 在现有监督者之外补充更标准的任务心跳和异常追踪能力。

## 当前 OdAIly 相关背景

OdAIly 当前已经具备较清晰的业务链路：

- Python 后端多 worker。
- 本地 SQLite 流水线队列 `local_pipeline`。
- Supabase/Postgres 作为原始记录、阶段结果、最终结果和失败原因的档案库。
- Vite/React 控制台。
- Chrome Side Panel 信息流与快讯生成插件。
- LiteLLM 本机代理承接生产文本模型调用。
- 搜索者使用在线 embedding、相似度阈值和 AI 复核做查重。

因此，不建议优先寻找一个“大一统框架”替换现有架构。更合理的方向是做小范围、可回滚、可量化收益的增强。

## 优先探索方向

### 1. LLM 调用观测：Langfuse / OpenLLMetry / Helicone

适合点：

- OdAIly 已经有 LiteLLM 统一入口。
- 判断者、搜索者 AI 复核、编写者、发布者、审核者都有不同模型别名和 Prompt。
- 线上排障经常需要知道某次模型调用的输入、输出、耗时、错误、成本和模型版本。

候选：

- Langfuse：https://github.com/langfuse/langfuse
- OpenLLMetry：https://github.com/traceloop/openllmetry
- Helicone：https://github.com/Helicone/helicone

优先级判断：

- Langfuse 最值得先看，因为它同时覆盖 LLM observability、prompt management、evals、datasets，并且明确支持 LiteLLM 集成。
- OpenLLMetry 更偏 OpenTelemetry 标准链路，适合已有通用观测栈时接入。
- Helicone 更像模型网关观测产品，若已经使用 LiteLLM，需要评估是否会重复。

建议切入：

- 先只接入非敏感的元数据、耗时、模型别名、状态码和 token/cost。
- 再选择性记录 Prompt 版本、任务 ID、阶段名、source、source_item_id。
- 正文、raw payload 和模型完整输出是否入观测平台，需要单独拍板。

### 2. Prompt 回归测试：promptfoo / DeepEval / OpenEvals

适合点：

- OdAIly 已有大量可复盘样本：判断者误判、搜索者查重边界、审核者误报、发布者 pass/reject。
- 当前 Prompt 或模型切换主要依赖人工抽样和文档记录。
- DeepSeek / GPT / 业务别名切换后，需要可重复比较。

候选：

- promptfoo：https://github.com/promptfoo/promptfoo
- DeepEval：https://github.com/confident-ai/deepeval
- OpenEvals：https://github.com/langchain-ai/openevals

优先级判断：

- promptfoo 更适合先做，因为它偏 CLI / 配置化 / CI，对“同一批案例比较多个模型或 Prompt”很直接。
- DeepEval 适合后续做更系统的 LLM 质量指标。
- OpenEvals 可作为轻量评估器参考。

建议切入：

- 从 `API替换效果测试.md` 中已经整理过的判断者、审核者样本开始。
- 新建一组离线样本：输入、期望 JSON、关键断言、允许差异。
- 每次改判断者、搜索者复核、审核者、发布者 Prompt 前先跑回归。

### 3. 正文提取与站点抓取兜底：Trafilatura / Crawl4AI / Readability

适合点：

- `non_mainstream_media` 已经有多站点特例、Jina fallback、Telegram-first 发现入口。
- 外部媒体站点 DOM、RSS、sitemap、反爬和正文结构变化频繁。
- 当前更适合增加“兜底提取器”，不适合直接替换所有站点解析逻辑。

候选：

- Trafilatura：https://github.com/adbar/trafilatura
- Crawl4AI：https://github.com/unclecode/crawl4ai
- Mozilla Readability：https://github.com/mozilla/readability

优先级判断：

- Trafilatura 是 Python 工具，和当前后端栈最贴合，适合作为正文和 metadata fallback。
- Crawl4AI 功能强，但可能引入更重的抓取运行时和策略复杂度。
- Readability 更适合网页正文清洗参考，若在 Python 后端使用需要再包一层。

建议切入：

- 先做只读探针，不进入生产入库。
- 对 CoinDesk、Cointelegraph、Decrypt、Business Insider、Tether 等站点跑 A/B。
- 比较字段：标题、canonical URL、正文长度、发布时间、作者、段落结构、失败率。

### 4. 查重与历史检索底座：LanceDB / Qdrant / pgvector

适合点：

- 搜索者现在已经做精确重复、embedding 相似度和 AI 复核。
- Odaily 历史镜像、运行中 candidate、此前消息召回都会越来越依赖相似检索。
- 当前 Supabase 主要作为档案库，本地优先方向下，可以考虑把“向量索引”也从主数据库压力中拆出来。

候选：

- LanceDB：https://github.com/lancedb/lancedb
- Qdrant：https://github.com/qdrant/qdrant
- pgvector：https://github.com/pgvector/pgvector

优先级判断：

- LanceDB 更适合先探索：embedded/local-first，和本地 SQLite 队列、服务器本地 runtime 数据目录的方向更一致。
- Qdrant 更适合数据量、并发和过滤查询都上来之后；它是独立服务，能力更强，但运维面也更大。
- pgvector 适合已经想把向量检索留在 Postgres 内部的团队；但 OdAIly 当前正在降低 Supabase 主链路依赖，优先级不一定最高。

注意：

- 这些工具不是 embedding 模型。
- 它们不负责把文本变成向量。
- 它们负责存储向量、建立索引、按相似度快速找候选。
- 继续使用 DashScope 或其他在线 embedding 完全可行。

建议切入：

- 保持在线 embedding 不变。
- 增加一个本地向量索引镜像，只保存 Odaily reference 和运行中 candidate 的向量、轻量 metadata、source_item_id。
- 搜索阶段仍以现有逻辑为准，只把候选召回从“全量/缓存扫描”逐步替换成“向量库 topK 召回”。
- 初期只做影子查询：不改变判重结果，只记录新旧召回差异。

### 5. 任务心跳与异常追踪：Healthchecks / Sentry

适合点：

- OdAIly 已有监督者和 Telegram 告警。
- 监督者负责业务语义告警，例如积压、失败窗口、心跳缺失。
- 外部健康检查或异常追踪可以补充“监督者自己是否还活着”和异常堆栈。

候选：

- Healthchecks：https://github.com/healthchecks/healthchecks
- Sentry：https://github.com/getsentry/sentry

优先级判断：

- Healthchecks 更轻，适合 worker/cron 心跳外部视角。
- Sentry 更适合捕获异常堆栈、性能问题和前端错误。

建议切入：

- 不替代 `pipeline_supervisor`。
- 先给监督者、local_pipeline、抓取类 worker 加外部 heartbeat。
- 程序异常仍由本地日志和 Telegram 作为第一响应，外部工具作为第二层保险。

### 6. 信源发现补充：RSSHub / RSS-Bridge / changedetection.io

适合点：

- 某些站点缺 RSS、RSS 不完整、页面结构容易变化。
- 可用于发现入口探针，而不是直接替换正文抓取。

候选：

- RSSHub：https://github.com/DIYgod/RSSHub
- RSS-Bridge：https://github.com/RSS-Bridge/rss-bridge
- changedetection.io：https://github.com/dgtlmoon/changedetection.io

注意：

- RSSHub / FreshRSS 等项目可能涉及 AGPL 许可证，接入方式需要谨慎。
- 更推荐作为外部服务消费，不把其代码混入 OdAIly 仓库。

## 暂不建议优先替换的方向

### 工作流编排平台：Prefect / Dagster / Temporal

候选：

- Prefect：https://github.com/PrefectHQ/prefect
- Dagster：https://github.com/dagster-io/dagster
- Temporal Python SDK：https://github.com/temporalio/sdk-python

判断：

- 它们都很成熟，但 OdAIly 当前本地流水线已经覆盖 claim、retry、stale running 回收、阶段恢复和 systemd 托管。
- 直接替换会增加部署、运维、状态迁移和排障复杂度。
- 更适合作为长期架构参考，而不是近期业务改善切入点。

### 通用任务队列：Celery / RQ / Dramatiq

候选：

- Celery：https://github.com/celery/celery
- RQ：https://github.com/rq/rq
- Dramatiq：https://github.com/Bogdanp/dramatiq

判断：

- 如果未来需要多机 worker、Redis/RabbitMQ broker、任务可视化和大规模并发，可以重新评估。
- 当前 OdAIly 的秒级新闻链路和本地优先目标下，替换收益不一定超过迁移成本。

## 推荐探索顺序

1. Langfuse：先解决“每次模型调用到底发生了什么”。
2. promptfoo：把判断者、审核者、发布者、搜索复核做成回归测试。
3. Trafilatura：做正文提取兜底探针。
4. LanceDB：做本地向量索引影子查询。
5. Healthchecks：给监督者和关键 worker 补外部心跳。
6. RSSHub / RSS-Bridge：只对不稳定信源做发现入口探针。

## 查重检索底座展开

### embedding 模型与向量数据库不是一回事

当前在线 embedding 的职责是：

```text
文本 -> 一组数字向量
```

例如一条快讯正文经过 DashScope embedding 后，可能得到一个 1024 维或 1536 维的浮点数组。这个数组表示文本语义。

LanceDB、Qdrant、pgvector 这类工具的职责是：

```text
保存这些向量
给这些向量建索引
按相似度快速找出最像当前文本的历史文本
```

所以它们不是“本地大模型”，也不替代 DashScope embedding。它们更像一个专门为语义相似度检索优化过的数据库。

### 为什么小服务器也能用

如果继续使用在线 embedding，小服务器只需要存储和检索向量，不需要运行大 embedding 模型。

存储成本通常取决于：

- 向量维度。
- 文档条数。
- 每个向量使用 float32 还是压缩格式。
- metadata 保存多少。
- 索引类型。

粗略理解：

```text
100,000 条文档 * 1536 维 * 4 bytes ~= 614 MB 原始向量
```

再加索引和 metadata，实际会更高。但对于只保留最近窗口、只保存轻量 metadata、或使用较低维 embedding 的场景，小服务器并不是完全不能承载。

OdAIly 也不一定要一开始就把全量历史都放进去。可以先只放：

- 最近 7 天或 30 天 Odaily reference。
- 运行中的 candidate。
- 编写者3 需要的 90 天历史候选。

### LanceDB 是什么

LanceDB 是一个偏 embedded/local-first 的向量数据库/检索库。

适合 OdAIly 的原因：

- 可以作为本地文件型数据资产使用，不一定一开始就跑独立服务。
- 和当前 `data/runtime/`、本地 SQLite 队列、本地优先目标比较一致。
- 用于“先做影子索引”比较方便。

可能用法：

```text
odaily_reference_items 同步/刷新
  -> 计算或读取已有 embedding
  -> 写入本地 LanceDB 表
  -> 搜索者查重时对当前任务向量做 topK 查询
  -> 返回候选给现有 AI 复核逻辑
```

优点：

- 接入轻。
- 本地优先。
- 不必先部署一个新服务。

风险：

- 多进程并发写入和生产稳定性需要实测。
- 运维、备份、索引重建策略需要补齐。
- 不能一上来替换现有查重判断，适合先做影子查询。

### Qdrant 是什么

Qdrant 是独立向量数据库服务。

适合场景：

- 向量数据量更大。
- 需要更强的过滤条件、topK 检索和并发能力。
- 未来可能有多个服务共享同一套向量索引。

优点：

- 能力强。
- API 清晰。
- 更像正式的向量检索基础设施。

风险：

- 需要多部署一个服务。
- 小服务器上要考虑内存、磁盘和后台维护。
- 对当前 OdAIly 来说，可能比 LanceDB 更重。

### pgvector 是什么

pgvector 是 Postgres 的向量扩展。

适合场景：

- 团队希望继续把向量、metadata、业务表都放在同一个 Postgres 里。
- 查询需要和 SQL 业务条件强绑定。

优点：

- 不需要另起一个向量服务。
- 可以直接用 SQL 与现有表 join。
- 备份和权限沿用 Postgres。

风险：

- 如果放在 Supabase/Postgres，会重新增加主数据库压力。
- OdAIly 当前目标是 Supabase 降级为异步档案库，不再让主链路过度依赖它。
- 如果使用本地 Postgres + pgvector，则又引入一个本地数据库服务。

### 对 OdAIly 的现实建议

第一阶段不换 embedding，也不换判重逻辑。

建议只做：

```text
在线 embedding 保持不变
现有搜索者判重保持不变
新增本地向量索引影子查询
记录新旧候选差异
观察是否减少漏判或降低扫描成本
```

如果影子查询稳定，再考虑：

1. 用向量库 topK 替代部分本地 cache 扫描。
2. 保留精确重复为第一优先。
3. 保留 AI 复核作为边界样本判断。
4. 只把向量库作为候选召回层，不让它单独决定是否重复。

推荐优先级：

```text
LanceDB 影子查询 > Qdrant 正式服务化 > pgvector
```

原因：

- LanceDB 更符合小步试验和本地优先。
- Qdrant 适合确认需求后再升级。
- pgvector 与 Supabase 降压方向略冲突，除非决定部署本地 Postgres。

## 后续可做的最小探针

### LLM 观测探针

- 选一个非关键阶段，例如审核者或搜索 AI 复核。
- 只记录模型别名、耗时、状态、任务 ID、阶段名。
- 不记录正文和完整模型输出。

### Prompt 回归探针

- 选 20 条判断者样本、20 条审核者样本、20 条发布者样本。
- 用 promptfoo 比较当前模型别名和候选模型别名。
- 输出通过率、结构化解析失败率、关键断言失败样本。

### 正文提取探针

- 针对 5 个站点各取最近 20 条 URL。
- 比较现有解析器与 Trafilatura 的标题、正文、发布时间、canonical URL。
- 只输出报告，不入库。

### 向量索引探针

- 取最近 7 天 Odaily reference。
- 复用现有在线 embedding。
- 建一个本地 LanceDB 索引。
- 对最近 100 条任务做影子 topK 查询。
- 比较现有搜索者候选与 LanceDB 候选重合度、漏召回样本和耗时。
