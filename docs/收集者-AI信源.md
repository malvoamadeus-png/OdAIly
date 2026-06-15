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
- `zdnet_korea_semiconductor`
- `ctee_semiconductor`

这些站点抓标题、来源链接、正文、作者、栏目和发布时间，并进入搜索查重、判断者-AI、编写者1、编写者2、发布者链路。

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
- 列表页直连失败时会尝试 `thelec.net` 与 `r.jina.ai` fallback
- 正文页直连失败时会退到 `r.jina.ai` 返回的 Markdown 正文，并继续提取标题、副标题、作者、栏目、发布时间和正文段落

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

## ZDNet Korea Semiconductor

- 入口：`https://zdnet.co.kr/newskey/?lstcode=%EB%B0%98%EB%8F%84%EC%B2%B4`
- 列表页解析 `/view/?no=...` 形式文章链接
- 列表正文块优先读取 `.newsPost .assetText`
- 发布时间兼容 `2026.06.04 AM 09:44` 这种列表格式
- 正文页优先读取：
  - `link[rel='canonical']`
  - `meta[property='og:title']`
  - `meta[property='article:published_time']`
  - `meta[property='dd:author']`
  - `meta[property='article:section']`
  - `#articleBody > div[id^='content-']`
- 正文清洗时会跳过 `관련기사`、广告块和记者附加信息

## CTEE Semiconductor

- 入口：`https://www.ctee.com.tw/industry/semi`
- 站点固定使用 `www.ctee.com.tw`；裸域 `ctee.com.tw` 当前会返回 Cloudflare `403`
- 列表页优先解析 `.newslist__card` 主列表，忽略右侧“编辑精选 / 热门新闻”等侧栏块
- 列表页直连失败时会退到 `r.jina.ai` 代理后的 Markdown 列表
- 正文页优先读取：
  - `link[rel='canonical']`
  - `meta[property='og:title']`
  - `meta[property='article:published_time']`
  - `script[type='application/ld+json']` 中的 `author`、`datePublished`、`articleBody`、`keywords`
  - `.content__header`
  - `.content__body article`
- 当前栏目固定接入半导体页，正文分类默认写入 `半導體`

## 判断者契约

AI信源全文在 `x_processing` 的 `judge_ai` 阶段调用判断模型，不再确定性直通。判断者-AI 只保留明确重要的 AI/半导体产业新闻。

保留后写入：

- `x_task_pipeline.news_type = 'ai_source'`
- `tasks.status = 'deduped'`

丢弃后写入：

- `tasks.status = 'discarded'`
- `discard_type = 'non_crypto_ai'`

判断细则以 `docs/判断者.md` 为准。

## 相关文档

- `完整程序架构.md`
- `判断者.md`
- `搜索者.md`
- `编写者1.md`
- `编写者2.md`
- `控制台-非主流媒体抓取.md`
