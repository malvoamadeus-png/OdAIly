# 控制台-Meme速递

## 职责

`Meme速递` 是控制台一级只读页面，用于查看 OdAIly 内置 Meme worker 生成的候选、任务状态和最终文本。
它不提供阈值编辑、重跑、发布或任务状态修改能力。

## 上游链路

- `odaily-meme-scanner.service` 常驻的是轻量 SQLite 队列、Dexscreener 跟踪调度和单线程叙事 worker，不常驻 Chromium。`MEME_COMPLETED_SCAN_INTERVAL` 默认每 5 分钟到期一次；到期时才取得共享浏览器锁、用有头 Playwright Chromium 打开匿名 OKX MemePump 页面，自然生成短期 `ok-verify-*` 请求头后依次读取 BSC `chainId=56`、Robinhood `chainId=4663` 的 `memefun/meme-ranking/content?rankType=4` 迁移列表，并在成功、失败和异常路径中关闭 Chromium。OKX 只等共享锁 `MEME_OKX_BROWSER_LOCK_TIMEOUT_SECONDS`（默认 1 秒）；锁被 FOMO 占用、网页失败或市场请求失败都会记录本次尝试，常驻 worker 不会在 5 秒轮询中反复启动 Chromium，而是在下一个 5 分钟窗口再试。浏览器关闭并释放锁后，才用 Dexscreener 批量补齐本轮市值和 24 小时成交量。`xvfb-run` 仅提供虚拟显示，不保留浏览器。网页首次加载已经捕获目标链排名响应时直接复用；需要切链时统一点击选择器真正带事件的 reference value box，再从通用 `role=option` 弹窗项按链名选择。快捷链按钮只改变页面展示，在 Xvfb 环境不作为排名请求触发器；不能假设目标链图标初始可见或内层文字节点可直接触发弹窗。每条链当前最多返回 30 条，列表只承担最新发现窗口，不是完整的历史市值扫描源。市值和 24 小时成交量统一由无需 API key 的 Dexscreener 按链批量查询补齐；同链、精确 CA 且市值和 24 小时成交量完整的交易对中优先选 USD 流动性最高者，缺少完整交易对时才回退可用交易对。OKX 网页 `mcap`/`vol1h` 只作发现上下文。
- `odaily-meme-tg-watcher.service` 监听 Telegram 白名单社群中的真人 CA 消息。
- 普通代币首次进入 OKX `MIGRATED` 列表即建立 3 天跟踪窗口；BSC 市值达到 50 万美元、Robinhood 市值达到 100 万美元后进入播报候选，后续里程碑为 100 万/300 万美元（BSC）和 300 万美元（Robinhood）；首次发现已跨多档时只触发最高档。
- 仍在最近一次 OKX 列表中的代币直接使用本轮 Dexscreener 市场数据。滚出列表但仍在 3 天窗口内的代币以 Dexscreener 单币查询继续观察；市值低于本链第一门槛每 4 小时一次，达到门槛每 15 分钟一次。固定 CA hash 相位和单线程调度用于错峰请求。
- 控制台卡片的当前市值和 24 小时成交量在同一 `chain + CA` 有 active 跟踪记录时，读取该记录最新一次成功的 Dexscreener 观察值；任务 payload、触发档位、审计材料和已生成正文仍保持入队时的不可变快照。没有 active 跟踪记录时，卡片回退显示任务快照。
- 3 天从首次发现时间计算；到期后状态改为 `expired`，清理下一次调度时间，即使重新出现在 OKX 最新列表也不重新激活。scanner 启动时会按当前窗口收紧仍 active 的历史记录，并按当前市值策略重算其调度周期；迁移前的历史 `observations` 标记为 `legacy_untracked`，不会回填跟踪窗口；观察和里程碑按 `chain + address` 隔离。
- 社群热议要求 20 分钟内至少 5 次命中、至少 3 个不同真人发送者；不限制代币的 launchpad/platform。`0x...` EVM CA 先查询 Robinhood Chain，再以 BNB Chain（30 万美元）兜底；Solana Base58 CA 查询 Solana（50 万美元），其他链暂不触发。门槛按代币查询返回的链判断，不按来源社群名判断；launchpad/platform 只作为结果字段记录，不作为发现、入队、任务消费或播报门槛，未知平台值也继续进入叙事流程。
- 普通里程碑使用独立的市值最高水位；热议任务和发布结果刷新当前市值时，不推进普通里程碑水位，避免热议先触发后吞掉 50 万或 100 万档。
- `token_snapshots` 以 CA 为主键维度保存每次成功调度到的链、平台、symbol、市值、成交量、时间、来源（`completed`/`token_info`/`tg`）和原始 payload；`market_cap_milestones` 保存 CA+档位的首次观测时间、快照 ID 和任务状态。热议先发现的记录在后续 `completed` 扫描时仍会激活跟踪，并依据里程碑账本判定首次跨档。
- 两类任务均执行成交量门槛、叙事生成、重试和 OdAIly 挂后台写入逻辑。成交量门槛按 `24小时成交量 / 市值` 动态计算：市值不高于 30 万美元要求至少 50%，市值达到 300 万美元及以上要求至少 20%，中间区间按市值线性插值。Dexscreener 的所选交易对、流动性、市值和成交量字段写入原始快照；Meme 链路不调用 OKX 签名详情接口，也不执行 OKX 风险字段拦截。
- 挂后台接口成功后同步写入信息流插件本地 store，使用 `meme_digest` 类型进入高频区并显示“Meme挂后台”；信息流写入失败只记录日志，不回滚已经成功的挂后台结果。
- 叙事生成保留版本化快速材料契约，但 Telegram、FxTwitter 和 FOMO Thesis 都由 `odaily-official` 的本地 collector 并行收集；OdAIly 使用 `gpt-5.6-terra` 做材料分类与最终写作。Telegram/FxTwitter 不启动浏览器。FOMO 只在合格叙事任务中取得同一把浏览器锁、以独立运行时 profile 启动一次 Chromium、调用页面自有模块后立即关闭；它不能和 OKX 发现浏览器重叠。程序确定性将 `据Odaily Meme速递监测，` 放在正文最前面并添加固定免责声明。FOMO Thesis 采用时只能在正文写成“某信源表示”，不能泄露产品或作者身份。叙事审计复用 `jobs.narrative_json` 保存三路材料、调用诊断和最终判断；真正没有可用材料时任务才标记为 `no_usable_narrative`，网络、模型、JSON 或校验异常记录具体阶段并进入 `retry_wait`。

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
- 叙事审计的“性能与调用诊断”展示本地三路快速材料汇合耗时、各来源诊断和 Terra 最终写作耗时；来源并行，因此来源耗时之和可能大于汇合耗时。
- `GET /console/meme/detail?id=<job_id>` 按需返回单条任务的完整 `narrative_json`。旧库或旧任务没有该字段时返回 `available=false`，不影响列表。
- 数据库不存在、不可读或 schema 不兼容时，接口返回 `available=false` 和错误文本，不创建空库。

