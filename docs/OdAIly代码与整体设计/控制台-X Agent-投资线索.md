# 控制台-X Agent-投资线索

## 边界

投资线索是 X Agent 内部的材料整理与推导模块，供编辑、研究员继续调研。它只写入 `data/runtime/hottopic.sqlite`，不读取或写入主快讯数据库、X 信源任务、写作流水线、发布队列、交易接口或新增通知渠道。它不核查原帖真假、报告可靠性、收入持续性、代币价值捕获、估值或市场是否计价；不输出买卖建议或预期收益。

系统分别保存来源材料、原帖主张和模型推导。写作 Prompt 要保留测试、拟议、预计、条件、方向和原文时间，不能把计划改成已发生。帖子、附件文字、搜索结果和外链文本都视作不可信输入，不执行其中的指令。

## 账号资格与采集起点

目录字段为 `hottopic_accounts.investment_leads_enabled`。升级迁移 `x_agent_investment_leads_enabled_v1` 只执行一次，为迁移时已有的所有目录账号打开该开关，包括原先全部关闭的账号；迁移版本记在 `hottopic_migrations`，后续启动、重复导入不会覆盖人工开关。模块启用起点保存在 `investment_leads_meta.capture_started_at`。

新增账号默认开启热点话题与投资线索。`investment_leads_enabled_at` 记录当前启用边界：首次新增账号的启用时间等于创建时间，因此允许分析上游返回的、创建前最多 24 小时材料，并受模块全局起点限制；关闭后重新开启则从新的开关时刻起算，不回放关闭期间材料。四项订阅均关闭时账号退出共享采集调度。新开关独立于情绪和推介的金融 / Crypto 筛选。

只有 `odaily-hottopic.service` 调用 FXTwitter。投资线索不自行拉取时间线；模块只处理共享采集响应中符合账号启用时间的材料。原帖 ID 与规范化内容哈希共同确定材料版本；哈希由帖子 ID、作者、正文、链接和附件构成。相同版本可以有多个目录账号观察关系，但只创建一次扫描任务。不同的引用/转发/回复关系作为观察信息保留，不会把目录外作者自动加入订阅。

采集材料包括：

- 原创帖子正文、可用链接和附件替代文本。
- 引用帖的账号评论与引用原文，分别保留作者和关系。
- 纯转发的原作者正文、原帖时间和可用转发活动时间。
- 回复正文及响应中可用的父帖；缺少父帖时保留回复关系。

新帖进入投资线索材料表，不改变热点话题、市场情绪或项目推介的输入。超过其他 HotTopic 消费窗口的帖子仍可供投资线索使用；普通 HotTopic inbox 的短期清理不会删除仍被线索引用的材料。

## 阶段与 Prompt

模型路由沿用 `X_AGENT_MODEL` 和 `X_AGENT_FALLBACK_MODEL`，默认 `gpt-6-luna`，文本请求使用 `reasoning_effort=none`。模型请求和 Web Search 均在 SQLite 写事务之外执行。Worker 一次处理扫描、推导、补充和身份搜索各一个批次/任务；扫描与推导按启用账号轮转，轮转游标保存在 `investment_lead_scheduler_cursor`。

### 第一阶段：扫描

Prompt 版本为 `investment_leads_scan_v1`。扫描不按账号类型、金融关键词、ticker 或 Crypto 词表预过滤。正文按段落拆分，每段最多 4,800 字符；输入按最多 20 个文本段和默认 24,000 序列化字符打包。批次未满时最多等待 60 秒。模型为每段给出分类、主体、动作、变化对象、时间、影响关系、摘要和判定原因；同一材料版本的各段合并，任一段识别为驱动候选时保留为候选。

分类与后续处理：

| 分类 | 含义 | 任务状态与后续 |
| --- | --- | --- |
| `driver_candidate` | 有具体变化和初步影响方向 | 保存事件摘要并排入推导 |
| `composable` | 有具体变化但单条不足以形成逻辑 | 保存事件摘要；召回到相关材料时再推导 |
| `no_candidate` | 闲聊、静态介绍或重复表述 | 标为无候选，不进入推导 |
| `incomplete` | 关键信息可能只在附件或链接 | 标为待补充，执行一次有界补充读取并重新扫描 |

### 补充读取

补充任务最多读取三项材料，包含外链页面和图片替代文本。HTML 读取只接收 HTML、XHTML 或纯文本，单次超时 10 秒、响应体最多读取 256 KB、可见文本最多保留 4,000 字符；拒绝非公网 IP、凭据 URL、非标准端口和重定向。不会默认抓取全部外链、执行 OCR 或启动第二条 FXTwitter 请求通道。

