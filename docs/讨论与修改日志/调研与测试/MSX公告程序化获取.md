# MSX公告程序化获取验证

验证日期：2026-09-22

## 结论

MSX公告可以通过公开 JSON API 稳定获取，不需要登录态，也不需要依赖浏览器渲染公告列表。站点页面本身是 SPA，页面脚本将 `/api/...` 请求改写到 `https://api9528mystks.mystonks.org`。

## 一手来源证据

- `https://msx.com/robots.txt` 允许抓取，并声明 `https://msx.com/sitemap-index.xml`。
- `https://msx.com/` 返回前端入口 `/assets/index-CAQoLVht.js`。
- 该前端脚本定义列表请求 `POST /api/v2/stat-msg/page`，详情请求 `POST /api/v2/stat-msg/detail`，并使用 `https://api9528mystks.mystonks.org` 作为 API 主机。
- 前端公告列表以 `classKey = system_msg`、`lang = zh`、`pageIndex`、`pageSize` 请求；详情以 `id`、`classKey = system_msg`、`lang = zh` 请求。

## 实际响应

2026-09-22 通过列表接口获得 `count = 94`。第一页实际返回了：

- `MSX社区双月福利季丨活动调整通知`，`id = 118`，`ctime = 1790057700000`，对应北京时间 `2026-09-22 14:15:00`。
- `MSX 上新公告｜$SAIL（SailPoint）`，`id = 115`，对应北京时间 `2026-09-21 15:43:00`。
- `MSX 上新公告｜$VRNS（Varonis Systems）`，`id = 114`，对应北京时间 `2026-09-21 15:43:00`。

详情接口返回 `actualContent` HTML；`id = 118` 的详情可以解析出活动调整正文及 Web / APP 链接。

## 仓库实现

- 客户端：`backend/packages/msx_notice.py`
- 命令：`python backend/src/main.py msx-notices --json`
- 测试：`backend/test_msx_notice.py`

当前实现保持只读，不接入主任务流水线。若后续要自动生成或发布快讯，需要另行确定 source、去重键、正文清洗、审核和发布时间口径。