## 命令与服务

```text
python backend/src/main.py meme scan --once
python backend/src/main.py meme scan --send
python backend/src/main.py meme fomo-login
python backend/src/main.py meme tg-watch --check
python backend/src/main.py meme tg-watch
```

- `meme scan --once`：执行一次 OKX BSC/Robinhood 发现并最多处理一个任务，默认 dry-run。
- `meme scan --send`：常驻轮询并真实写入挂后台稿件，固定 `isPublish=false,isPush=false`。
- `meme fomo-login`：仅在收到 FOMO 登录失效提醒后执行的一次性维护命令，不属于 systemd 服务。服务器上以 `xvfb-run -a -s "-screen 0 1440x900x24" /opt/OdAIly/.venv/bin/python backend/src/main.py meme fomo-login` 启动；它最多保持 `MEME_FOMO_LOGIN_TIMEOUT_SECONDS`（默认 900 秒），DevTools 只监听 `127.0.0.1:MEME_FOMO_LOGIN_CDP_PORT`（默认 9224）。若该 loopback 端口已被占用，命令会失败，不会接管或误认已有端点。操作者从本机使用已配置的 `odaily-official` SSH 别名建立 `ssh -N -L 9224:127.0.0.1:9224 odaily-official` 本地转发后，在本机 Chrome 的 `chrome://inspect/#devices` 添加 `localhost:9224` 并附着目标；这提供 DevTools 控制，不是图形远程桌面。若登录流程需要普通可视化交互，先另行启用同样只经 SSH 暴露的一次性图形维护通道，不能把 DevTools 绑定到公网。完成登录后随即停止该一次性命令，不得复制、打印或提交 profile、Cookie 或 JWT。
- Meme 速递内部保存的正文仍为纯文本；仅在组装推送请求时，将每个非空行转换为 HTML 段落并转义 `&`、`<`、`>`，与通用 `PushClient` 使用同一规则。空行会被删除。
- `meme tg-watch --check`：校验 Telegram 登录和白名单可见性后退出。
- systemd unit：`deploy/odaily-meme-scanner.service`、`deploy/odaily-meme-tg-watcher.service`。

## 配置

