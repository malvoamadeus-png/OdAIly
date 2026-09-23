# 收集者-MSX

## 职责

`MSXNoticeWorker` 是 MSX 公告信源的独立收集者。它轮询 MSX 中文公告列表 API，发现新公告后调用详情 API 获取正文，再将完整公告写入主 SQLite 的 `tasks` 并提交现有 `local_pipeline`。

MSX 不复用非主流媒体收集者的 HTML 站点注册表，但复用同一主库、任务表、查重、判断、编写、格式化和发布链路。

## 接口与链接

- 列表：`POST https://api9528mystks.mystonks.org/api/v2/stat-msg/page`
- 详情：`POST https://api9528mystks.mystonks.org/api/v2/stat-msg/detail`
- 人工查看链接：`https://msx.com/zh-hans/notice-center-detail/{alias}`
- 列表固定使用 `classKey=system_msg`、`lang=zh`。

详情页是前端 SPA 外壳，程序必须通过详情 API 读取 `actualContent`，不能把详情页 HTML 当作正文。

## 轮询与去重

- 默认每 `600` 秒轮询一次，即 10 分钟。
- 首次运行只将当前列表写入 `msx_notice_seen_items` 作为历史基线，不创建任务、不补发旧公告。
- 稳定去重键为 `source_item_id = msx:{id}`，任务来源为 `source = msx`。
- 只有详情读取、任务写入和本地流水线入队均成功后才标记已见；详情失败会在后续轮询重试。
- 轮询失败、详情失败和入队失败写入 worker heartbeat，不影响其他来源。

## 任务字段

- `source_url`：MSX 详情链接。
- `title`：列表公告标题。
- `content`：详情 API HTML 转换后的正文。
- `published_at`：公告时间，转换为带时区的 UTC ISO 时间保存。
- `metadata.site_display_name`：`MSX`。
- `metadata.category`：MSX 公告类别。
- `metadata.links`：正文内结构化链接。
- `metadata.writer_template_key`：`msx_notice_writer`。
- `metadata.omit_site_attribution`：`true`，避免编写者2追加重复的 `（MSX）`。

## 命令

```bash
python backend/src/main.py msx-notice-worker --once
python backend/src/main.py msx-notice-worker
```

`msx-notices` 仍保留为只读 API 调试命令；生产持续收集使用 `msx-notice-worker` 和 `deploy/odaily-msx-notice.service`。
