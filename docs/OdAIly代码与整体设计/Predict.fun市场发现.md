# Predict.fun 市场发现

`predict-html` 每小时读取 Predict.fun 金融、科技、经济、文化、政治栏目的公开 HTML，仅扫描每个栏目的热门首屏；平台并不保证新类目都出现在首屏，可能遗漏。控制台「信源与监测 / 预测市场 / Predict / 上新」展示每栏扫描结果、发现记录、官方规则和草稿。Polymarket、体育、电竞尚未实现。

每栏首次成功扫描单独建立基线；失败不建立基线。后续按类目 ID 跨栏目去重，首次看到且类目 `createdAt` 落在过去48小时内的主题进入队列。旧类目下的新子市场不会另行出稿。扫描记录为类别的总量、首屏数量和分页标记；页面变化、缺少字段或网络失败时记录错误。

资产价格、行情及指数的涨跌预测不论周期均排除；明确的涨跌模板和每日天气用规则排除，其余资产涨跌及日常重复题由写作模型判断。候选读取官方市场详情 HTML 的 `description`，并通过一次带 `PREDICT_FUN_API_KEY` 的官方 REST 类目请求获取每个子市场独立规则，再使用 `predict-new-v2` 专用 Prompt 由编写者1结合规则写出自然的陈述式主题和规则摘要，标题保留估值、市值等必要比较指标，避免复述选项占位词；编写者2 `format_brief` 规范格式。正文首句之后换段写结算规则。缺少规则或模型判断规则不完整时不投递。题目是预测，不是已经发生的事实。每个主题一篇。

SQLite 模式下运行数据保存在独立的 `data/runtime/predict_html_discovery.sqlite`，可用 `PREDICT_HTML_DB_PATH` 覆盖，旧 REST 数据库不作为基线。PostgreSQL 模式使用 `main.predict_html_feed_baselines`、`main.predict_html_topics` 和 `main.predict_html_scan_runs`；处理中的主题使用有期限 claim，投递不确定状态不自动重发，过期的投递 claim 变为 `delivery_uncertain`。状态包括 `baseline`、`too_old`、`invalid_time`、`filtered`、`pending`、`processing`、`retry_wait`、`rules_incomplete`、`ready`、`delivering`、`backgrounded` 和 `delivery_uncertain`。请求挂后台固定 `isPublish=false,isPush=false`。PostgreSQL 下控制台从同一组原生表读取，不访问 SQLite 文件。

```bash
python backend/src/main.py predict-html status
python backend/src/main.py predict-html run --once --dry-run
python backend/src/main.py predict-html run
```

生产 unit 依赖本机 `postgresql.service`，从 `/etc/odaily/postgresql.env` 读取 `ODAILY_POSTGRES_DSN`，并设置 `ODAILY_STORAGE_BACKEND=postgres`。

`--dry-run` 不投递，但会写扫描、基线和草稿状态；验证时必须使用单独的 `PREDICT_HTML_DB_PATH`。服务为 `odaily-predict-html.service`。旧 REST 扫描服务 `odaily-predict-market.service` 停用，不作为新功能的数据源；其官方服务器 unit 在重新启用时使用专用 PostgreSQL 环境文件和 `ODAILY_STORAGE_BACKEND=postgres`。控制台接口 `/console/predict-html/get` 要求控制台管理员认证，仅只读访问新库。