- OKX：发现源固定为 `MEME_OKX_DISCOVERY_SOURCE=web_meme`，只通过匿名 MemePump 网页及其浏览器自然生成的动态请求头读取迁移列表；不支持官方签名 `tokenList` 回退。`MEME_OKX_WEB_TIMEOUT_SECONDS`、`MEME_OKX_WEB_SETTLE_MS`、`MEME_OKX_WEB_HEADLESS`、`MEME_OKX_WEB_PROXY` 控制网页运行，生产需要 Playwright Chromium 和 `xvfb-run`，默认有头模式。`MEME_BROWSER_LOCK_PATH` 由 OKX、FOMO 采集和一次性 FOMO 登录共用；FOMO 使用 `MEME_BROWSER_LOCK_TIMEOUT_SECONDS`（默认 90 秒），而定时 OKX 发现使用 `MEME_OKX_BROWSER_LOCK_TIMEOUT_SECONDS`（默认 1 秒）快速放弃。市场数据固定使用公开、无鉴权的 Dexscreener，`MEME_DEXSCREENER_TIMEOUT_SECONDS` 默认 `15`；BSC、Robinhood 和 Solana 都使用该适配器，不调用 GMGN 或 OKX 签名市场/详情接口。
- 跟踪调度：`MEME_COMPLETED_SCAN_INTERVAL` 默认 `300` 秒；`MEME_WORKER_POLL_INTERVAL` 默认 `5` 秒，只轮询队列和 Telegram 候选，不触发未到期的 OKX 浏览器发现。无论 OKX 浏览器锁冲突、网页发现失败还是本轮 Dexscreener 批量请求失败，均消耗本次正常发现尝试并等到下一窗口，防止故障时 Chromium 高频拉起。Dexscreener 的列表外调度继续按 `MEME_TOKEN_INFO_HIGH_INTERVAL`（默认 900 秒，即 15 分钟）、`MEME_TOKEN_INFO_LOW_INTERVAL`（默认 14400 秒，即 4 小时）、`MEME_TRACKING_WINDOW_SECONDS`（默认 259200 秒，即 3 天）和 `MEME_TOKEN_INFO_MIN_GAP_SECONDS`（默认 3 秒）独立运行。
- Telegram：`MEME_TELEGRAM_API_ID`、`MEME_TELEGRAM_API_HASH`、`MEME_TELEGRAM_WATCH_SESSION`。
- Telegram 白名单：`data/config/meme_whitelist.txt`，格式参考 `meme_whitelist.example.txt`。
- 屏蔽发送者：`data/config/meme_blocked_senders.txt`，格式参考 `meme_blocked_senders.example.txt`。
- CA 匹配：EVM 使用 `0x` 加 40 位十六进制；Solana 使用 32-44 位 Base58 公钥格式，并在候选中保存 `chain`。EVM 候选按 Robinhood、BSC 顺序以 Dexscreener 查询并采用首个市场指标完整的链结果；任务 payload、快照、观察、里程碑、叙事和标题沿用该链值，BSC/Robinhood 的发现来源仍是 OKX MemePump 网页。
- 叙事 Telegram：本地 collector 使用 `MEME_NARRATIVE_TELEGRAM_CONFIG`、`MEME_NARRATIVE_TELEGRAM_SESSION` 和 `MEME_NARRATIVE_TELEGRAM_ALLOWED_CHATS`；它与 watcher 使用不同 session，但使用同一白名单。FxTwitter 请求由 `MEME_NARRATIVE_X_TIMEOUT_SECONDS` 控制。
- FOMO：`MEME_FOMO_PROFILE_DIR` 是服务器本地、权限 `0700` 的独立运行时 profile，绝不纳入 Git 或审计 JSON；其中浏览器自身保留的认证状态只供后续按需采集使用，应用不读取、导出、打印或写入 Cookie/JWT。`MEME_FOMO_ENABLED`、`MEME_FOMO_NAVIGATION_TIMEOUT_SECONDS`、`MEME_FOMO_REQUEST_TIMEOUT_SECONDS`、`MEME_FOMO_MAX_PAGES` 和 `MEME_FOMO_PAGE_SIZE` 约束单次按需采集。没有 profile、跳转登录页或认证响应时，collector 标记 `login_required`；scanner 使用 `MEME_FOMO_LOGIN_ALERT_CHAT_ID`（回退 `TELEGRAM_CHAT_ID`）、`MEME_FOMO_LOGIN_ALERT_THREAD_ID`（回退全局 topic）、`MEME_FOMO_LOGIN_ALERT_COOLDOWN_SECONDS` 和 `MEME_FOMO_LOGIN_ALERT_TIMEOUT_SECONDS` 向值班人提醒。`MEME_FOMO_LOGIN_RETRY_SECONDS` 默认 300 秒；`MEME_FOMO_LOGIN_TIMEOUT_SECONDS` 和 `MEME_FOMO_LOGIN_CDP_PORT` 只控制一次性 `meme fomo-login` 维护窗口和其 loopback DevTools 端口。不会启动常驻 FOMO 服务或公开远程调试端口。
- 快速材料不再使用 `MEME_FAST_EVIDENCE_URL` 或 `MEME_FAST_EVIDENCE_INTERNAL_KEY`，不再请求 HideOnBush。`MEME_LOCAL_EVIDENCE_TIMEOUT` 控制本地采集的总配置上限。
- 最终写作：使用独立的 `MEME_FAST_WRITER_BASE_URL`、`MEME_FAST_WRITER_API_KEY` 和 `MEME_FAST_WRITER_MODEL`，模型默认固定为 `gpt-5.6-terra`，请求使用 `reasoning_effort=none`，GPT 客户端默认超时为 `90` 秒。Meme 叙事不调用 Grok、Grok X Search、Grok 实体补充或 GMGN。
- 未单独设置快速 writer 地址或密钥时才回退 OdAIly 的 `ODAILY_LLM_BASE_URL`、`ODAILY_LLM_API_KEY`；生产应显式配置支持 Terra 的独立 relay。
- 推送接口复用 `ODAILY_PUSH_ENDPOINT`，也可由 `MEME_ODAILY_PUSH_ENDPOINT` 单独覆盖。

