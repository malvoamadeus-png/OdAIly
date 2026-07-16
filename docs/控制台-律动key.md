# 控制台-律动key

## 职责

`信源管理 / 律动key` 子页用于查看和明文编辑 BlockBeats API Key，并展示最近一次抓取得到的 key 状态。

BlockBeats API Key 明文保存，便于在临时邮箱批量注册的 key 之间手工切换。该 key 只用于只读抓取，不进入 Supabase。

## 数据边界

BlockBeats API Key 与运行状态写入 Linux 本地 JSON：

- 默认路径：`data/config/blockbeats_key.json`
- 覆盖环境变量：`BLOCKBEATS_KEY_CONFIG_PATH`
- 控制台接口：`POST /console/blockbeats-key/get`、`POST /console/blockbeats-key/save`
- 文件字段：`api_key`、`status`、`last_checked_at`、`last_success_at`、`last_quota_error_at`、`last_error`、`last_error_payload`、`updated_at`、`updated_by`

`status` 可为：

- `unknown`：新 key 保存后或尚未检查。
- `ok`：最近一次 BlockBeats 抓取成功。
- `quota_exhausted`：BlockBeats 返回额度不足或 429。
- `request_failed`：普通请求失败。
- `missing_key`：本地文件和 `.env` 都没有可用 key。

## 控制台能力

控制台支持：

- 查看和明文编辑当前 BlockBeats API Key。
- 保存新 key。
- 查看 BlockBeats 最近检查、最近成功、额度不足时间和最近错误摘要。

保存新 BlockBeats key 时，后端会清空旧错误、旧额度不足时间，并把状态重置为 `unknown`。读取和保存都走 `editor-plugin-api-server`，接口继续使用控制台 Supabase Auth 会话与 `console_admins` 白名单；只是 key 数据本身不写入 Supabase。

## 生效方式

`competitor-monitor-worker` 每轮抓取 BlockBeats 前读取本地 JSON 的最新 key；本地 key 为空时才回退 `.env` / 环境变量里的 `BLOCKBEATS_API_KEY`。保存新 key 后不需要重启 worker。

BlockBeats 额度不足、缺 key 或普通请求失败只在本页展示，不作为 Telegram worker 健康告警条件。

## 相关文档

- `收集者-竞品.md`：worker 读取 key 与写回状态的位置。
- `控制台-竞品配置.md`：竞品排除词维护。
