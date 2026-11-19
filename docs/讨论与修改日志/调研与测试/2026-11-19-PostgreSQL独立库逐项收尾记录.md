# PostgreSQL 独立库逐项收尾记录

2026-11-19，官方服务器。按业务确认配置、运行状态和必要增量后，删除对应 SQLite 主文件及 WAL/SHM；已导入的历史不重复搬运。以下均为本次现场结果，不表示主库或全部迁移已完成。

## 已清理

- Hyperliquid：地址、基线、Prompt、派发和账号备注已核对，监测、普通监测和派发完成一轮。删除独立 SQLite 文件组；随后删除 PostgreSQL 中不再使用的 `legacy_main.hl_news_fills`，当前成交表为 `hyperliquid_news.hl_news_fills`。
- HotTopic：2,558 个账号开关及 9 个事件追踪 Prompt 一致；178 条旧发布 outbox 状态均有对应。旧待处理任务按业务键在 PostgreSQL 中均有结果。删除 `data/runtime/hottopic.sqlite` 文件组。
- Meme：链配置、Grok 状态和旧库未完成任务均有 PostgreSQL 对应。删除 `data/processed/meme_scanner.sqlite3` 文件组。
- Gate：设置、交易对配置、模板和旧播报状态一致，历史价格采样按留存处理。删除 `data/runtime/gate_market.sqlite` 文件组。
- 股票：设置一致；两条缺少的旧播报早于 PostgreSQL 当前最近 500 条的范围，符合滚动留存。删除 `data/runtime/stock_market.sqlite` 文件组。
- 宏观快讯、MSX、搜索缓存、耗时看板及旧 AI 文本自升级运行库：核对对应状态或留存后，分别删除 `macro_newsflash.sqlite`、`msx_notice.sqlite`、`data/processed/searcher/searcher.sqlite`、`pipeline_timing.sqlite` 和 `data/runtime/ai_text_self_upgrade.sqlite` 文件组。未删除观测目录下的同名自升级库。
- 商务快讯：切换窗口漏掉 4 条终态消息，其中两条已挂后台。按 `(chat_id, message_id, update_id)` 补入 PostgreSQL，SQLite 的 `plugin_notice_result` 文本 `sent` 保留为 JSON 字符串；原 ID 764–767 与 PostgreSQL 已有 ID 冲突，分配为 784–787。逐条状态与接收游标核对后删除 `data/runtime/business_news.sqlite` 文件组；补录不触发重发。原 `business_news.json` 失败报告仍是历史记录，不作为此次补录的结果。

清理后相关活动服务为 `active`；根盘可用空间约 6.0 GiB。无需将本记录理解为全部模块或全部历史数据完成验收。

## 暂留

- `local_pipeline.sqlite`：910 条旧 pending 在 PostgreSQL 仍为 pending，7 条已成功；另有 10 条旧 pending 没有 PostgreSQL 队列项，关联主任务仍为 pending。它们已延迟数小时，不能未经业务判断直接重发过时消息。
- `ai_review.sqlite`：有 24 条旧待处理建议未在 PostgreSQL 找到，其中 22 条超过三天留存窗口，另两条尚未超过窗口；已处理建议有对应记录。
- `editor_plugin_local.sqlite`：一个缺失会话已过期，44 条缺失 feed 中 42 条已过期，另两条是当天旧提醒；商务消息补录后 ID 有变化，不能直接复制旧 feed 引用。
- `writer3.sqlite`、`predict_market_discovery.sqlite` 和 `predict_html_discovery.sqlite`：导入报告及主键已核对，但相关旧服务处于停用状态，`odaily-writer3.service` 与 `odaily-predict-market.service` 尚未注入 PostgreSQL 环境；服务器默认 `.env` 仍选择 SQLite，须先解决再次启动的存储路径。
- `/data/OdAIly/system-observability/ai_text_self_upgrade.sqlite`：旧库有一个 PostgreSQL 中不存在的 `running` 记录，需确认其恢复或放弃方式。
- 主库、系统信息库及 small evidence 不属于本次清理范围。
