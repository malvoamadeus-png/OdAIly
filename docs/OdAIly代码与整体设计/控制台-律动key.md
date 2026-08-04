# 控制台-律动key

## 职责

`信源管理 / 律动key` 子页用于查看和明文编辑当前 BlockBeats API Key，并展示最近一次抓取得到的 key 状态和自动申请状态。

BlockBeats API Key 明文保存，便于在临时邮箱批量注册的 key 之间手工切换。该 key 只用于只读抓取，不进入业务主 SQLite。

## 数据边界

BlockBeats API Key 与运行状态写入 Linux 本地 JSON：

- 默认路径：`data/config/blockbeats_key.json`
- 覆盖环境变量：`BLOCKBEATS_KEY_CONFIG_PATH`
- 控制台接口：`POST /console/blockbeats-key/get`、`POST /console/blockbeats-key/save`
- 文件字段：`api_key`、`status`、`last_checked_at`、`last_success_at`、`last_quota_error_at`、`last_error`、`last_error_payload`、`updated_at`、`updated_by`、`auto_register_status`、`last_auto_register_at`、`last_auto_register_error`、`last_auto_register_error_payload`

`status` 可为：

- `unknown`：新 key 保存后或尚未检查。
- `ok`：最近一次 BlockBeats 抓取成功。
- `quota_exhausted`：BlockBeats 返回额度不足或 429。
- `request_failed`：普通请求失败。
- `missing_key`：本地文件和 `.env` 都没有可用 key。

`auto_register_status` 可为：

- `idle`：尚未触发自动申请。
- `running`：正在申请临时邮箱、验证码和新 key。
- `succeeded`：已申请并替换当前 key。
- `failed`：最近一次自动申请失败；worker 会在冷却时间内停止重复申请。

## 控制台能力

控制台支持：

- 查看和明文编辑当前 BlockBeats API Key。
- 保存新 key。
- 查看 BlockBeats 最近检查、最近成功、额度不足时间和最近错误摘要。
- 查看最近一次自动申请的状态、时间和错误摘要。

保存新 BlockBeats key 时，后端会清空旧错误、旧额度不足时间，并把状态重置为 `unknown`。读取和保存都走 `editor-plugin-api-server`，接口使用本地操作者 session；key 数据本身不写入业务主 SQLite。

## 生效方式

`competitor-monitor-worker` 每轮抓取 BlockBeats 前读取本地 JSON 的最新 key；本地 key 为空时回退 `.env` / 环境变量里的 `BLOCKBEATS_API_KEY`。默认启用自动申请：首次没有可用 key，或当前 key 被判定额度不足 / 429 时，worker 调用内置注册器，通过 mail.tm 临时邮箱完成注册、获取免费 API key 并原子替换本地 JSON，然后只用新 key 重试当前轮一次。注册失败不会覆盖旧 key；自动申请失败后默认冷却 `3600` 秒，避免反复创建账号。

自动申请由以下环境变量控制：

- `BLOCKBEATS_AUTO_REGISTER_ENABLED`：默认 `true`；设为 `false` 时恢复人工 key / 仅记录失败的模式。
- `BLOCKBEATS_REGISTRATION_TIMEOUT_SECONDS`：验证码等待上限，默认 `120` 秒。
- `BLOCKBEATS_AUTO_REGISTER_COOLDOWN_SECONDS`：自动申请失败后的冷却时间，默认 `3600` 秒。

控制台仍可随时手工保存 key；保存后下一轮 worker 直接读取新 key，不需要重启。临时邮箱地址、邮箱密码和 BlockBeats 注册密码只在注册进程内存中存在，不写入配置文件或业务 SQLite。

BlockBeats 额度不足、缺 key 或普通请求失败只在本页展示，不作为 Telegram worker 健康告警条件。

## 相关文档

- `收集者-竞品.md`：worker 读取 key 与写回状态的位置。
- `控制台-竞品配置.md`：竞品排除词维护。
