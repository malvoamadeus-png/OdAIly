# 控制台-Meme速递

## 职责

`Meme速递` 是控制台一级只读页面，用于查看 OdAIly 内置 Meme worker 生成的候选、任务状态和最终文本。
它不提供阈值编辑、重跑、发布或任务状态修改能力。

## 上游链路

- `odaily-meme-scanner.service` 每 1 分钟通过有头 Playwright Chromium 打开匿名 OKX MemePump 页面，由页面自然生成动态 `ok-verify-*` 请求头，再分别读取 BSC `chainId=56`、Robinhood `chainId=4663` 的 `memefun/meme-ranking/content?rankType=4` 迁移列表，不附加市值过滤；每条链当前最多返回 30 条，列表只承担最新发现窗口，不是完整的历史市值扫描源。网页 row 的 `mcap`/`vol1h` 只作发现上下文，列表代币再通过 `MEME_PRICE_SOURCE` 选择的市场价格适配器补齐市值和 24 小时成交量；OKX 模式批量调用官方签名 `price-info`，GMGN 模式逐个调用 `gmgn-cli token info`。
- `odaily-meme-tg-watcher.service` 监听 Telegram 白名单社群中的真人 CA 消息。
- 普通代币首次进入 OKX `MIGRATED` 列表即建立 3 天跟踪窗口；BSC 市值达到 50 万美元、Robinhood 市值达到 100 万美元后进入播报候选，后续里程碑为 100 万/300 万美元（BSC）和 300 万美元（Robinhood）；首次发现已跨多档时只触发最高档。
- 仍在最近一次 OKX 列表中的代币直接使用本轮列表和市场价格适配器数据，不调用单币详情。滚出列表但仍在 3 天窗口内的代币由持久化调度器按链调用 OKX `tokenDetails` 与选定的市场价格适配器：市值低于本链第一门槛每 4 小时一次，达到门槛每 15 分钟一次。固定 CA hash 相位、全局最小 3 秒间隔和单线程调度用于错峰限流；GMGN 发现补价阶段另用 `MEME_GMGN_PRICE_WORKERS` 控制并发。
- 3 天从首次发现时间计算；到期后状态改为 `expired`，清理下一次调度时间，即使重新出现在 OKX 最新列表也不重新激活。scanner 启动时会按当前窗口收紧仍 active 的历史记录，并按当前市值策略重算其调度周期；迁移前的历史 `observations` 标记为 `legacy_untracked`，不会回填跟踪窗口；观察和里程碑按 `chain + address` 隔离。
- 社群热议要求 20 分钟内至少 5 次命中、至少 3 个不同真人发送者；不限制代币的 launchpad/platform。`0x...` EVM CA 先查询 Robinhood Chain，再以 BNB Chain（30 万美元）兜底；Solana Base58 CA 查询 Solana（50 万美元），其他链暂不触发。门槛按代币查询返回的链判断，不按来源社群名判断；launchpad/platform 只作为结果字段记录，不作为发现、入队、任务消费或播报门槛，未知平台值也继续进入叙事流程。
- 普通里程碑使用独立的市值最高水位；热议任务和发布结果刷新当前市值时，不推进普通里程碑水位，避免热议先触发后吞掉 50 万或 100 万档。
- `token_snapshots` 以 CA 为主键维度保存每次成功调度到的链、平台、symbol、市值、成交量、时间、来源（`completed`/`token_info`/`tg`）和原始 payload；`market_cap_milestones` 保存 CA+档位的首次观测时间、快照 ID 和任务状态。热议先发现的记录在后续 `completed` 扫描时仍会激活跟踪，并依据里程碑账本判定首次跨档。
- 两类任务均执行成交量门槛、OKX 风险字段记录、叙事生成、重试和 OdAIly 挂后台写入逻辑。成交量门槛按 `24小时成交量 / 市值` 动态计算：市值不高于 30 万美元要求至少 50%，市值达到 300 万美元及以上要求至少 20%，中间区间按市值线性插值。OKX 的 Top10、Dev、Insiders、Bundlers、Snipers、疑似钓鱼钱包、流动性和社交字段写入原始快照；`MEME_OKX_RISK_MODE=shadow` 只记录风险标记，`block` 才会拦截疑似钓鱼钱包比例不低于 5%或 Top10 持仓不低于 80%的普通里程碑任务。
- 挂后台接口成功后同步写入信息流插件本地 store，使用 `meme_digest` 类型进入高频区并显示“Meme挂后台”；信息流写入失败只记录日志，不回滚已经成功的挂后台结果。
- 叙事生成使用版本化快速材料契约：HideOnBush 以链加精确 CA 并行收集 Telegram、FxTwitter、FOMO Thesis，只返回规范化材料和诊断；OdAIly 使用 `gpt-5.6-luna` 做材料分类与最终写作。程序确定性将 `据Odaily Meme速递监测，` 放在正文最前面并添加固定免责声明。FOMO Thesis 采用时只能在正文写成“某信源表示”，不能泄露产品或作者身份。叙事审计复用 `jobs.narrative_json` 保存三路材料、调用诊断和最终判断；真正没有可用材料时任务才标记为 `no_usable_narrative`，网络、模型、JSON 或校验异常记录具体阶段并进入 `retry_wait`。