外链读取失败、没有内容或未命中每日额度时，不得标为 `no_candidate`。已尝试的链接和成功摘要会保留；额度不足的链接进入 `budget_paused`，UTC 日期切换后继续。最多三项补充材料处理完后重新扫描；仍不完整时保留 `needs_material` 状态，不无限重复读取。只有重新扫描得出完整分类后材料完整性才更新为 `complete`。

### 关联与第二阶段

推导 Prompt 版本为 `investment_leads_derive_v1`。候选先从最近 30 天扫描事件中检索，最多筛看 100 条；严格同主体可使用近 30 天材料，其他材料限近 7 天。当前实现使用主体精确匹配和简单字词重合召回，再取最多 20 条不同材料交给模型，不是全量两两比较，也不保证找齐语义相似但措辞不同的跨期材料。仅有 `composable` 候选且没有相关材料时标为无候选。

推导模型最多输出 4 条线索，每条包括主体、1–3 句驱动逻辑、必要条件、标的及方向、原帖主张和系统推导的分别归属，以及支持材料 ID。引用/转发内容保留其原作者归属；引用不是目录账号自己的主张。标的可以留空，具体但没有代码的对象会单独排入身份搜索；不列泛化受益名单填充结果。

模型只可用候选及本次关联材料中出现的材料 ID。新线索使用 UUID；只有模型返回当前提供的已有线索 ID 才会修订对应线索。新增来源不刷新 `last_substantive_at`；驱动、条件、标的或主体变化才新增修订。当前已有线索上下文最多取近 30 天 30 条，因此极高线索量下模型不一定看到全部可归并项。

### 标的身份浅搜索

Prompt 版本为 `investment_leads_identity_search_v1`。当线索含具体资产名但缺 ticker 时，独立 `investment_lead_identity_searches` 队列可启用一次 GPT Responses `web_search` 查询。查询只解决公司身份、交易代码和市场；每项最多保留三个搜索摘要及实际返回的来源 URL。搜索结果不能反向改变驱动方向，歧义、私营公司或无法确认的代码必须留空。

身份任务状态为 `pending / processing / succeeded / unresolved / budget_paused / ignored / failed / expired`。找到代码时将代码和市场写入资产并生成线索修订，来源和搜索摘要保留在修订记录；未找到时状态为 `unresolved`，继续展示待定位资产。请求错误会按 15 分钟间隔自动重试，最多三次；可通过重试接口手动重新排队。搜索调用单独计入 `search` 用量，默认每日上限 50 次；输入 token 仍计入总输入 token 预算。

## 队列与状态

共享材料扫描/推导/补充使用 `investment_lead_tasks`，主键为材料 ID、内容哈希、阶段和 Prompt 版本。状态包括：

| 状态 | 含义 |
| --- | --- |
| `pending` | 等待处理或重试时间已到 |
| `processing` | 已领取并持有租约 |
| `succeeded` | 扫描/推导/补充阶段完成 |
| `no_candidate` | 扫描或组合推导确认没有可保留线索 |
| `needs_material` | 当前材料不足，等待或已完成一次补充后仍不足 |
| `budget_paused` | 日预算达到上限，等待 UTC 日期切换 |
| `ignored` | 已无任何启用账号观察，或账号资格在提交前关闭 |
| `failed` | 达到三次自动尝试上限，保留错误供人工重试 |
| `expired` | 任务超过 30 天保留期限 |

模型请求成功但响应未包含某材料的判定会标为失败，避免把缺失结果当成无候选。Worker 心跳及模型路由错误写入 `investment_leads_meta`；任务错误保存在各队列行。任务租约为 30 分钟；重启后过期租约回收，未耗尽次数的任务重新排队。失败任务可按阶段和数量有界重试，单次最多 500 项。

扫描领取查询必须读取任务的 `attempts`，领取时数据库计数和内存任务计数同时递增。2026-10-30 修复了查询漏取该字段导致 `KeyError`、领取事务回滚、全部任务停留在待扫描的问题。既有待扫描任务由修复后的 Worker 正常领取，无需清空或重建队列。

任务落库在写事务中再次检查观察资格。关闭一个账号仅在其他启用目录账号都没有观察同一材料/线索时取消未开始任务；运行中工作在保存扫描结果、线索或搜索身份前再次检查。共享材料后续被新的启用账号观察时，该材料及其线索下先前因无资格而忽略的扫描、推导、补充和身份搜索任务会重新排队。

