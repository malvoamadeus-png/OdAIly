# 控制台-Meme速递

## 职责

`Meme速递` 是控制台一级只读页面，用于查看 OdAIly 内置 Meme worker 生成的候选、任务状态和最终文本。页面有“Meme速递 / 今日热门Meme”两个二级标签。
它不提供阈值编辑、重跑、发布或任务状态修改能力。

## 上游链路

- `odaily-meme-scanner.service` 常驻的是轻量 SQLite 队列、Dexscreener 跟踪调度和单线程叙事 worker，不常驻 Chromium。`MEME_COMPLETED_SCAN_INTERVAL` 默认每 5 分钟到期一次；到期时才取得共享浏览器锁、用有头 Playwright Chromium 打开匿名 OKX MemePump 页面，由页面运行时生成短期动态请求头后依次读取 BSC `chainId=56`、Robinhood `chainId=4663` 和 Solana `chainId=501` 的 `memefun/meme-ranking/content/batch` 迁移列表，并在成功、失败和异常路径中关闭 Chromium。排名请求为 POST；未从页面初始请求取得目标链时，由页面同源 `fetch` 发起请求，不向 Python 导出或保存动态请求头。Solana 对 OKX launchpad `protocolId` 120596（Pump.fun）和 141620（Stonkfun）分别查询，再交错合并，避免平台之一占满每链最多 30 条的列表；结果按 Pump.fun / Stonkfun 平台信息筛选，只有 OKX 未提供平台身份时，CA 以 `pump` 结尾才作为 Pump.fun 辅助识别。OKX 只等共享浏览器锁 `MEME_OKX_BROWSER_LOCK_TIMEOUT_SECONDS`（默认 1 秒）；锁被 FOMO 占用、网页失败或市场请求失败都会记录本次尝试，常驻 worker 不会在 5 秒轮询中反复启动 Chromium，而是在下一个 5 分钟窗口再试。浏览器关闭并释放锁后，才用 Dexscreener 批量补齐本轮市值和 24 小时成交量。`xvfb-run` 仅提供虚拟显示，不保留浏览器。列表只承担最新发现窗口，不是完整的历史市值扫描源。市值和 24 小时成交量统一由无需 API key 的 Dexscreener 按链批量查询补齐；同链、精确 CA 且市值和 24 小时成交量完整的交易对中优先选 USD 流动性最高者，缺少完整交易对时才回退可用交易对。OKX 网页 `mcap`/`vol1h` 只作发现上下文。
- `odaily-meme-tg-watcher.service` 监听 Telegram 白名单社群中的真人 CA 消息。
- 普通代币首次进入 OKX `MIGRATED` 列表、或 Telegram 社群热议候选通过链/市值/成交量门槛并生成热议任务时，均立即建立 3 天市值跟踪窗口；不要求热议代币出现在 OKX 列表。BSC、Robinhood、Solana Pump.fun 和 Stonkfun 分别按 50 万、100 万、100 万、100 万美元首档开始观察；Pump.fun 与 Stonkfun 后续共用同一档位及跟踪规则。每次市值上破新档都刷新 3 天期限；首次发现或单次行情跨越多个档位时只处理最高档。
- 仍在最近一次 OKX 列表中的代币直接使用本轮 Dexscreener 市场数据。滚出列表但仍在 3 天窗口内的代币以 Dexscreener 单币查询继续观察；市值低于本链第一门槛每 4 小时一次，达到门槛每 15 分钟一次。固定 CA hash 相位和单线程调度用于错峰请求。
- 控制台卡片的当前市值和 24 小时成交量在同一 `chain + CA` 有 active 跟踪记录时，读取该记录最新一次成功的 Dexscreener 观察值；任务 payload、触发档位、审计材料和已生成正文仍保持入队时的不可变快照。没有 active 跟踪记录时，卡片回退显示任务快照。
- 3 天期限从首次发现或最近一次上破新档的时间计算；连续 3 天没有上破新档后状态改为 `expired`，清理下一次调度时间，即使重新出现在 OKX 最新列表也不重新激活。scanner 启动时会按持久化窗口锚点收紧仍 active 的历史记录，并按当前市值策略重算其调度周期；迁移前的历史 `observations` 标记为 `legacy_untracked`，不会回填跟踪窗口；观察和里程碑按 `chain + address` 隔离。
- 社群热议要求 20 分钟内至少 5 次命中、至少 3 个不同真人发送者；不限制代币的 launchpad/platform。`0x...` EVM CA 先查询 Robinhood Chain，再以 BNB Chain（30 万美元）兜底；Solana Base58 CA 查询 Solana（50 万美元），其他链暂不触发。门槛按代币查询返回的链判断，不按来源社群名判断；launchpad/platform 只作为结果字段记录，不作为发现、入队、任务消费或播报门槛，未知平台值也继续进入叙事流程。
- 普通里程碑使用独立的市值最高水位；热议入选时以当时市值建立跟踪基线，不为已经生成热议稿的当前水位重复生成普通里程碑稿。后续跟踪观察上破新档时，仍按普通里程碑成交量资格和幂等规则生成任务。
- `token_snapshots` 以 CA 为主键维度保存每次成功调度到的链、平台、symbol、市值、成交量、时间、来源（`completed`/`token_info`/`tg`）和原始 payload；`market_cap_milestones` 保存 CA+档位的首次观测时间、快照 ID 和任务状态。热议成功入队时立即写 active 跟踪状态及查询计划；后续 OKX `completed` 扫描复用该观察和里程碑账本，不重新启动窗口。
- 两类任务均执行成交量门槛、叙事生成、重试和 OdAIly 挂后台写入逻辑。成交量门槛按 `24小时成交量 / 市值` 动态计算：市值不高于 30 万美元要求至少 50%，市值达到 300 万美元及以上要求至少 20%，中间区间按市值线性插值。Dexscreener 的所选交易对、流动性、市值和成交量字段写入原始快照；Meme 链路不调用 OKX 签名详情接口，也不执行 OKX 风险字段拦截。
- 挂后台接口成功后同步写入信息流插件本地 store，使用 `meme_digest` 类型进入高频区并显示“Meme挂后台”；信息流写入失败只记录日志，不回滚已经成功的挂后台结果。
- 叙事生成保留版本化快速材料契约，但 Telegram、FxTwitter 和 FOMO Thesis 都由 `odaily-official` 的本地 collector 并行收集；两个栏目额外请求Grok Bot Routine，非阻塞等待回调最多15分钟；失败或超时仍用其他材料写稿。OdAIly使用 `gpt-6-luna` 在同一次调用内判断 `pure_meme/celebrity_anchor/app_linked`，再按对应入口写作；Grok材料单独以 `Grok补充资料显示，` 开头，允许重复炒作角度。Telegram/FxTwitter 不启动浏览器。FOMO 只在合格叙事任务中取得同一把浏览器锁、以独立运行时 profile 启动一次有头 Chromium、调用页面自有模块后立即关闭；它不能和 OKX 发现浏览器重叠。FOMO 当前只会在有头 Chromium 中初始化 Privy runtime，`odaily-meme-scanner.service` 已提供 Xvfb；不要把 `MEME_FOMO_HEADLESS` 设为 `true`，否则页面只渲染外壳且材料请求会失败。程序确定性将 `据Odaily Meme速递监测，` 放在正文最前面并添加固定免责声明。FOMO Thesis 采用时只能在正文写成“某信源表示”，不能泄露产品或作者身份。叙事审计复用 `jobs.narrative_json` 保存三路材料、调用诊断和最终判断；真正没有可用材料时任务才标记为 `no_usable_narrative`，网络、模型、JSON 或校验异常记录具体阶段并进入 `retry_wait`。

