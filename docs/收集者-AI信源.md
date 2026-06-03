# 收集者-AI信源

## 说明

AI信源是独立业务来源，底层暂时复用 `non_mainstream_media` 实现包、worker、配置表和控制台页面。

区分口径：

- `non_mainstream_media_sources.source_group = 'ai_source'`
- `write_flow` 入库为 `tasks.source = 'ai_source'`
- `alert_only` 入库为 `tasks.source = 'ai_source_alert'`
- 入库 metadata 固定写入 `source_group = 'ai_source'`、`source_label = 'AI信源'`

## 当前站点

### `write_flow`

- `thelec_china`
- `etnews_electronics`
- `etnews_sw`

这些站点抓标题、来源链接、正文、作者、栏目和发布时间，并进入搜索查重、判断者、编写者1、编写者2、发布者链路。

### `alert_only`

当前没有生产站点。链路合同已保留：

- `tasks.source = 'ai_source_alert'`
- 进入 `external_media_alert(domain_judge -> search -> notify)`

## 抓取频率

AI信源默认使用站点级频率 `interval_seconds = 300`，即 5 分钟抓取一次。worker 调度优先使用站点级频率，没有配置时才回退到全局频率。

## TheElec CHINA

- 入口：`https://www.thelec.net/news/articleList.html?sc_section_code=S1N2&view_type=sm`
- 列表页解析站内 `articleView.html?idxno=...` 链接
- 正文页解析标题、作者、栏目、发布时间和正文
- 直连失败时会尝试 `thelec.net` 与 `r.jina.ai` fallback

## ETNews

- 电子栏目：`https://www.etnews.com/news/section.html?id1=06`
- SW 栏目：`https://www.etnews.com/news/section.html?id1=04`
- 列表页解析 `/YYYYMMDDNNNNN` 形式文章链接
- 正文页优先读取：
  - `link[rel='canonical']`
  - `meta[property='og:title']`
  - `meta[property='article:published_time']`
  - `#articleBody.article_body`
  - `.reporter_info`

## 判断者契约

AI信源全文在 `x_processing` 的 `judge` 阶段不调用判断模型，不按内容丢弃。进入判断者后直接写入：

- `x_task_pipeline.news_type = 'ai_source'`
- `tasks.status = 'deduped'`

也就是说，无论是否 Crypto、是否 AI 泛科技内容，只要前面搜索阶段没有判重，都会进入后续写作链路。

## 相关文档

- `完整程序架构.md`
- `判断者.md`
- `搜索者.md`
- `编写者1.md`
- `编写者2.md`
- `控制台-非主流媒体抓取.md`