## 预算、保留和服务

每日用量记在 `investment_lead_usage`，按 UTC 日期区分扫描、推导、补充读取和身份搜索。输入 token 预算统计扫描、推导及身份搜索的输入用量；请求前按材料或提示长度估算剩余额度，实际调用后再记录服务端用量。默认输入预算 500,000 tokens、推导模型请求 2,000 次、补充链接读取 100 次、身份搜索 50 次。达到上限后任务保留并显示预算暂停；预算恢复时继续处理。扫描每批最多 20 项，文本模型输出上限 5,000 tokens，模型/搜索超时默认 90 秒。

每小时维护时，30 天未处理任务标记为过期；30 天未再观察、且没有线索来源引用的材料和事件可清理。线索引用的正文摘录、来源 URL、账号观察关系和修订长期保留。采集起点、Worker 最近心跳、扫描/推导/补充/身份搜索积压、失败、预算暂停、材料不完整和各类用量通过控制台状态接口读取。

独立服务为 `odaily-investment-leads.service`，命令为 `backend/src/main.py x-agent-investment-leads-worker`。它只打开共享 HotTopic SQLite，不调用 FXTwitter。首次部署在 `/opt/OdAIly` 安装 `deploy/odaily-investment-leads.service` 到 `/etc/systemd/system/`，执行 `systemctl daemon-reload`，并启用服务；后续代码更新同步仓库后重启 `odaily-hottopic.service`、`odaily-editor-plugin-api.service` 和 `odaily-investment-leads.service`。纯前端更新由 GitHub 连接的 Vercel 流程发布。服务配置显式固定 `X_AGENT_MODEL` 与 `X_AGENT_FALLBACK_MODEL` 为 `gpt-6-luna`；默认 10 秒轮询。控制台管理员 API 为：

- `/console/x-agent/investment-leads`：列表、时间窗口、关键词、账号和待定位筛选，服务端分页。
- `/console/x-agent/investment-leads/detail`：原帖/推导、来源材料、补充读取、身份搜索、线索修订。
- `/console/x-agent/investment-leads/status`：订阅数、Worker、任务、失败、预算和用量。
- `/console/x-agent/investment-leads/retry-failed`：按阶段和数量重试失败任务。

全部接口复用现有控制台管理员 Bearer session。

## 环境变量

- `X_AGENT_OPENAI_BASE_URL` / `X_AGENT_OPENAI_API_KEY`：可选专用兼容 API 路由与凭据；为空时沿用现有 X Agent / ODAILY LLM 配置。
- `X_AGENT_MODEL` / `X_AGENT_FALLBACK_MODEL`：文本分析主模型和回退模型，默认 `gpt-6-luna`。
- `X_AGENT_INVESTMENT_LEADS_TIMEOUT_SECONDS`：单次模型或 Web Search 超时，默认 `90` 秒，范围 `5–300`。
- `X_AGENT_INVESTMENT_LEADS_MAX_OUTPUT_TOKENS`：单次文本模型输出上限，默认 `5000`。
- `X_AGENT_INVESTMENT_LEADS_BATCH_SIZE`：扫描批次材料数，默认 `20`，范围 `1–20`。
- `X_AGENT_INVESTMENT_LEADS_BATCH_WAIT_SECONDS`：低流量批次等待上限，默认 `60` 秒，范围 `0–300`。
- `X_AGENT_INVESTMENT_LEADS_MAX_BATCH_CHARS`：序列化请求字符预算，默认 `24000`，范围 `8000–300000`。
- `X_AGENT_INVESTMENT_LEADS_DAILY_INPUT_TOKENS`：每日输入 token 预算，默认 `500000`；设为 `0` 会暂停全部文本分析。
- `X_AGENT_INVESTMENT_LEADS_DAILY_DERIVE_CALLS`：每日推导请求预算，默认 `2000`。
- `X_AGENT_INVESTMENT_LEADS_DAILY_SUPPLEMENT_READS`：每日公网外链读取次数，默认 `100`。
- `X_AGENT_INVESTMENT_LEADS_DAILY_SEARCH_CALLS`：每日身份 Web Search 请求数，默认 `50`。
- `X_AGENT_INVESTMENT_LEADS_WORKER_INTERVAL_SECONDS`：Worker 轮询间隔，默认 `10` 秒，范围 `1–60`。
