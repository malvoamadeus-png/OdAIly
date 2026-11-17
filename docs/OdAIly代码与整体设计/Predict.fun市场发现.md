# Predict.fun 市场发现

`predict-market` 是独立只读 worker，从 Predict.fun 官方 REST API 分页读取开放类目和嵌套市场，为人工选题提供增量候选。它不创建 `tasks`，不调用发布接口，也不把未经编辑判断的市场推入快讯流。

## 发现逻辑

- 请求 `GET /v1/categories?first=100&sort=PUBLISHED_AT_DESC&status=OPEN`，通过 `cursor` 分页；主网使用 `x-api-key`。
- 默认每 10 分钟扫描一次，每轮最多 80 页、每页 100 个类目。分页游标可能随列表变化，程序按市场 ID 持久去重；触及页数上限会在轮次结果中报告 `page_limit_reached`。
- 首轮建立已见市场基线，同时把开始时间在当前时刻前后 72 小时且 `tradingStatus=OPEN` 的市场放入复核队列；市场自身的 `status` 可能是 `REGISTERED`，不能要求它等于 `OPEN`。首轮既有市场不会计为新发现。后续扫描只将首轮后首次见到的市场计为新市场。
- 时间统一转为 UTC 后按市场所属类目的 `startsAt` 筛选。每个市场自身的 `createdAt` 也会保存；类目的 `publishedAt` 不被误当成子市场创建时间。
- `CRYPTO_UP_DOWN`、`UP_DOWN` 变体，以及标题明确命中天气/气温/降雨/降雪词的市场保存在已见账本中，但不进入人工候选。此规则只是压制常见重复模板，不代表“有趣”判断。
- 一个类目下同一主题有多个期限或选项市场时，目前按市场 ID 分别存储，并在每条记录上保留类目标题、slug 和开始时间，供人工识别和合并。

## 状态与命令

状态存入独立 SQLite：`data/runtime/predict_market_discovery.sqlite`。`status` 返回最近扫描结果、已见市场数及当前 72 小时窗口内的复核候选。worker 每轮把摘要和候选 JSON 写入 journald。

```bash
python backend/src/main.py predict-market init-db
python backend/src/main.py predict-market run --once
python backend/src/main.py predict-market status
python backend/src/main.py predict-market run
```

生产 systemd unit 为 `deploy/odaily-predict-market.service`，服务名为 `odaily-predict-market.service`。

## 配置

在服务器 `/opt/OdAIly/.env` 设置：

```text
PREDICT_FUN_API_KEY=...
```

可选变量：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `PREDICT_FUN_API_BASE_URL` | `https://api.predict.fun` | REST API 地址 |
| `PREDICT_MARKET_DB_PATH` | `data/runtime/predict_market_discovery.sqlite` | 独立 SQLite 路径 |
| `PREDICT_MARKET_POLL_INTERVAL_SECONDS` | `600` | 轮询间隔，最小 60 秒 |
| `PREDICT_MARKET_REQUEST_TIMEOUT_SECONDS` | `15` | 单次 HTTP 请求超时 |
| `PREDICT_MARKET_MAX_PAGES` | `80` | 单轮最大页数，范围 1 至 100 |
| `PREDICT_MARKET_PAGE_SIZE` | `100` | 每页类目数，范围 1 至 100 |

主网 key 必须从 Predict.fun Developer Console 申请。REST 通用限流为每 app 每分钟 250 次；默认配置最多每 10 分钟请求 80 页。GraphQL 不作为后台依赖，因为官方 REST 已实测可在 Linux 请求，GraphQL 在该服务器上返回 Cloudflare `403`。

“有趣”仍由人工复核决定。当前扫描器只做重复模板规则过滤，不自动分类、写稿或发布。