## 文本口径

普通新币：

```text
Meme速递：{chain}上{symbol}市值突破{market_cap}万美元
```

社群热议：

```text
Meme速递：{chain}上{symbol}社群热议中，市值{market_cap}万美元
```

标题和正文不展示“发射 X 分钟/小时”，也不展示“在社区短时多次出现”等扫描过程语言。

## 数据接口

- 接口：`GET /console/meme/get`。
- 鉴权：复用控制台本地操作者 Bearer session。
- 数据源：OdAIly 的 `data/processed/meme_scanner.sqlite3`，接口以 SQLite `mode=ro` 打开。
- 默认生产路径：`/opt/OdAIly/data/processed/meme_scanner.sqlite3`。
- 覆盖变量：`MEME_SCANNER_DB_PATH`。
- 返回最近 100 条 `jobs`，并为 `tg_burst` 关联 `tg_candidates` 的命中数、群数和发送者数。
- 前端列表隐藏明确未通过门槛的任务：`volume_gate_failed`、`tg_market_cap_gate_failed`、`unsupported_chain`、`token_not_found`；这些任务仍保留在 SQLite 中用于审计。
- 列表响应仅增加叙事摘要：`narrative_available`、`narrative_status`、`failure_stage`、`failure_code`、`primary_type`、`type_hypothesis`；不把 Telegram 上下文塞入列表。
- 任务详情的 `timing` 还原生命周期耗时：排队（`queued_at -> processing_started_at`）、叙事（`narrative.performance.total_duration_ms`）、发布写入（`publishing_started_at -> completed_at`）和总耗时（`queued_at -> completed_at`）。新库由 `jobs.processing_started_at`、`publishing_started_at`、`completed_at` 记录，旧任务缺字段时返回 `null`。
- 叙事审计的“性能与调用诊断”展示 HideOnBush 三路快速材料汇合耗时、各来源诊断和 Luna 最终写作耗时；HideOnBush 内部来源并行，因此来源耗时之和可能大于汇合耗时。
- `GET /console/meme/detail?id=<job_id>` 按需返回单条任务的完整 `narrative_json`。旧库或旧任务没有该字段时返回 `available=false`，不影响列表。
- 数据库不存在、不可读或 schema 不兼容时，接口返回 `available=false` 和错误文本，不创建空库。

## 命令与服务

```text
python backend/src/main.py meme scan --once
python backend/src/main.py meme scan --send
python backend/src/main.py meme tg-watch --check
python backend/src/main.py meme tg-watch
```

- `meme scan --once`：执行一次 OKX BSC/Robinhood 发现并最多处理一个任务，默认 dry-run。
- `meme scan --send`：常驻轮询并真实写入挂后台稿件，固定 `isPublish=false,isPush=false`。
- Meme 速递内部保存的正文仍为纯文本；仅在组装推送请求时，将每个非空行转换为 HTML 段落并转义 `&`、`<`、`>`，与通用 `PushClient` 使用同一规则。空行会被删除。
- `meme tg-watch --check`：校验 Telegram 登录和白名单可见性后退出。
- systemd unit：`deploy/odaily-meme-scanner.service`、`deploy/odaily-meme-tg-watcher.service`。

## 配置