## 状态与失败处理

- 最终叙事通过 `narrative_v2` 的生成阶段校验后，scanner 不再维护或执行另一套发布前正文禁词与角度正则；非空正文直接进入标题、正文组装和挂后台流程。
- `queued -> processing -> publishing -> publisher_pending`：正常挂后台路径。
- 临时叙事错误进入 `retry_wait`，最多 3 次，退避 60/300/900 秒；耗尽后 `discarded`。
- `volume_gate_failed`、`tg_market_cap_gate_failed`、`no_usable_narrative`、`queue_expired` 为明确不播报原因。
- 服务重启会把遗留 `processing/publishing` 恢复为可重试状态。
- OKX 网页发现成功后只更新最新发现窗口并释放 Chromium；网页页面失败会保留明确错误，不把空结果或浏览器异常当成“没有迁移代币”，也不会回退官方发现。Dexscreener 市场适配器成功后更新当前市值、24 小时成交量、最高水位和动态调度周期；发现阶段按链批量请求，列表外跟踪按单币请求。任一市场适配器失败只增加 `token_info_failures`、记录 `last_token_info_error` 并按原周期重试，不把失败当作零成交量或诈骗；调度积压写入 worker 日志。
- FOMO 的 `login_required` 不阻断已有 Telegram/FxTwitter 材料；若三路均无正文且 FOMO 登录缺失，任务进入 `retry_wait`，每 `MEME_FOMO_LOGIN_RETRY_SECONDS`（默认 300 秒）再试一次，不受通常 3 次临时重试上限限制，但仍在入队 1 小时后按 `queue_expired` 结束；同时通知在冷却窗口内只发一次。FOMO 其他浏览器或页面错误保留安全错误码，不导出会话或上游原始响应。
- `observations` 的跟踪字段包括代币 `chain`、`tracking_status`、3 天起止时间、最近 `completed` 时间、最近 `token_info` 时间、下一次调度时间、周期、来源、成交量和失败信息；服务重启后按 `next_token_info_at` 恢复，并使用记录的 `chain` 选择对应的 Dexscreener 查询。历史库新增该字段时优先从该代币最新 `token_snapshots.chain` 回填，找不到时按 BSC 兼容默认值处理。
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
- 页面不自动轮询，不触发 Dexscreener、Telegram、FOMO、叙事模型或发布接口。

## 叙事契约

内部审计保存 `status`、`failure_stage`、`failure_code`、`failure_message`、`material_counts`、`decision_code`、`decision_reason`、`fast_evidence`、`telegram_messages`、`x_posts`、`fomo_materials`、最终分类材料、使用/丢弃材料、`reader_text` 和性能诊断。命中频次、群组数量、用户情绪、税务或官网链接、机器人卡片和检索过程不能进入正文。详细规则见 控制台-Meme速递叙事规范.md。

## 白名单外发现

执行 meme tg-discover 可通过 Telegram 全局搜索 0x，列出当前账号可见且不在白名单中的群组/频道、真人、机器人和频道帖子命中数及代表性消息。命令只输出 JSON 和 Markdown 报告，不自动修改白名单。