## 今日热门Meme

`daily_hot.py` 从HideOnBush迁移GMGN CLI的榜单查询和 `data.rank` 解析；独立发现线程每小时扫描Solana、BSC、Base、Ethereum、Robinhood，各取1小时涨幅榜、6小时市值榜、24小时市值榜前20名。1小时要求涨幅至少20%，6小时至少30%，24小时无涨幅要求。市值由Dexscreener提供，三类均要求至少100万美元并通过现有 `volume_ratio_gate`。不限制发行平台，不增加控盘/刷量或安全筛选，不截断全链候选数。

每链全部榜单及行情批次首次成功后，原子建立基线而不发稿；部分失败不完成该链初始化。按链＋CA永久记录首次满足全部条件的代币，成交量不足不消耗资格；首次入选后离榜回榜不再触发。Solana地址保留大小写。GMGN负责排名和对应时段涨幅，Dexscreener按完整行情字段和最高USD流动性选择交易对。

`daily_hot_chains`保存初始化，`daily_hot_scans`保存最近7天扫描结果，`daily_hot_seen`永久保存入选身份。`jobs.trigger_kind=daily_hot`复用任务及挂后台链路，不写入Meme速递观察/里程碑账本。两个栏目独立去重。首次达标后没有可用叙事也不重新产生候选，普通暂时错误沿用队列重试。

