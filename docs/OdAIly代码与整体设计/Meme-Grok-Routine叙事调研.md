# Meme Grok Routine 叙事调研

## 用途

`meme grok-narrative` 是人工触发的一次性调研入口。它把 CA、可选链和代币名、可选线索提交给外部自动化 Webhook，调查市场在用什么角度和理由炒作该代币。当前接入的是用户提供的 Cursor 自动化端点，由它触发 Grok Bot；这不是直接调用 xAI X Search API。该命令不接入 Meme速递自动任务、worker、挂后台或发布流程。

Routine 调查只整理市场叙事与证据，不生成买卖建议或价格预测。请求中的 `notes` 只作为检索线索；调查结果仍由 Routine 独立检索并按其结果契约返回。

## 命令

```bash
python backend/src/main.py meme grok-narrative \
  --ca AhBidGAgv6QbKSWfrSCegGrzFLcBx4E5FNYTKHNQQj6C \
  --chain solana \
  --token LAUNCH \
  --wait-for-result
```

可选字段：

- `--request-id`：调用方的关联 ID；省略时生成 UUID。
- `--webhook-url`：覆盖 Webhook 地址；默认优先读取 `grok.txt` 的 `url=...`，再读取 `GROK_ROUTINE_WEBHOOK_URL`。
- `--credentials-file`：凭证文件；默认读取仓库根目录的 `grok.txt`。文件保存 `url=...`、`key=...`、`header=Authorization: Bearer`。
- `--callback-url`：指定外部公网 HTTPS 回调地址；不能和 `--wait-for-result` 同时使用。
- `--wait-for-result`：先向官方服务器申请一次性回调地址，再提交 Grok 调研；CLI 等待服务器收到结果后打印并保存 JSON。
- `--callback-base-url`：覆盖官方回调服务基址；默认 `https://api.odaily.uk/hooks/grok-meme-result`。
- `--result-timeout`：等待结果秒数，默认 900。
- `--poll-interval`：CLI 轮询间隔秒数，默认 5。
- `--output`：结果 JSON 保存路径；默认保存到 `data/exports/meme_grok_results/<request_id>.json`。
- `--notes`：辅助调查的已知线索。
- `--timeout`：Webhook HTTP 请求超时，默认 20 秒。

## 认证与配置

- 当前 Cursor 自动化端点使用 Bearer 认证。命令读取 `grok.txt` 的 `key=...` 和 `header=Authorization: Bearer` 两项；header 可只写 scheme，也可重复包含同一个 key。两项不匹配时命令拒绝发送，并统一构造 `Authorization: Bearer <key>`；它不要求 `whsec_...` signing secret。
- Webhook URL 和 Bearer key 从本机 `grok.txt` 读取，也可由命令行或 `.env` 覆盖。URL 和凭证不应提交到 Git。
- `grok.txt` 是本机凭证文件，保持未跟踪且不要提交到 Git；`GROK_API_KEY`、`GROK_BASE_URL` 是 Grok API 搜索配置，和这个 Webhook 调用不是同一接口。

## 回应与结果闭环

- 当前端点实际返回 HTTP 200 JSON：`{"success":true,"runUuid":"..."}`。这只表示任务已收单，不表示调查完成，也不含结果 JSON。若上游返回 `202`，命令也将其视为收单。
- `--wait-for-result` 使用官方服务器的 HTTPS 回调路由。CLI 先登记本次 `request_id` 和 CA，服务器返回高熵一次性 token；CLI 将带 token 的 callback URL 发给 Grok Bot。Grok POST 结果后，CLI 轮询该 URL 并打印、保存结果。
- callback POST 必须匹配已登记的 request ID、CA 和 token；服务器仅保存结果 JSON 的 SHA-256 token 摘要和结果正文，不保存明文 token。回调请求体上限为 2 MiB；未完成登记保留 1 小时，完成结果保留 7 天。回调路由不使用浏览器 session，且不把含 token 的路径写入应用访问日志。
- 没有 `--wait-for-result` 或 `--callback-url` 时，Routine 仍只会把结果发到 Grok Bot Main Bot 聊天；操作者需在那里读取结果。
- `--callback-url` 可用于外部接收端；接收端须自行验证一次性 URL token 等凭证。
- 命令只发送一次请求，不自动重试，避免网络结果不确定时重复创建调查任务。
- Routine 返回的 `ok`、`primary_narrative`、`narratives`、`evidence`、`summary`、`caveats` 和失败结构由上游定义；本模块只按 `request_id`、CA 和 token 关联并传回结果，不改写叙事正文。

## 隐私与失败处理

- 不打印 Webhook URL、签名密钥或签名头。
- URL 必须使用 HTTPS；命令禁止自动跟随重定向，避免向新主机转发 Bearer 凭证。
- 非收单响应返回非零退出码且不打印上游响应体；请求超时或连接失败只报告异常类型。
- Webhook 收单回执不包含结果。等待超时返回退出码 2，并保留服务器上尚未完成的登记直至过期。
