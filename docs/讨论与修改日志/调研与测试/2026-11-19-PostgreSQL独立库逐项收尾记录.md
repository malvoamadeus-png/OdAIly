# PostgreSQL 独立库逐项收尾记录

2026-11-19，官方服务器。按业务确认配置、运行状态和必要增量后，删除对应 SQLite 主文件及 WAL/SHM；已导入的历史不重复搬运。以下均为本次现场结果，不表示主库或全部迁移已完成。

## 已清理

- Hyperliquid：地址、基线、Prompt、派发和账号备注已核对，监测、普通监测和派发完成一轮。删除独立 SQLite 文件组；随后删除 PostgreSQL 中不再使用的 `legacy_main.hl_news_fills`，当前成交表为 `hyperliquid_news.hl_news_fills`。
- HotTopic：2,558 个账号开关及 9 个事件追踪 Prompt 一致；178 条旧发布 outbox 状态均有对应。旧待处理任务按业务键在 PostgreSQL 中均有结果。删除 `data/runtime/hottopic.sqlite` 文件组。
- Meme：链配置、Grok 状态和旧库未完成任务均有 PostgreSQL 对应。删除 `data/processed/meme_scanner.sqlite3` 文件组。
- Gate：设置、交易对配置、模板和旧播报状态一致，历史价格采样按留存处理。删除 `data/runtime/gate_market.sqlite` 文件组。
- 股票：设置一致；两条缺少的旧播报早于 PostgreSQL 当前最近 500 条的范围，符合滚动留存。删除 `data/runtime/stock_market.sqlite` 文件组。
- 宏观快讯、MSX、搜索缓存、耗时看板及旧 AI 文本自升级运行库：核对对应状态或留存后，分别删除 `macro_newsflash.sqlite`、`msx_notice.sqlite`、`data/processed/searcher/searcher.sqlite`、`pipeline_timing.sqlite` 和 `data/runtime/ai_text_self_upgrade.sqlite` 文件组。
- Writer3、旧 Predict REST 和 Predict HTML：导入报告和主键已核对，分别删除 `data/processed/writer3/writer3.sqlite`、`predict_market_discovery.sqlite` 和 `predict_html_discovery.sqlite` 文件组。停用的 `odaily-writer3.service` 与 `odaily-predict-market.service` 已安装 PostgreSQL 环境配置，仍保持停用。
- 自升级观测库：79 条案例在 PostgreSQL；唯一未迁入运行记录是切换时中断的旧 `running`，后续 PostgreSQL 运行已完成。删除 `/data/OdAIly/system-observability/ai_text_self_upgrade.sqlite` 文件组，不恢复旧运行记录。
- 系统信息与小证据 SQLite：业务表和 449,813 条小证据已有导入检查点，420 条巡检样本均在 PostgreSQL。切换窗口遗漏的四条 X 写作响应文件已校验 hash 并补登记及 pin；127 个旧巡检样本 pin 对应的原始证据已过期释放。删除两份 SQLite 文件组，回收约 28.6 GiB；外部证据文件和主业务数据未随之删除。
- 商务快讯：切换窗口漏掉 4 条终态消息，其中两条已挂后台。按 `(chat_id, message_id, update_id)` 补入 PostgreSQL，SQLite 的 `plugin_notice_result` 文本 `sent` 保留为 JSON 字符串；原 ID 764–767 与 PostgreSQL 已有 ID 冲突，分配为 784–787。逐条状态与接收游标核对后删除 `data/runtime/business_news.sqlite` 文件组；补录不触发重发。原 `business_news.json` 失败报告仍是历史记录，不作为此次补录的结果。
- 本地队列：旧未完成任务按业务键核对，9 条切换遗漏的过时快讯在 PostgreSQL 中标记 `legacy_skipped`，不重发；其余有对应队列或终态。删除 `data/runtime/local_pipeline.sqlite` 文件组。
- AI 审核：已处理的人工决定均有对应；两条未过期建议补入后被当前候选筛选再次移除，关联事件仍在。删除 `data/runtime/ai_review.sqlite` 文件组。
- 插件本地库：反馈、阅读和有效会话已核对；两条未过期提醒补入 PostgreSQL，商务提醒从旧消息 ID 766 映射到新 ID 786。删除 `data/runtime/editor_plugin_local.sqlite` 文件组。

清理后相关活动服务为 `active`；根盘可用空间约 6.0 GiB。无需将本记录理解为全部模块或全部历史数据完成验收。

## 暂留

- PostgreSQL 队列仍有约 910 条历史记录显示 pending 且已达 3 次重试上限，当前领取逻辑不会执行它们；新任务正常运行。状态展示可单独修正，不能降低重试计数来重放旧内容。
- 服务器默认 `.env` 仍选择 SQLite；运行已配置的生产服务外，手动命令或其他停用服务须显式选择 PostgreSQL 环境。
- 主业务 SQLite 库仍待处理。PostgreSQL 系统信息历史副本由六小时留存任务分批清理；部分业务来源尚未安装原生信息流触发器，不能将其当作实时覆盖完成。