Grok请求用 `meme_grok_jobs` 按任务trigger_key保存；回调校验与接收复用API服务。等待期间任务为 `retry_wait/reason=grok_pending`，不消耗普通重试次数，不阻塞候选扫描或其他写作。提交前先持久化请求，重启不重复投递不确定的请求。明确失败/超时后以其他材料降级，不补改已写稿。

运行配置：`GROK_ROUTINE_CREDENTIALS_FILE`默认 `data/config/grok.txt`，私有文件权限600；Webhook URL优先 `GROK_ROUTINE_WEBHOOK_URL`，其次凭证文件url；`GROK_CALLBACK_BASE_URL`默认既有公网回调入口；`MEME_HOT_GMGN_REQUEST_INTERVAL`默认35秒。GMGN沿用CLI私有认证。扫描线程与现有scanner同服务运行；`meme scan --once`也执行到期的热门扫描。上游失败记录扫描诊断，下一小时重试。

## 文本口径

普通新币：

```text
Meme速递：{chain}上{symbol}市值突破{market_cap}万美元
```

社群热议：

```text
Meme速递：{chain}上{symbol}社群热议中，市值{market_cap}万美元
```

今日热门Meme：`今日热门Meme：{chain}上{symbol}市值突破{market_cap}万美元`；导语和结尾使用同一栏目名称，详情见共用写作框架。

标题和正文不展示“发射 X 分钟/小时”，也不展示“在社区短时多次出现”等扫描过程语言。

## 数据接口

- 接口：`GET /console/meme/get?column=digest|daily_hot|all`。省略column保留旧接口全部记录行为，前端两个标签分别传digest/daily_hot；服务端先筛选栏目再限制条数。
- 鉴权：复用控制台本地操作者 Bearer session。
- 数据源：OdAIly 的 `data/processed/meme_scanner.sqlite3`，接口以 SQLite `mode=ro` 打开。
- 默认生产路径：`/opt/OdAIly/data/processed/meme_scanner.sqlite3`。
- 覆盖变量：`MEME_SCANNER_DB_PATH`。
- 返回所选栏目最近 100 条 `jobs`，并为 `tg_burst` 关联 `tg_candidates` 的命中数、群数和发送者数。
- 前端列表隐藏明确未通过门槛的任务：`volume_gate_failed`、`tg_market_cap_gate_failed`、`unsupported_chain`、`token_not_found`；这些任务仍保留在 SQLite 中用于审计。
- 列表响应仅增加叙事摘要：`narrative_available`、`narrative_status`、`failure_stage`、`failure_code`、`primary_type`、`type_hypothesis`；不把 Telegram 上下文塞入列表。
- 任务详情的 `timing` 还原生命周期耗时：排队（`queued_at -> processing_started_at`）、叙事（`narrative.performance.total_duration_ms`）、发布写入（`publishing_started_at -> completed_at`）和总耗时（`queued_at -> completed_at`）。新库由 `jobs.processing_started_at`、`publishing_started_at`、`completed_at` 记录，旧任务缺字段时返回 `null`。
- 叙事审计的“性能与调用诊断”展示本地三路快速材料汇合耗时、各来源诊断和 Luna 最终写作耗时；来源并行，因此来源耗时之和可能大于汇合耗时。
- `GET /console/meme/detail?id=<job_id>` 按需返回单条任务的完整 `narrative_json`。旧库或旧任务没有该字段时返回 `available=false`，不影响列表。
- 数据库不存在、不可读或 schema 不兼容时，接口返回 `available=false` 和错误文本，不创建空库。

