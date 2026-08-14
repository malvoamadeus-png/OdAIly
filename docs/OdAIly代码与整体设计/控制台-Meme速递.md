# 控制台-Meme速递

## 职责

`Meme速递` 是控制台一级只读页面，用于查看 OdAIly 内置 Meme worker 生成的候选、任务状态和最终文本。
它不提供阈值编辑、重跑、发布或任务状态修改能力。

## 上游链路

- `odaily-meme-scanner.service` 每 5 分钟无市值筛选读取 GMGN BSC `completed` 发射代币列表；`completed` 只承担首次发现窗口，不是完整的市值扫描源。
- `odaily-meme-tg-watcher.service` 监听 Telegram 白名单社群中的真人 CA 消息。
- 普通新币首次进入 `completed` 即建立 7 天跟踪窗口，市值达到 50 万美元后进入播报候选，后续里程碑为 100 万和 300 万美元；首次发现已跨多档时只触发最高档。
- 仍在最近一次 `completed` 列表中的代币直接使用列表市值，不调用 `token_info`。滚出列表但仍在 7 天窗口内的代币由持久化调度器调用 `token_info`：市值低于 50 万美元每小时一次，达到 50 万美元每 5 分钟一次。固定 CA hash 相位、全局最小 3 秒间隔和单线程请求用于错峰限流。
- 7 天从首次发现时间计算；到期后状态改为 `expired`，清理下一次调度时间，即使重新出现在 `completed` 也不重新激活。迁移前的历史 `observations` 标记为 `legacy_untracked`，不会回填跟踪窗口。
- 社群热议要求 20 分钟内至少 5 次命中、至少 3 个不同真人发送者；`0x...` EVM CA 只查询 BNB Chain（30 万美元）和 Robinhood Chain（100 万美元），Solana Base58 CA 查询 Solana（50 万美元），其他链暂不触发。门槛按代币查询返回的链判断，不按来源社群名判断。
- 普通里程碑使用独立的市值最高水位；热议任务和发布结果刷新当前市值时，不推进普通里程碑水位，避免热议先触发后吞掉 50 万或 100 万档。
- `token_snapshots` 以 CA 为主键维度保存每次成功调度到的链、平台、symbol、市值、成交量、时间、来源（`completed`/`token_info`/`tg`）和原始 payload；`market_cap_milestones` 保存 CA+档位的首次观测时间、快照 ID 和任务状态。热议先发现的记录在后续 `completed` 扫描时仍会激活跟踪，并依据里程碑账本判定首次跨档。
- 两类任务均执行成交量门槛、叙事生成、重试和 OdAIly 挂后台写入逻辑。
- 挂后台接口成功后同步写入信息流插件本地 store，使用 `meme_digest` 类型进入高频区并显示“Meme挂后台”；信息流写入失败只记录日志，不回滚已经成功的挂后台结果。
- 叙事生成使用 CommunityMonitor V2 材料契约：对精确 CA 做 Telegram 白名单全历史搜索，保留全局最早 20 条和最新 20 条命中，并为每个命中回读前 2 条、后 15 条上下文；X 使用 FxTwitter 精确 CA 的 `top` 两页和 `latest` 两页，去重后仅排除正文含 `gmgn.ai/` 链接的帖子；GMGN 并行读取精确链和 CA 的 `data.zh_cn`；Grok 只执行独立研究和实体补充，最后由 GPT writer 生成读者正文。程序确定性添加 `据Odaily Meme速递监测，` 开头、`Grok补充：`/`GMGN补充：` 来源段落标记和末尾免责声明。叙事审计复用 `jobs.narrative_json` 保存各阶段材料、调用诊断和最终判断；真正没有可用材料时任务才标记为 `no_usable_narrative`，模型、网络、JSON 或校验异常记录具体阶段并进入 `retry_wait`。

## 文本口径

普通新币：

```text
Meme速递：BSC上{symbol}市值突破{market_cap}万美元
```

社群热议：

