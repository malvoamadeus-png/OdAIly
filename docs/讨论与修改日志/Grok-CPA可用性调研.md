# Grok CPA 可用性调研

## 调研时间

2026-09-25

## 结论

“Grok CPA”不是 xAI 官方产品名，通常是社区对 `CLIProxyAPI` 通过 Grok Build OAuth / `cli-chat-proxy.grok.com` 提供兼容接口的简称。

目前不能得出“Grok CPA 整体不能用”的结论。公开资料显示：

- CLIProxyAPI 官方仓库仍将 Grok Build OAuth 列为支持能力，并持续维护 xAI/Grok 相关代码。
- 官方仓库近期仍有针对 Grok OAuth、模型发现、订阅路由和 Responses 兼容性的更新或待合并修复。
- `HTTP 402: Grok Build usage balance exhausted` 被近期使用记录明确当作 Grok Build / SuperGrok 订阅额度墙，而不是 OAuth 登录失效。
- 本服务器的请求确实经过了 CLIProxyAPI 的 xAI OAuth 代理，并被上游返回相同的 402。因此当前最直接的结论是：代理可达、OAuth 路径被调用，但当前账号的 Grok Build 可用额度耗尽。

## 本次升级与重新登录复核

- 服务器上的 CLIProxyAPI 已从 `7.2.104` 升级到 `7.3.17`，服务正常运行并监听 `127.0.0.1:8318`。
- 通过 xAI device flow 重新登录成功，CLIProxyAPI 明确记录 `xAI authentication successful`，新的 OAuth 认证文件已写入认证目录。
- 重新登录后，代理 `/v1/models` 返回 HTTP 200，能够发现 `grok-4.7`、`grok-4.7-build-fast` 等 Grok 模型。
- 重新登录后发起最小 `/v1/responses` 请求，`grok-4.7` 仍返回 HTTP 402 `Grok Build usage balance exhausted`。

因此可以确认：账号确实重新登录成功，CLIProxyAPI 升级也已生效；当前阻断点仍是该账号的 Grok Build 上游额度，不是登录过期、代理未启动或模型不存在。

### 第二个账号的对照测试

- 通过第二次 xAI device flow 登录了一个用户指定的无会员账号，并确认 OAuth 认证成功、认证文件可被 CLIProxyAPI 加载。
- 只加载该账号的隔离代理实例中，普通 `grok-4.7` `/v1/responses` 请求返回 HTTP 200，说明无会员账号并非完全不能调用 Grok Build 普通响应。
- 但按项目现有 `x_search` 请求格式测试 `grok-4.5`、`grok-4.7-build-fast`，以及对 `grok-4.5` 强制 `tool_choice=required`，均没有真实工具调用：`num_sources_used=0`、`num_server_side_tools_used=0`，响应只是模型输出的伪工具调用文本。
- 该测试账号已从线上认证目录移出并保留在服务器 `/tmp` 临时备份中，线上只继续加载原账号，避免未通过 X Search 验收的账号参与轮询。

对照结论是：无会员账号可以跑普通 Grok 请求，但本次没有证明它可以跑真正的 X Search；热点追踪仍需要一个有可用额度且能产生真实 X Search 来源的账号。

## 原始来源

### 1. CLIProxyAPI 官方仓库