## 命令与服务

```text
python backend/src/main.py meme scan --once
python backend/src/main.py meme scan --send
python backend/src/main.py meme fomo-login
python backend/src/main.py meme bloomberg-read <Bloomberg文章URL> [--output <服务器上的txt路径>]
python backend/src/main.py meme tg-watch --check
python backend/src/main.py meme tg-watch
```

- `meme scan --once`：执行一次 OKX BSC、Robinhood、Solana Pump.fun/Stonkfun 发现并最多处理一个任务，默认 dry-run。
- `meme scan --send`：常驻轮询并真实写入挂后台稿件，固定 `isPublish=false,isPush=false`。
- `meme fomo-login`：仅在收到 FOMO 登录失效提醒后执行的一次性维护命令，不属于 systemd 服务。服务器上以 `xvfb-run -a -s "-screen 0 1440x900x24" /opt/OdAIly/.venv/bin/python backend/src/main.py meme fomo-login` 启动；它最多保持 `MEME_FOMO_LOGIN_TIMEOUT_SECONDS`（默认 900 秒），DevTools 只监听 `127.0.0.1:MEME_FOMO_LOGIN_CDP_PORT`（默认 9224）。若该 loopback 端口已被占用，命令会失败，不会接管或误认已有端点。命令会尝试识别并点击唯一的 FOMO Login、Google 登录和 Google 已登录账号选项；不能唯一识别时停止自动点击，操作者可从本机使用已配置的 `odaily-official` SSH 别名建立 `ssh -N -L 9224:127.0.0.1:9224 odaily-official` 本地转发后，在本机 Chrome 的 `chrome://inspect/#devices` 添加 `localhost:9224` 并附着目标，人工完成验证。DevTools 提供控制，不是图形远程桌面；验证码、新账号登录和 Google 风控仍需人工处理。完成登录后随即停止该一次性命令，不得复制、打印或提交 profile、Cookie 或 JWT。
- `meme bloomberg-read <URL>`：仅处理一篇 `bloomberg.com/news/...` 文章，复用 `MEME_FOMO_PROFILE_DIR` 中已有的 Meme FOMO 持久登录 profile 和共享浏览器锁，不新建登录 profile，也不复制 Cookie。命令会在服务器按次启动并关闭 Chromium，沿用 profile 中已有的 Google 登录态；DevTools 只监听服务器 loopback。命令不要求本地 Chrome 或本机端口转发；人工登录需要能操作该服务器浏览器的图形会话。完成 Bloomberg 登录并回到目标文章后，在命令终端按 Enter；程序只读取可见的 `ArticleBodyText_` 段落，保持段落顺序并保存为纯文本，默认路径为 `data/exports/bloomberg/<文章slug>-<读取时间>.txt`，也可用 `--output` 指定服务器路径。终端只返回标题、作者、发布时间、栏目、规范链接、读取时间、状态和文件路径，不打印正文；不读取隐藏的 `__NEXT_DATA__`，不输出或保存 Cookie/JWT/HTML，正文完整性标记为 `unverified`。没有可见段落时返回 `article_body_not_visible` 且不创建正文文件；不会尝试移除访问限制。该命令是人工单篇研究工具，不接入自动 worker、SQLite 或审计材料。
- Meme 速递内部保存的正文仍为纯文本；仅在组装推送请求时，将每个非空行转换为 HTML 段落并转义 `&`、`<`、`>`，与通用 `PushClient` 使用同一规则。空行会被删除。
- 登录辅助的页面检查只允许 `fomo.family` 和 `accounts.google.com`，每个自动步骤在维护窗口内最多点击一次，避免重复打开 OAuth。2026-10-13 在官方服务器实测未登录临时窗口：Login 和 Continue with Google 均为唯一按钮，点击后进入 Google 登录页面；已有生产 profile 的 CA 材料查询成功。临时窗口未保存 Google 会话，因此本次未验证账号选择器及完整 OAuth 回跳；不能把前两步可点击或已登录 profile 可查询当作自动重新登录成功。此辅助只属于一次性维护命令，collector 认证失效时仍按原有重试与告警逻辑处理。
- `meme tg-watch --check`：校验 Telegram 登录和白名单可见性后退出。
- systemd unit：`deploy/odaily-meme-scanner.service`、`deploy/odaily-meme-tg-watcher.service`。