```text
Meme速递：BSC上{symbol}社群热议中，市值{market_cap}万美元
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
- 叙事审计的“性能与调用诊断”展示 Telegram、FxTwitter、GMGN、Grok 研究、实体补充和最终写作各调用耗时；Telegram、FxTwitter、GMGN、Grok 研究首轮调用并行，Grok 研究与实体补充在依赖满足后也可并行，因此各调用耗时之和可能大于叙事总耗时。
- `GET /console/meme/detail?id=<job_id>` 按需返回单条任务的完整 `narrative_json`。旧库或旧任务没有该字段时返回 `available=false`，不影响列表。
- 数据库不存在、不可读或 schema 不兼容时，接口返回 `available=false` 和错误文本，不创建空库。

## 命令与服务

```text
python backend/src/main.py meme scan --once
python backend/src/main.py meme scan --send
python backend/src/main.py meme tg-watch --check
python backend/src/main.py meme tg-watch
```

- `meme scan --once`：执行一次 GMGN 发现并最多处理一个任务，默认 dry-run。
- `meme scan --send`：常驻轮询并真实写入挂后台稿件，固定 `isPublish=false,isPush=false`。
- `meme tg-watch --check`：校验 Telegram 登录和白名单可见性后退出。
- systemd unit：`deploy/odaily-meme-scanner.service`、`deploy/odaily-meme-tg-watcher.service`。

## 配置

- GMGN：`GMGN_API_KEY`，可选 `GMGN_HTTPS_PROXY`；服务器需安装 `gmgn-cli`。
- 跟踪调度：`MEME_COMPLETED_SCAN_INTERVAL`（默认 300 秒）、`MEME_TOKEN_INFO_HIGH_INTERVAL`（默认 300 秒）、`MEME_TOKEN_INFO_LOW_INTERVAL`（默认 3600 秒）、`MEME_TRACKING_WINDOW_SECONDS`（默认 604800 秒）、`MEME_TOKEN_INFO_MIN_GAP_SECONDS`（默认 3 秒）。
- Telegram：`MEME_TELEGRAM_API_ID`、`MEME_TELEGRAM_API_HASH`、`MEME_TELEGRAM_WATCH_SESSION`。
- Telegram 白名单：`data/config/meme_whitelist.txt`，格式参考 `meme_whitelist.example.txt`。
- 屏蔽发送者：`data/config/meme_blocked_senders.txt`，格式参考 `meme_blocked_senders.example.txt`。
- CA 匹配：EVM 使用 `0x` 加 40 位十六进制；Solana 使用 32-44 位 Base58 公钥格式，并在候选中保存 `chain`，查询 GMGN 时按对应链路请求。
- 叙事 Telegram 配置：`MEME_TELEGRAM_CONFIG`、`MEME_TELEGRAM_NARRATIVE_SESSION`、`MEME_TELEGRAM_ALLOWED_CHATS`；默认读取 `data/config/meme_telegram.txt`、`data/processed/meme_telegram_narrative` 和 `data/config/meme_whitelist.txt`。
- FxTwitter CA 搜索：调用公开 `https://api.fxtwitter.com/2/search`，不需要 Grok 凭证；Grok 研究/实体补充读取 `GROK_BASE_URL`、`GROK_API_KEY`、`GROK_MODEL`，也兼容 `MEME_GROK_BASE_URL`、`MEME_GROK_API_KEY`、`MEME_GROK_MODEL`；默认模型为 `grok-4.5`。
- GMGN 叙事：通过有头 Playwright 浏览器在 `xvfb-run` 虚拟显示器中访问公开 `https://gmgn.ai/api/v1/token_ai_narrative/{chain}/{token_address}`，不需要登录、API Key 或 Authorization；公共查询参数可由 `MEME_GMGN_DEVICE_ID`、`MEME_GMGN_FP_DID`、`MEME_GMGN_CLIENT_ID`、`MEME_GMGN_APP_VER`、`MEME_GMGN_TZ_NAME`、`MEME_GMGN_TZ_OFFSET`、`MEME_GMGN_APP_LANG` 覆盖，代理可由 `MEME_GMGN_HTTPS_PROXY` 或 `GMGN_HTTPS_PROXY` 指定，超时由 `MEME_GMGN_TIMEOUT` 控制，页面稳定等待由 `MEME_GMGN_BROWSER_SETTLE_MS` 控制。服务器需安装 Playwright Chromium 和 `xvfb-run`。GMGN 403/429、浏览器依赖缺失等补充接口失败只记录诊断，不阻断已有主材料写作。
- 正文整理复用 OdAIly 的 `ODAILY_LLM_BASE_URL`、`ODAILY_LLM_API_KEY`，模型可由 `MEME_WRITER_MODEL` 覆盖。
- 推送接口复用 `ODAILY_PUSH_ENDPOINT`，也可由 `MEME_ODAILY_PUSH_ENDPOINT` 单独覆盖。

