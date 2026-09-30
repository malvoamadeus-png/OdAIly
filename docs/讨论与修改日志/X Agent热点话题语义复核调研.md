# X Agent 热点话题语义复核调研

## 调研范围

本次调研于 2026-09-30 只读检查官方服务器 `odaily-official:/opt/OdAIly` 的生产 SQLite：
`data/runtime/hottopic.sqlite`。生产代码 revision 为 `59e1cfa`。未修改服务器文件、数据库或服务配置。

## 生产样本

样本话题：`topic-934a4d5537612336`。

- 话题标题：`Aave扩展抵押品版图，代币经济学3.0或加入销毁机制`
- `topics.participant_count_24h`：18
- `topic_participations`：64 个独立账号、64 条参与记录
- 话题 memberships：330 条去重 claim
- 最新正文的 `source_claim_ids` 只覆盖 4 个账号的 4 条 claim
- 因此详情页展示的 64 个账号中，有 60 个没有进入最新正文证据，占比约 93.8%

上述事实来自生产数据库中的 `topics`、`topic_participations`、`memberships`、`claims`、`content_items` 和最新 `brief_revisions` 查询。生产正文仍然较准确，是因为正文生成前的证据选择器只把少量高信息价值材料交给模型；它不能证明 `topic_participations` 中的账号都与话题主线相关。

## 污染来源

生产 memberships 的主要归并原因包括：

- 82 条：`same primary contract=['0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9']`
- 67 条：`repeated subordinate event asset=['gpt']`
- 29 条：`repeated subordinate event asset=['nvidia']`
- 21 条：`repeated subordinate event asset=['cpu']`
- 19 条：`primary asset prose alias=['gpu']`
- 12 条：`repeated subordinate event asset=['aave']`

这说明当前机械归并把“同一个资产/合约出现在文本或引用链中”当成了较强的同一话题证据。对于 Aave 样本，GPU、NVIDIA、CPU、GPT 等周边内容沿着“抵押品、算力、AI 基础设施”链路被并入，但它们并不一定讨论 Aave 的抵押品扩展或 Aavenomics 3.0。

## 结论

不应直接删除原有机械聚合。它仍承担低成本、高召回的候选发现。应在候选 claim 已进入 topic、但在计入参与账号/热度和生成读者正文之前增加 claim 级语义复核。

复核结果建议固定为三类：

- `support`：讨论同一事件、公告、产品进展或明确围绕主线的市场反应；计入参与账号、热度和正文证据。
- `context`：与主线相关的背景材料，但不是该事件的独立参与；不计入参与账号，可在正文需要时作为背景。
- `unrelated`：仅共享资产、合约、引用链或泛化实体，实际讨论其他事件；不计入参与账号和正文，只保留审计记录。

账号必须按 claim 处理。同一个账号可以有一条 `support` 和多条 `unrelated`，不能因为一条污染内容删除整个账号。

## AI 调用建议

初步建议使用 `gpt-5.6-luna`、`reasoning_effort=high`，但只审查以下材料：topic 当前标题/主线、少量已确认的代表性证据、一个待裁决 claim 及其原帖文本。不要每轮把整个 topic 的全部原帖发送给模型。

复核应按 `(topic_id, claim_id, input_hash)` 缓存，并优先触发于：话题首次达到可见门槛、候选依赖泛化实体或子资产、以及话题出现新的实质证据时。模型失败时保留机械结果和待复核状态，不把失败误判为 `unrelated`。

## 代码依据

- `backend/packages/hottopic/topic_aggregator.py` 的 `_retrieve_candidates` 和 `_decide` 使用硬关联、实体重叠、词汇相似度和时间接近度做候选归并。
- `_attach_claim` 会把每个被挂入 topic 的 `activity_account` 写入 `topic_participations`。
- `backend/packages/hottopic/service.py` 的 `topic_detail` 直接读取全部 `topic_participations` 展示账号，而最新正文使用 `brief_revisions.source_claim_ids_json` 绑定的少量 claim。
