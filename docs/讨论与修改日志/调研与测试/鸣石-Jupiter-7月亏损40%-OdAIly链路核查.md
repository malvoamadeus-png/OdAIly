# “鸣石关联量化机构 Jupiter 旗下基金 7 月亏损超 40%”链路核查

核查日期：2026-08-07

## 结论

在当前本地工作区可读取的 OdAIly 链路留存中，没有找到标题或正文包含以下关键词组合的记录：`鸣石`、`Jupiter 旗下基金`、`7 月亏损超 40%`、`量化机构`。

因此，目前不能证明这条消息是 OdAIly 的收集者-竞品抓取后生成并发布的稿件。

本地流水线明细中确实有两条 Jupiter 相关竞品任务，但内容是 JUP 持仓/增持，不是该基金亏损消息：

- 2026-07-04 14:30（北京时间），`jinse`，标题：`Jupiter战略储备信托基金累计购买1.45亿枚JUP，价值约3480万美元`，状态为 `ready_review`，发布决策为 `manual_review`。
- 2026-07-07 14:59（北京时间），`jinse`，标题：`持仓达1.46亿枚JUP，Jupiter Litterbox Trust昨日增持25.58万枚JUP`，状态为 `ready_review`，发布决策为 `manual_review`。

这两条只能说明竞品链路抓到过 Jupiter 这个词，不能作为目标消息来源或复制关系的证据。

## 已核查数据

### 竞品信源明细

文件：`data/exports/source_audit/竞品快讯信源明细-过去60天.csv`

- 记录范围：2026-05-02 05:26:09 至 2026-07-01 05:03:00（UTC）。
- 字段包含竞品站点、来源 item id、竞品页、外部原文 URL、来源类型、原文站点和标题。
- 对 `jupiter`、`鸣石`、`量化机构`、`亏损超40%`、`亏损超过40%` 等关键词检索，没有命中目标消息。

### 本地流水线明细

文件：`data/exports/pipeline_timing_detail_20260714_184653.csv`

- 处理范围：2026-07-04 13:38:41 至 2026-07-14 18:40:14（北京时间字段）。
- 对 1,720 条流水线记录的标题检索，Jupiter 仅命中上面两条 JUP 持仓/增持任务。
- 没有命中 `鸣石`、`亏损超40` 或目标标题。

### 去重审计

目录：`data/exports/dedup_audit_24h/`

- 审计结果中没有目标消息标题或正文。
- 该目录里出现的“旗下基金”是另一条 `SHAZ / Leopold Aschenbrenner` 持股消息，不是 Jupiter 基金亏损消息，不能混同。

### 本地搜索缓存和运行库

- `data/processed/searcher/searcher.sqlite` 只有 165 条文档缓存，未命中 `jupiter` 或 `鸣石`。
- 本地工作区没有生产主库 `data/database/odaily.sqlite`，也没有 `data/processed/competitor_monitor/competitor_monitor.sqlite`。
- 生产服务器只读 SSH 连接在本次核查中超时，因此没有完成生产主库截至 2026-08-07 的实时查询。

## 对“是不是 OdAIly 生成”的判断

当前证据支持的判断是：**本地已留存的 OdAIly 竞品抓取与流水线记录中未发现这条消息；无法据此认定它由 OdAIly 生成或发布。**

“本地未发现”不等于“生产绝对没有”，因为生产主库和竞品运行库不在本地工作区，且实时连接失败。要做最终确认，需要查询生产 SQLite 的 `tasks`、`x_task_pipeline`、`newsflash_items` 和 `odaily_reference_items`，按标题、正文、来源 item id 与 URL 搜索。

## 对“是不是复制别家”的判断

在 OdAIly 本地保留的竞品来源明细中，没有找到这条消息的对应源条目或外部原文 URL，所以目前**没有 OdAIly 内部证据证明它复制了某一家竞品**。

若生产库查到该条，应继续比较：

1. `tasks.source` 和 `source_item_id`，确认是 `blockbeats`、`panews` 还是 `jinse`。
2. `tasks.raw_payload` / `newsflash_items.raw_payload` 中的原始标题、正文、竞品页和外链。
3. `x_task_pipeline.writer_output`、`draft_title`、`final_title`、`final_content`，确认是原文转述、模型改写还是人工定稿。
4. 同一事件的 `newsflash_event_sources`，查看 OdAIly、竞品和外部原文是否被聚合为同一事件。

## 链路依据

- `docs/OdAIly代码与整体设计/收集者-竞品.md`：竞品快讯进入 `tasks` 后提交 `write_flow`，主流程为 `search -> judge_crypto -> write -> format_publish -> publish`。
- `docs/OdAIly代码与整体设计/发布者.md`：发布者只负责最终稿件的发布决策，不负责改写内容。