## 配置

- OKX：发现源固定为 `MEME_OKX_DISCOVERY_SOURCE=web_meme`，只通过匿名 MemePump 网页及其浏览器自然生成的动态请求头读取迁移列表；不支持官方签名 `tokenList` 回退。`MEME_OKX_WEB_TIMEOUT_SECONDS`、`MEME_OKX_WEB_SETTLE_MS`、`MEME_OKX_WEB_HEADLESS`、`MEME_OKX_WEB_PROXY` 控制网页运行，生产需要 Playwright Chromium 和 `xvfb-run`，默认有头模式。`MEME_BROWSER_LOCK_PATH` 由 OKX、FOMO 采集、一次性 FOMO 登录和单篇 Bloomberg 读取共用；FOMO 使用 `MEME_BROWSER_LOCK_TIMEOUT_SECONDS`（默认 90 秒），而定时 OKX 发现使用 `MEME_OKX_BROWSER_LOCK_TIMEOUT_SECONDS`（默认 1 秒）快速放弃。市场数据固定使用公开、无鉴权的 Dexscreener，`MEME_DEXSCREENER_TIMEOUT_SECONDS` 默认 `15`；BSC、Robinhood 和 Solana 都使用该适配器，不调用 GMGN 或 OKX 签名市场/详情接口。
- 跟踪调度：`MEME_COMPLETED_SCAN_INTERVAL` 默认 `300` 秒；`MEME_WORKER_POLL_INTERVAL` 默认 `5` 秒，只轮询队列和 Telegram 候选，不触发未到期的 OKX 浏览器发现。无论 OKX 浏览器锁冲突、网页发现失败还是本轮 Dexscreener 批量请求失败，均消耗本次正常发现尝试并等到下一窗口，防止故障时 Chromium 高频拉起。Dexscreener 的列表外调度继续按 `MEME_TOKEN_INFO_HIGH_INTERVAL`（默认 900 秒，即 15 分钟）、`MEME_TOKEN_INFO_LOW_INTERVAL`（默认 14400 秒，即 4 小时）、`MEME_TRACKING_WINDOW_SECONDS`（默认 259200 秒，即 3 天）和 `MEME_TOKEN_INFO_MIN_GAP_SECONDS`（默认 3 秒）独立运行。新观察记录首次发现时建立 3 天窗口；每次观测到上破新市值档位都将持久化跟踪锚点和截止时间刷新为 3 天后，无论该档成交量资格是否通过。连续 3 天没有上破新档则过期且不重新激活。BSC 档位为 50 万、100 万、300 万、500 万、1000 万及之后每 1000 万至 1 亿美元；Robinhood、Solana Pump.fun 和 Stonkfun 从 100 万美元开始，后续同档。一次跨过多个档位只处理最高档。成交量/市值比例未达门槛会消耗当前档位；首次在任一档位达标后，将 `volume_qualified` 永久记为真，后续档位免检。
- Telegram：`MEME_TELEGRAM_API_ID`、`MEME_TELEGRAM_API_HASH`、`MEME_TELEGRAM_WATCH_SESSION`。
- Telegram 白名单：`data/config/meme_whitelist.txt`，格式参考 `meme_whitelist.example.txt`。
- 屏蔽发送者：`data/config/meme_blocked_senders.txt`，格式参考 `meme_blocked_senders.example.txt`。
- CA 匹配：EVM 使用 `0x` 加 40 位十六进制；Solana 使用 32-44 位 Base58 公钥格式，并在候选中保存 `chain`。EVM 候选按 Robinhood、BSC 顺序以 Dexscreener 查询并采用首个市场指标完整的链结果；任务 payload、快照、观察、里程碑、叙事和标题沿用该链值，BSC/Robinhood 的发现来源仍是 OKX MemePump 网页。
- 叙事 Telegram：本地 collector 使用 `MEME_NARRATIVE_TELEGRAM_CONFIG`、`MEME_NARRATIVE_TELEGRAM_SESSION` 和 `MEME_NARRATIVE_TELEGRAM_ALLOWED_CHATS`；它与 watcher 使用不同 session，但使用同一白名单。FxTwitter 请求由 `MEME_NARRATIVE_X_TIMEOUT_SECONDS` 控制。
- FOMO：`MEME_FOMO_PROFILE_DIR` 是服务器本地、权限 `0700` 的独立运行时 profile，绝不纳入 Git 或审计 JSON；其中浏览器自身保留的认证状态只供后续按需采集使用，应用不读取、导出、打印或写入 Cookie/JWT。`MEME_FOMO_ENABLED`、`MEME_FOMO_NAVIGATION_TIMEOUT_SECONDS`、`MEME_FOMO_REQUEST_TIMEOUT_SECONDS`、`MEME_FOMO_MAX_PAGES` 和 `MEME_FOMO_PAGE_SIZE` 约束单次按需采集。`MEME_FOMO_HEADLESS` 默认 `false`：FOMO 的 Privy runtime 目前只在有头 Chromium 中就绪，生产 scanner 已由 Xvfb 提供虚拟显示；每次采集仍在完成后立即关闭 Chromium。没有 profile、跳转登录页或认证响应时，collector 标记 `login_required`；scanner 使用 `MEME_FOMO_LOGIN_ALERT_CHAT_ID`（回退 `TELEGRAM_CHAT_ID`）、`MEME_FOMO_LOGIN_ALERT_THREAD_ID`（回退全局 topic）、`MEME_FOMO_LOGIN_ALERT_COOLDOWN_SECONDS` 和 `MEME_FOMO_LOGIN_ALERT_TIMEOUT_SECONDS` 向值班人提醒。`MEME_FOMO_LOGIN_RETRY_SECONDS` 默认 300 秒；`MEME_FOMO_LOGIN_TIMEOUT_SECONDS` 和 `MEME_FOMO_LOGIN_CDP_PORT` 控制一次性 `meme fomo-login` 与 `meme bloomberg-read` 维护窗口及其 loopback DevTools 端口。登录辅助只在选项唯一时自动点击，Google 验证挑战由操作者接管；不会启动常驻 FOMO 服务或公开远程调试端口。
- 快速材料不再使用 `MEME_FAST_EVIDENCE_URL` 或 `MEME_FAST_EVIDENCE_INTERNAL_KEY`，不再请求 HideOnBush。`MEME_LOCAL_EVIDENCE_TIMEOUT` 控制本地采集的总配置上限。
- 最终写作：使用独立的 `MEME_FAST_WRITER_BASE_URL`、`MEME_FAST_WRITER_API_KEY` 和 `MEME_FAST_WRITER_MODEL`，模型默认固定为 `gpt-6-luna`，请求使用 `reasoning_effort=none`，GPT 客户端默认超时为 `90` 秒。Grok补充来自Bot Routine回调；不调用Grok X Search、Grok实体补充或GMGN叙事。
- 未单独设置快速 writer 地址或密钥时才回退 OdAIly 的 `ODAILY_LLM_BASE_URL`、`ODAILY_LLM_API_KEY`；生产应显式配置支持 Luna 的独立 relay。
- 推送接口复用 `ODAILY_PUSH_ENDPOINT`，也可由 `MEME_ODAILY_PUSH_ENDPOINT` 单独覆盖。

