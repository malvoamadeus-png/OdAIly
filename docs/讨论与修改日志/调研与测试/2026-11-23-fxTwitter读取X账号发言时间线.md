# fxTwitter 读取 X 账号发言时间线

核查日期：2026-11-23（Asia/Shanghai）。对公开账号 `@168X_Fortune` 只读请求 fxTwitter 官方公开 API 与 FixupX feed；未使用账号凭证。

## 结论

可以。fxTwitter 的 API v2 有账号状态列表接口：`GET https://api.fxtwitter.com/2/profile/{handle}/statuses`。对 `168X_Fortune` 的实测返回了公开时间线记录、作者、正文、发布时间和上下页 cursor；将上一页的 `cursor.bottom` 作为下一页的 `cursor` 参数，能够继续读取更早页面。它适合读取近期公开时间线，也支持用 `since` 做增量检查。[官方 API Overview](https://docs.fxembed.com/api/introduction)；[官方 OpenAPI 规格](https://api.fxtwitter.com/2/openapi.json)。

这不等于已经证明能完整导出账号全部历史。资料接口当时报告账号有 850 条 statuses；本次只验证了 12 页，最旧记录到 2026-10-11，没有继续遍历全部历史。实施时应保留 cursor、逐条去重并记录采集边界，不能把已读页数或 profile 计数当作完整性证明。

## 接口与字段

官方 OpenAPI 将 `/2/profile/{handle}/statuses` 定义为 `List user statuses`：`count` 范围 1–100、默认 20；`cursor` 用于翻页；`since` 接受 Unix 秒或毫秒；`with_replies` 可切换包含回复的上游时间线。`since` 不会严格筛出增量记录：无 cursor 且当前页没有晚于该时间的帖子时返回 204，否则返回常规时间线页，因此客户端仍需按时间戳过滤并按 ID 去重。官方通用说明规定列表响应包含 `cursor.top` 和 `cursor.bottom`，下一页传 `cursor.bottom`。[官方 OpenAPI 规格](https://api.fxtwitter.com/2/openapi.json)；[分页说明](https://docs.fxembed.com/api/introduction#pagination)。

单条记录使用 `created_at`（UTC 字符串）和 `created_timestamp`（Unix 秒）表示时间，作者在 `author.screen_name`，正文在 `text`。响应也会列出账号转推的他人原帖；`reposted_by.screen_name` 可识别转推账号，引用内容在 `quote` 字段。只统计该账号本人发布的帖子时，筛选 `author.screen_name == "168X_Fortune"`；统计完整账号活动时，应保留并区分这些转推、引用。请求可不带登录凭证；该接口是 FxEmbed 文档所述的公开 FxTwitter API。[状态对象定义](https://api.fxtwitter.com/2/openapi.json)。

## 实测

- `GET /2/profile/168X_Fortune/statuses?count=100` 于核查时返回 HTTP 200，首屏 19 条并提供 `cursor.bottom`；虽然规格允许请求最多 100 条，实际页数由上游响应决定。
- 沿 bottom cursor 连续请求 12 页，返回 19–21 条/页，覆盖最新记录至 `Sun Oct 11 09:26:08 +0000 2026`。跨页观察到重复 status ID，需按 ID 去重。
- 最新一条记录的 `created_at` 为 `Mon Nov 23 00:08:31 +0000 2026`，`created_timestamp` 为 `1795392511`；时间字段为 UTC。
- `with_replies=true` 的首屏中，作者不止 `168X_Fortune`，还出现 `billqian_hk`、`ViewsOfChris`；这些他人原帖的 `reposted_by.screen_name` 为 `168X_Fortune`。因此首屏记录不能全部算作该账号本人原创，应按 `author`、`reposted_by` 和 `quote` 区分。
- `https://fixupx.com/168X_Fortune/feed.xml` 也能读到 RSS，但实测为滚动 90 条；同一时点 Atom feed 只有 18 条。因此自动采集优先使用 API v2 的 cursor 分页，而不是把 feed 当全量时间线。[FixupX 账号页](https://fixupx.com/168X_Fortune)（页面声明 RSS/Atom alternate links）。

## 来源

- [FxEmbed/FxEmbed 官方仓库 README](https://github.com/FxEmbed/FxEmbed)：确认 FxTwitter 属于 FxEmbed 项目，并链接官方 API 文档。
- [API Overview](https://docs.fxembed.com/api/introduction)：公开 FxTwitter API、v2 路径、列表 cursor 分页、限流及 OpenAPI 入口。
- [运行时 OpenAPI 3.0 规格](https://api.fxtwitter.com/2/openapi.json)：`/2/profile/{handle}/statuses` 的参数、响应和状态对象字段。
- [本账号资料 API](https://api.fxtwitter.com/2/profile/168X_Fortune) 与[本账号状态列表 API](https://api.fxtwitter.com/2/profile/168X_Fortune/statuses)：核查时的实时响应；状态列表 cursor 可直接复用以验证后续页。