来源：[router-for-me/CLIProxyAPI README](https://github.com/router-for-me/CLIProxyAPI/blob/main/README.md)

README 的 Overview 明确列出：

- `Grok Build support via OAuth login`
- `Grok Build multi-account load balancing`
- Grok 兼容的 OpenAI/Responses API 接口

GitHub API 在 2026-09-25 显示该仓库仍为公开、未归档项目，最近仍有提交活动。最新 release 为 [v7.3.17](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v7.3.17)，发布时间为 2026-09-24。

这说明 CPA 项目本身仍在维护，不能因为一次上游 402 就判断整个方案被废弃。

### 2. 官方仓库关于 OAuth 流程的 issue

来源：[CLIProxyAPI issue #5172](https://github.com/router-for-me/CLIProxyAPI/issues/5172)

该 issue 曾报告“xAI OAuth 流程变化、Grok 不可用”。维护者在 2026-08-22 回复称：

- xAI OIDC discovery 当前可用；
- RFC 8628 device authorization 当前可用；
- CLIProxyAPI 仍支持 xAI OAuth device flow；
- 未发现需要关闭该能力的代码缺陷。

因此，公开记录不支持“Grok Build OAuth 已被 xAI 全面关闭”这一判断。

### 3. 官方仓库近期的 SuperGrok OAuth 实测记录

来源：[CLIProxyAPI PR #6020](https://github.com/router-for-me/CLIProxyAPI/pull/6020)

这是一个尚未合并的 PR，不能当作已发布版本的保证，但其验证说明记录了一次 SuperGrok OAuth 账号上的实时请求：

- 使用 `grok-4.7-build-fast`；
- 通过 Grok Build chat-proxy；
- 带 `reasoning_effort=xhigh`；
- 返回 HTTP 200。

这个记录至少证明在 2026-09-24，Grok Build OAuth 路径仍存在可工作的账号/额度组合。

### 4. 402 与订阅额度、API 余额的区别

来源：[CLIProxyAPI issue #5335](https://github.com/router-for-me/CLIProxyAPI/issues/5335)

该 issue 记录了一个 active SuperGrok Heavy 订阅账号：

- 通过开发者 API `https://api.x.ai/v1` 的媒体请求返回 `spending-limit`；
- 同一 OAuth 账号通过 `https://cli-chat-proxy.grok.com/v1` 使用订阅路径可以成功；
- 订阅额度和开发者 API 的 pay-as-you-go 余额是不同的额度体系。

这说明错误信息中的“余额/额度”不能简单理解为 OAuth token 失效，也不能把开发者 API 的余额状态等同于 SuperGrok 订阅状态。

### 5. 近期对同一错误文本的独立记录

来源：[fleet-ops PR #4386](https://github.com/Nishfleet/fleet-ops/pull/4386)

该记录把以下错误明确归类为 xAI OAuth 的 provider quota wall：

`HTTP 402: Grok Build usage balance exhausted`

其上下文把它描述为 SuperGrok weekly subscription balance drain，而不是 token 认证失败。该来源不是 CLIProxyAPI 官方仓库，因此只作为独立交叉印证，不作为唯一依据。

## 对本服务器结果的解释

服务器上的实际状态：

- `cliproxyapi-xai.service` 正常运行；
- `127.0.0.1:8318` 由 `cli-proxy-api` 监听；
- `/v1/models` 在正确加载 `.env` 后返回 HTTP 200；
- CLIProxyAPI 已升级为 `7.3.17`，构建时间为 2026-09-24；
- 重新 OAuth 登录后 `/v1/models` 返回 HTTP 200，模型列表包含 `grok-4.7`；
- 重新登录后请求 `/v1/responses` 使用 `grok-4.7`，仍被上游返回：`{"error":"Grok Build usage balance exhausted"}`；
- CLIProxyAPI 日志显示请求被转发到 `https://cli-chat-proxy.grok.com/v1`，上游响应 HTTP 402。

所以这次失败链路是：

```text
本地 CLI -> CLIProxyAPI -> cli-chat-proxy.grok.com -> HTTP 402 额度耗尽
```

不是：

```text
本地 CLI -> 找不到代理
本地 CLI -> OAuth token 一定失效
本地 CLI -> x_search 工具一定被移除
```

## 仍需注意的两个问题

1. CLIProxyAPI 升级和 OAuth 重新登录均已完成，但没有改变上游 402。后续不应继续把版本升级或反复登录当作主要修复方向。
2. `/v1/models` 能够返回模型，只能证明代理和模型发现正常；在当前账号额度恢复前，不能把模型列表成功误判为 Grok X Search 可用。

## 最终判断

当前应把 Grok CPA 标记为：

**项目和 OAuth 路径仍然存在；当前服务器代理可用；原账号的 Grok Build 上游额度不可用；第二个无会员账号可以进行普通响应，但 X Search 未实际执行。**

下一步应该先核对/恢复该 Grok 账号的 SuperGrok/Grok Build 额度或切换一个有可用额度的已授权账号，然后重新做同一条只读请求。只有在恢复额度后仍出现认证错误、模型不可用或 x_search 失败，才需要继续判断 CPA 版本或请求格式问题。