## 状态与失败处理

- 最终叙事通过 `narrative_v2` 的生成阶段校验后，scanner 不再维护或执行另一套发布前正文禁词与角度正则；非空正文直接进入标题、正文组装和挂后台流程。
- `queued -> processing -> publishing -> publisher_pending`：正常挂后台路径。
- 临时叙事错误进入 `retry_wait`，最多 3 次，退避 60/300/900 秒；耗尽后 `discarded`。
- `volume_gate_failed`、`tg_market_cap_gate_failed`、`no_usable_narrative`、`queue_expired` 为明确不播报原因。
- 服务重启会把遗留 `processing/publishing` 恢复为可重试状态。
- OKX 网页发现成功后只更新最新发现窗口并释放 Chromium；网页页面失败会保留明确错误，不把空结果或浏览器异常当成“没有迁移代币”，也不会回退官方发现。Dexscreener 市场适配器成功后更新当前市值、24 小时成交量、最高水位和动态调度周期；发现阶段按链批量请求，列表外跟踪按单币请求。任一市场适配器失败只增加 `token_info_failures`、记录 `last_token_info_error` 并按原周期重试，不把失败当作零成交量或诈骗；调度积压写入 worker 日志。
- FOMO 的 `login_required` 不阻断已有 Telegram/FxTwitter 材料；若三路均无正文且 FOMO 登录缺失，任务进入 `retry_wait`，每 `MEME_FOMO_LOGIN_RETRY_SECONDS`（默认 300 秒）再试一次，不受通常 3 次临时重试上限限制，但仍在入队 1 小时后按 `queue_expired` 结束；同时通知在冷却窗口内只发一次。FOMO 其他浏览器或页面错误保留安全错误码，不导出会话或上游原始响应。
- `observations` 的跟踪字段包括代币 `chain`、`tracking_status`、首次开始时间、滑动窗口锚点与截止时间、永久成交量资格、最近 `completed` 时间、最近 `token_info` 时间、下一次调度时间、周期、来源、成交量和失败信息；服务重启后按持久化截止时间、资格和 `next_token_info_at` 恢复，并使用记录的 `chain` 选择对应的 Dexscreener 查询。旧库新增锚点时从原始开始时间回填，新增档位不会依据历史最高市值补发；原 `market_cap_milestones` 档位账本保持不变，避免旧档重触发。历史库的 `chain` 字段优先从该代币最新 `token_snapshots.chain` 回填，找不到时按 BSC 兼容默认值处理。
- `observations` 是当前跟踪状态，不是完整历史；溯源和首次档位判断以 `token_snapshots`、`market_cap_milestones` 为准。
- TG 消息按 `CA + chat_id + message_id` 和转发源双重去重，候选 6 小时冷却，原始提及默认保留 3 天。

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