- OKX：`OKX_API_KEY`、`OKX_SECRET_KEY`、`OKX_PASSPHRASE` 用于 `tokenDetails` 和 OKX 风险字段；`MEME_OKX_DISCOVERY_SOURCE` 默认 `web_meme`，可设为 `official` 切回签名 `tokenList`；`MEME_OKX_DISCOVERY_FALLBACK=official` 才允许网页发现失败时回退官方列表，默认不回退以避免静默消耗额度。`MEME_OKX_WEB_TIMEOUT_SECONDS`、`MEME_OKX_WEB_SETTLE_MS`、`MEME_OKX_WEB_HEADLESS`、`MEME_OKX_WEB_PROXY` 控制网页运行，生产需要 Playwright Chromium 和 `xvfb-run`，默认有头模式。`MEME_OKX_TIMEOUT_SECONDS`、`MEME_OKX_MAX_ATTEMPTS` 控制仍保留的官方 REST 请求超时和重试，`MEME_OKX_RISK_MODE` 默认为 `shadow`。`MEME_PRICE_SOURCE` 控制 BSC/Robinhood 的市值、价格和 24 小时成交量，默认 `okx`；临时设为 `gmgn` 时使用 `gmgn-cli token info`，市值按 `price.price × circulating_supply` 计算，成交量取 `price.volume_24h`，不再调用 OKX `price-info`。网页发现仍来自 OKX MemePump，单币 `tokenDetails` 和风险字段仍可来自 OKX；`MEME_GMGN_PRICE_WORKERS` 控制发现阶段并发查询数，默认 4，`MEME_GMGN_REQUEST_INTERVAL_SECONDS` 控制所有 GMGN 价格请求启动间隔，默认 0.15 秒，GMGN CLI 就绪检查在 scanner 进程内只执行一次；识别到 GMGN 限流封禁后，冷却窗口内不再发送请求。GMGN 价格适配器要求服务器已安装 `gmgn-cli` 并通过 `gmgn-cli config --check`；Solana 兼容路径仍可使用 GMGN。
- 跟踪调度：`MEME_COMPLETED_SCAN_INTERVAL`（默认 60 秒）、`MEME_TOKEN_INFO_HIGH_INTERVAL`（默认 900 秒，即 15 分钟）、`MEME_TOKEN_INFO_LOW_INTERVAL`（默认 14400 秒，即 4 小时）、`MEME_TRACKING_WINDOW_SECONDS`（默认 259200 秒，即 3 天）、`MEME_TOKEN_INFO_MIN_GAP_SECONDS`（默认 3 秒）。
- Telegram：`MEME_TELEGRAM_API_ID`、`MEME_TELEGRAM_API_HASH`、`MEME_TELEGRAM_WATCH_SESSION`。
- Telegram 白名单：`data/config/meme_whitelist.txt`，格式参考 `meme_whitelist.example.txt`。
- 屏蔽发送者：`data/config/meme_blocked_senders.txt`，格式参考 `meme_blocked_senders.example.txt`。
- CA 匹配：EVM 使用 `0x` 加 40 位十六进制；Solana 使用 32-44 位 Base58 公钥格式，并在候选中保存 `chain`。EVM 候选按 Robinhood、BSC 顺序查询并采用首个有效链结果；任务 payload、快照、观察、里程碑、叙事和标题沿用该链值，BSC/Robinhood 市场字段来源由 `MEME_PRICE_SOURCE` 决定，发现来源仍是 OKX MemePump 网页。
- 叙事 Telegram 会话、白名单与 FOMO 浏览器会话归 HideOnBush 管理；OdAIly 不再读取这些运行资产。
- 快速叙事材料：OdAIly 通过 `MEME_FAST_EVIDENCE_URL` 调用 HideOnBush 内部材料接口，请求显式携带链、CA 和 symbol，并使用 `MEME_FAST_EVIDENCE_INTERNAL_KEY` 鉴权。HideOnBush 并行收集 FxTwitter、Telegram、FOMO Thesis，只返回材料和诊断；超时由 `MEME_FAST_EVIDENCE_TIMEOUT` 控制。
- 最终写作：使用独立的 `MEME_FAST_WRITER_BASE_URL`、`MEME_FAST_WRITER_API_KEY` 和 `MEME_FAST_WRITER_MODEL`，模型默认固定为 `gpt-5.6-luna`，请求使用 `reasoning_effort=none`。Meme 叙事不再调用 Grok、Grok X Search、Grok 实体补充或 GMGN 叙事。`MEME_PRICE_SOURCE=gmgn` 的市场价格适配器是独立链路，仍可继续使用。
- 未单独设置快速 writer 地址或密钥时才回退 OdAIly 的 `ODAILY_LLM_BASE_URL`、`ODAILY_LLM_API_KEY`；生产应显式配置支持 Luna 的独立 relay。
- 推送接口复用 `ODAILY_PUSH_ENDPOINT`，也可由 `MEME_ODAILY_PUSH_ENDPOINT` 单独覆盖。

