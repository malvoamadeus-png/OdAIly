# 控制台-Meme速递

## 职责

`Meme速递` 是控制台一级只读页面，用于查看 OdAIly 内置 Meme worker 生成的候选、任务状态和最终文本。
它不提供阈值编辑、重跑、发布或任务状态修改能力。

## 上游链路

- `odaily-meme-scanner.service` 定期读取 GMGN BSC `completed` 发射代币列表和市值区间。
- `odaily-meme-tg-watcher.service` 监听 Telegram 白名单社群中的真人 CA 消息。
- 普通新币市值达到 50 万美元后才进入播报候选，后续里程碑为 100 万和 300 万美元。
- 社群热议要求 20 分钟内至少 5 次命中、至少 3 个不同真人发送者，且查询时市值达到 30 万美元。
- 两类任务均执行成交量门槛、叙事生成、重试和 OdAIly 挂后台写入逻辑。
- 叙事材料优先使用 watcher 已保存的 CA 消息；配置 Grok 时补充 X Search 材料，再通过 OdAIly LLM 客户端整理为读者正文。无任何可用材料时任务标记为 `no_usable_narrative`，不生成机械占位稿。

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
- Telegram：`MEME_TELEGRAM_API_ID`、`MEME_TELEGRAM_API_HASH`、`MEME_TELEGRAM_WATCH_SESSION`。
- Telegram 白名单：`data/config/meme_whitelist.txt`，格式参考 `meme_whitelist.example.txt`。
- 屏蔽发送者：`data/config/meme_blocked_senders.txt`，格式参考 `meme_blocked_senders.example.txt`。
- Grok X Search：可选 `MEME_GROK_BASE_URL`、`MEME_GROK_API_KEY`、`MEME_GROK_MODEL`。
- 正文整理复用 OdAIly 的 `ODAILY_LLM_BASE_URL`、`ODAILY_LLM_API_KEY`，模型可由 `MEME_WRITER_MODEL` 覆盖。
- 推送接口复用 `ODAILY_PUSH_ENDPOINT`，也可由 `MEME_ODAILY_PUSH_ENDPOINT` 单独覆盖。

## 状态与失败处理

- `queued -> processing -> publishing -> publisher_pending`：正常挂后台路径。
- 临时叙事错误进入 `retry_wait`，最多 3 次，退避 60/300/900 秒；耗尽后 `discarded`。
- `volume_gate_failed`、`tg_market_cap_gate_failed`、`no_usable_narrative`、`queue_expired` 为明确不播报原因。
- 服务重启会把遗留 `processing/publishing` 恢复为可重试状态。
- TG 消息按 `CA + chat_id + message_id` 和转发源双重去重，候选 6 小时冷却，原始提及默认保留 90 天。

## 页面字段

- 链、平台、CA、名称、symbol。
- 当前市值、24 小时成交量和触发档位。
- 普通新币或社群热议触发类型。
- 社群热议的命中数、不同发送者数和群数。
- 任务状态、未播报原因、排队和更新时间。
- 已生成标题和正文；尚未生成时展示当前状态或失败原因。

## 读取策略

- 首次进入页面时读取一次。
- 顶部刷新按钮只刷新本页。
- 页面不自动轮询，不触发 GMGN、Telegram、叙事模型或发布接口。