## 三天数据保留与空间回收

scanner 常驻循环每小时执行清理（启动后的第一次循环也执行），失败写入 poll 日志并重试。普通 `token_snapshots` 按 `observed_at` 保留滚动 72 小时；达到 300 万美元档位的 `market_cap_milestones.snapshot_id` 对应首次突破快照长期保留，不延长跟踪。`ca_mentions` 按 `sent_at` 保留三天；`tg_candidates` 按 `detected_at` 清理三天前且没有任务引用的记录。任务仅清理 `publisher_pending`、`discarded`、`publish_failed`，按 `completed_at`（旧记录回退 `updated_at`）保留三天；其他任务保留直到结束。

过期及旧版 `observations` 清理展示、调度和错误详情，但保留链+CA、跟踪生命周期、来源及最高市值水位，防止旧币重新开启跟踪；`market_cap_milestones` 保留链+CA+档位幂等账本，清理后低档位无快照时 `snapshot_id=0` 表示已清理，失效 `job_id` 置空。300 万美元突破任务的状态、原因、标题和结束时间以 `key_breakthrough_results` 保存，不保留大型叙事材料。此表仅供审计，不重新放回控制台任务列表。`scanner_meta` 保留初始化、扫描节奏及告警冷却状态。

行情快照分批每 1000 条提交，限制运行中写锁和 WAL 膨胀；索引 `idx_token_snapshots_retention` 支持按时间清理。普通删除使空间可复用，不自动缩小数据库文件。首次历史清理及文件压缩必须停止 scanner、TG watcher 和所有其他数据库写入者，运行 `PYTHONPATH=backend .venv/bin/python -m packages.meme_scanner.retention --db data/processed/meme_scanner.sqlite3 --vacuum`，检查输出、SQLite 完整性和磁盘余量后恢复服务。VACUUM 必须预留压缩后数据库及临时文件所需空间；空间不足时不要运行。浏览器认证 profile 和已发布到其他存储的稿件不属于本数据库保留规则。