## 状态与失败处理

- `queued -> processing -> publishing -> publisher_pending`：正常挂后台路径。
- 临时叙事错误进入 `retry_wait`，最多 3 次，退避 60/300/900 秒；耗尽后 `discarded`。
- `volume_gate_failed`、`tg_market_cap_gate_failed`、`no_usable_narrative`、`queue_expired` 为明确不播报原因。
- 服务重启会把遗留 `processing/publishing` 恢复为可重试状态。
- `token_info` 成功后更新当前市值、24 小时成交量、最高水位和动态调度周期；失败只增加 `token_info_failures`、记录 `last_token_info_error` 并按原周期重试，不推进市值水位。429 会记录服务端退避时间并暂停全局 `token_info` 请求；调度积压写入 worker 日志。
- `observations` 的跟踪字段包括 `tracking_status`、7 天起止时间、最近 `completed` 时间、最近 `token_info` 时间、下一次调度时间、周期、来源、成交量和失败信息；服务重启后按 `next_token_info_at` 恢复。
- `observations` 是当前跟踪状态，不是完整历史；溯源和首次档位判断以 `token_snapshots`、`market_cap_milestones` 为准。
- TG 消息按 `CA + chat_id + message_id` 和转发源双重去重，候选 6 小时冷却，原始提及默认保留 90 天。

## 页面字段

- 链、平台、CA、名称、symbol。
- 当前市值、24 小时成交量和触发档位。
- 普通新币或社群热议触发类型。
- 社群热议的命中数、不同发送者数和群数。
- 任务状态、未播报原因、排队和更新时间。
- 已生成标题和正文；尚未生成时展示当前状态或失败原因。
- 叙事审计入口默认收起，点击后懒加载详情；顶层分组同一时间只展开一个：失败诊断/运行状态、最终判断、Telegram 消息、X CA 搜索、Grok/GMGN 叙事材料、性能与调用诊断。
- Telegram 分组再分为最老 20 条和最新 20 条命中，每条命中单独展开查看命中消息、发送者、群组、时间及前 2/后 15 条上下文。
- 最终判断显示 Grok 类型假设、最终类型、source/angle/supplement、使用/丢弃材料、`decision_code`、`decision_reason` 和 `reader_text`；为空时显示“未判断/未形成”和确定性原因。

## 读取策略

- 首次进入页面时读取一次。
- 顶部刷新按钮只刷新本页。
- 页面不自动轮询，不触发 GMGN、Telegram、叙事模型或发布接口。

## 叙事契约

内部审计保存 `status`、`failure_stage`、`failure_code`、`failure_message`、`material_counts`、`decision_code`、`decision_reason`、`telegram_contexts`、`telegram_messages`、`x_posts`、`x_excluded_posts`、`gmgn_supplement`、`gmgn_diagnostic`、`grok_research`、`entity_supplements`、最终分类材料、使用/丢弃材料、`reader_text` 和性能诊断；读者正文只输出最终炒作角度、GMGN补充和固定免责声明。命中频次、群组数量、用户情绪、税务或官网链接、机器人卡片和检索过程不能进入正文。详细规则见 控制台-Meme速递叙事规范.md。

## 白名单外发现

执行 meme tg-discover 可通过 Telegram 全局搜索 0x，列出当前账号可见且不在白名单中的群组/频道、真人、机器人和频道帖子命中数及代表性消息。命令只输出 JSON 和 Markdown 报告，不自动修改白名单。
