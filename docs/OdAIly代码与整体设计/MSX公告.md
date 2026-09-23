# MSX公告

## 模块职责

`msx-notices` 是一个只读、可重复执行的 MSX 中文公告获取命令。生产收集由 `msx-notice-worker` 完成：它写入主 SQLite 的 `tasks` 并提交现有本地流水线，不直接调用 Push Data API。

## 官方接口

- 公告列表：`POST https://api9528mystks.mystonks.org/api/v2/stat-msg/page`
- 公告详情：`POST https://api9528mystks.mystonks.org/api/v2/stat-msg/detail`
- 列表固定使用 `classKey = system_msg`、`lang = zh`；可选 `subType` 与分页参数。
- 请求使用公开网页同样的 `my-stonks-lang: zh`、`source: web` 请求头，不需要登录态。
- 列表返回 `id`、`alias`、`actualTitle`、`subTypeName`、毫秒时间戳 `ctime`；详情返回 `actualContent` HTML。
- 展示详情链接为 `https://msx.com/zh-hans/notice-center-detail/{alias}`。

## 命令

```bash
python backend/src/main.py msx-notices --json
python backend/src/main.py msx-notices --page-size 10 --list-only --json
python backend/src/main.py msx-notices --sub-type 1 --json
python backend/src/main.py msx-notice-worker --once
```

默认抓取第一页并补齐每条详情正文。`--list-only` 只请求列表，适合快速轮询；`--json` 输出 `total`、分页信息和 `notices` 数组。正文同时保留 `content_html`、去除标签后的 `content` 和正文内所有 `links`，时间统一转换为带 `+08:00` 的 ISO 8601 字符串。

详情页 URL 本身返回前端 SPA 外壳；自动化程序应使用列表返回的 `id` 调用详情 API，再把 `alias` 生成的 `detail_url` 作为人工查看链接。

持续收集规则见 `收集者-MSX.md`：默认每 10 分钟轮询，首次运行只建立基线，后续新公告才进入 `source=msx` 的常规快讯流水线。

## 失败处理

- 网络错误、非 2xx 响应和无效 JSON 最多重试 3 次，最终抛出命令错误并返回非零退出码。
- API 返回非零 `code`、缺少 `data.list`、公告缺少标题/别名或时间无效时视为协议错误，不产生部分成功结果。
- 详情按列表顺序逐条获取；任一详情失败会使本次命令失败，不写入任何业务状态。