## 状态与失败处理

- 最终叙事通过 `narrative_v2` 的生成阶段校验后，scanner 不再维护或执行另一套发布前正文禁词与角度正则；非空正文直接进入标题、正文组装和挂后台流程。
- `queued -> processing -> publishing -> publisher_pending`：正常挂后台路径。
- 临时叙事错误进入 `retry_wait`，最多 3 次，退避 60/300/900 秒；耗尽后 `discarded`。
- `volume_gate_failed`、`tg_market_cap_gate_failed`、`no_usable_narrative`、`queue_expired` 为明确不播报原因。
- 服务重启会把遗留 `processing/publishing` 恢复为可重试状态。
- OKX 网页发现成功后只更新最新发现窗口；网页页面失败会保留明确错误，不把空结果或浏览器异常当成“没有迁移代币”，也不会默认静默回退官方发现。市场价格适配器成功后更新当前市值、24 小时成交量、最高水位和动态调度周期；`MEME_PRICE_SOURCE=okx` 时使用官方 `price-info`，`MEME_PRICE_SOURCE=gmgn` 时使用 GMGN `token info` 并按代币逐个查询。单币 `tokenDetails` 仍只负责可选的身份/风险原始字段。任一市场适配器失败只增加 `token_info_failures`、记录 `last_token_info_error` 并按原周期重试，不把失败当作零成交量或诈骗；调度积压写入 worker 日志。
- `observations` 的跟踪字段包括代币 `chain`、`tracking_status`、3 天起止时间、最近 `completed` 时间、最近 `token_info` 时间、下一次调度时间、周期、来源、成交量和失败信息；服务重启后按 `next_token_info_at` 恢复，并使用记录的 `chain` 选择对应的 GMGN `token_info` 链路。历史库新增该字段时优先从该代币最新 `token_snapshots.chain` 回填，找不到时按 BSC 兼容默认值处理。
- `observations` 是当前跟踪状态，不是完整历史；溯源和首次档位判断以 `token_snapshots`、`market_cap_milestones` 为准。
- TG 消息按 `CA + chat_id + message_id` 和转发源双重去重，候选 6 小时冷却，原始提及默认保留 90 天。

## 页面字段

- 链、平台、CA、名称、symbol。
- 当前市值、24 小时成交量和触发档位。
- 普通新币或社群热议触发类型。
- 社群热议的命中数、不同发送者数和群数。
- 任务状态、未播报原因、排队和更新时间。
- 已生成标题和正文；尚未生成时展示当前状态或失败原因。
- 叙事审计入口默认收起，点击后懒加载详情；顶层分组同一时间只展开一个：失败诊断/运行状态、最终判断、Telegram 消息、快速叙事信源、性能与调用诊断。
- Telegram 分组再分为最老 20 条和最新 20 条命中，每条命中单独展开查看命中消息、发送者、群组、时间及前 2/后 15 条上下文。
- 最终判断显示最终类型、source/angle/supplement、使用/丢弃材料、`decision_code`、`decision_reason` 和 `reader_text`；为空时显示“未判断/未形成”和确定性原因。

## 读取策略

- 首次进入页面时读取一次。
- 顶部刷新按钮只刷新本页。
- 页面不自动轮询，不触发 GMGN、Telegram、叙事模型或发布接口。

## 叙事契约

内部审计保存 `status`、`failure_stage`、`failure_code`、`failure_message`、`material_counts`、`decision_code`、`decision_reason`、`fast_evidence`、`telegram_messages`、`x_posts`、`fomo_materials`、最终分类材料、使用/丢弃材料、`reader_text` 和性能诊断。命中频次、群组数量、用户情绪、税务或官网链接、机器人卡片和检索过程不能进入正文。详细规则见 控制台-Meme速递叙事规范.md。

## 白名单外发现

执行 meme tg-discover 可通过 Telegram 全局搜索 0x，列出当前账号可见且不在白名单中的群组/频道、真人、机器人和频道帖子命中数及代表性消息。命令只输出 JSON 和 Markdown 报告，不自动修改白名单。
