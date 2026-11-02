# Robinhood-LIT 热点自动追踪误归因调研

## 结论

这不是热点正文把 `Robinhood` 或 `Lighter` 静态替换成了 `FOMO`，也不是参与账号目录映射错误。线上生产数据确认：

1. HotTopic 正文和参与账号属于正确的 `topic-6ba246b9ae311f07`，正文来源围绕 Robinhood、LIT、Lighter、Bitstamp、Hyperliquid 和 CFTC；参与账号是 `cryptokevin2011`、`cryptogle`、`OnchainLens`、`JonahBlake`、`econoar`、`machibigbrother`。
2. 控制台看到的“争议直接影响用户……”来自另一条链路：`event_tracking_events.rationale`，不是 HotTopic brief。
3. 这条 event 最初于 2026-09-28 06:07:49 UTC 由 FOMO/ZSHIB 主题创建，之后错误地吸收了 Bitget、Anthropic 等无关主题，最后又吸收 Robinhood/LIT 主题。事件标题被后来的 Robinhood 标题覆盖，但 FOMO 的事实、理由和官方回应假设没有同步覆盖。
4. 因此，官方账号发现阶段接收到的仍是 FOMO 事件上下文，搜索出并绑定 `@fomo`、`@onfomo` 是污染链条的后续结果，而不是 Robinhood/Lighter 被直接改名为 FOMO。

## 线上证据

核查范围：`odaily-official`、Linux 用户 `root`、`/opt/OdAIly`；只读查询生产本地 SQLite `/opt/OdAIly/data/runtime/hottopic.sqlite`。生产代码版本为 `f8bf519`，与本地工作区一致。

### HotTopic 本身正确

目标热点为 `topic-6ba246b9ae311f07`，其第 3 版 brief 标题为：

> Robinhood涉足永续合约，LIT暴跌14%后市场转向“谁会成为合作方”

该 brief 的来源绑定包括：

- `@OnchainLens`：LIT 下跌超过 14%，Robinhood 整合 Bitstamp；
- `@cryptogle`：询问 Robinhood 永续合约是否通过 `@Lighter_xyz`；
- `@JonahBlake`：认为 Robinhood 想自行建设更多基础设施；
- `@econoar`：讨论 CFTC 许可与 LIT 价格预期；
- `@machibigbrother`：讨论 Hyperliquid、Lighter、非排他合作和 CFTC；
- `@PANewsCN`：LIT 价格和清算数据，作为上下文来源。

该主题的 `topic_participations` 只有 6 个参与账号，未包含 `fomo` 或 `onfomo`；第 3 版 brief 正文也没有 FOMO。

### 自动追踪 event 已被污染

同一个 `event_id` 为 `tracking-event:1e281dbc8b7c7cee4fe62b8c`，当前展示标题是 Robinhood 标题，但其持久化字段仍是：

- `confirmed_facts_json`：FOMO 空投 ZSHIB、FOMO 排行榜 PnL、独立重建工具等；
- `unconfirmed_claims_json`：KOL 提前建仓、排行榜 PnL 夸大、FOMO 用户规模和手续费等；
- `rationale`：包含“FOMO平台及相关KOL可能需要公开解释排行榜口径、已实现收益和返佣披露”；
- `official_response_hypothesis_json`：实体为“FOMO平台运营方”和“被质疑的FOMO排行榜账号和KOL”。

该 event 的关联时间线如下：

| 时间（UTC） | 被关联的主题 |
|---|---|
| 09-28 06:07:49 | `topic-319832eaffb951e5`：`$ZSHIB` 空投给 FOMO 用户，FOMO 排行榜争议 |
| 09-28 13:32:49 | Bitget 安全事件 |
| 09-29 05:46:20 | Anthropic 未来 27 年基础设施投入 |
| 09-30 06:36:21 | Robinhood 永续合约与 LIT/Lighter |

该 event 一共已经关联 28 个主题。FOMO 主题的原始 brief 是“FOMO向用户空投1.02亿枚$ZSHIB，平台高费率与收益真实性引发质疑”。生产审计中的 `event_created` 也明确记录了它最初来自 `topic-319832eaffb951e5`。

官方账号发现的三次结果分别围绕 FOMO：

- 09-28：搜索 `FOMO crypto official...`，绑定 `@fomo`；
- 09-29：再次绑定 `@fomo`；
- 09-30 06:16：搜索 `FOMO ZSHIB ... leaderboard PnL`，绑定 `@onfomo`。

最后一次 FOMO 账号发现发生在 Robinhood 主题被关联之前约 20 分钟，但事件标题随后被 Robinhood 主题覆盖，造成了用户看到的“Robinhood 标题 + FOMO 分析/账号”组合。

## 代码链路

### 1. 两个界面字段来自不同数据源

HotTopic 快照从 `brief_revisions` 取标题和正文，同时从 `topic_participations` 取参与账号，见 [`event_tracking.py:918`](/mnt/d/Coding/OdAIly/backend/packages/hottopic/event_tracking.py:918) 和 [`event_tracking.py:946`](/mnt/d/Coding/OdAIly/backend/packages/hottopic/event_tracking.py:946)。

自动快讯详情则展示 event 的 `rationale`、事实和官方账号绑定。事件发现 Prompt 又把 `rationale`、回应假设及关联主题快照作为输入，见 [`event_tracking.py:1289`](/mnt/d/Coding/OdAIly/backend/packages/hottopic/event_tracking.py:1289)。因此，FOMO 一旦进入 event 主记录，就会继续影响账号发现。

### 2. 事件归并条件过宽

`_find_event()` 先按 topic link 和完整 identity key 查找；查找不到时，把当前事件身份、核心实体、原始 `canonical_subject` 和标题全部拆成 token，见 [`event_tracking.py:1223`](/mnt/d/Coding/OdAIly/backend/packages/hottopic/event_tracking.py:1223)。

随后只要满足以下任一条件，就复用已有 event：

- 共享任一 `$cashtag` 或合约地址；
- 共享任意两个 token；
- 只共享一个不在 `GENERIC_IDENTITY_TOKENS` 中的 token，见 [`event_tracking.py:1232`](/mnt/d/Coding/OdAIly/backend/packages/hottopic/event_tracking.py:1232)。

Robinhood 主题的原始 `canonical_subject` 是英文帖子：

> Some many catalysts **for** $LIT, so many catalysts **for** robinhood:0x4203... 引用 @ligerdotmem

与最初 FOMO event 的 77 个身份 token 做只读回放时，唯一命中的 token 是普通英文词 `for`。`for` 不在通用 token 黑名单中，所以该主题被错误并入 FOMO event。此前的 Bitget、Anthropic 等主题也通过类似的低约束 token 交叉命中被并入，形成 28 个主题的大杂 event。

### 3. 复用 event 时只更新标题，不更新语义上下文

新主题命中已有 event 后，代码只更新 `updated_at`、`last_hot_topic_at` 和 `title`，见 [`event_tracking.py:1183`](/mnt/d/Coding/OdAIly/backend/packages/hottopic/event_tracking.py:1183)。`rationale`、事实、未确认说法和 `official_response_hypothesis` 均保留旧值。

所以线上最终状态必然可能出现：

```text
event.title                         = Robinhood/LIT
event.rationale                     = FOMO 排行榜/KOL/返佣争议
event.official_response_hypothesis = FOMO 官方账号
```

### 4. 模型自由文本会放大污染

任务 1 的 `reason` 是模型自由生成文本，解析时没有验证其中的实体必须来自当前热点证据；保存 event 时直接写入 `rationale`，见 [`event_tracking.py:1164`](/mnt/d/Coding/OdAIly/backend/packages/hottopic/event_tracking.py:1164)。官方账号发现阶段又把这个字段送回模型，并且只检查官方关系、主页 URL 和引用，没有对候选账号与当前事件实体做交叉验证，见 [`event_tracking.py:140`](/mnt/d/Coding/OdAIly/backend/packages/hottopic/event_tracking.py:140) 和 [`event_tracking.py:1440`](/mnt/d/Coding/OdAIly/backend/packages/hottopic/event_tracking.py:1440)。

这是一条可级联的污染链：

```text
错误归并或模型自由生成 FOMO
  -> event.rationale / event facts 持久化为 FOMO
  -> Web Search Prompt 继续携带 FOMO 上下文
  -> @fomo / @onfomo 通过官方关系校验
  -> 控制台展示 Robinhood 标题 + FOMO 理由/账号
```

## 判断

- **不是** Robinhood、Lighter 或参与账号被替换成 FOMO。
- **不是**普通 `hottopic_accounts` 中的 FOMO 账号被错误当成官方追踪账号。事件账号使用独立的 `event_tracking_*` 表。
- **是**自动追踪的 event 归并键过宽，旧 event 语义字段不可变更新，且下游账号发现没有实体一致性校验。
- 这条生产 event 应视为错误归因记录；它的 Robinhood 标题不能证明 FOMO 与 Robinhood/Lighter 存在业务关系。

## 未实施的修复方向

本次只做调研，没有修改代码或生产数据。后续修复至少应分别处理：

1. 收紧事件归并：普通英文停用词不能作为唯一身份锚点；单 token 合并应要求强实体、明确 cashtag/合约或经过事件级证据确认。
2. 复用 event 时重新校验并更新事件语义，禁止只覆盖标题而保留旧 rationale/facts/官方回应主体。
3. 在任务 1 和官方账号发现之间增加实体一致性校验，拒绝只在旧 rationale 中出现、却不在当前热点证据中的主体。
4. 为 event 语义字段保留不可变版本或关联快照，避免多个无关 HotTopic 共用一条可变主记录。
5. 增加“标题、rationale、官方账号、来源证据”跨字段一致性监控，发现跨事件主体混合时自动阻断展示或追踪。

## 相关一手来源

- 生产 HotTopic source bindings 中的原帖：
  - https://x.com/machibigbrother/status/2105128328961404951
  - https://x.com/econoar/status/2105097964050166044
  - https://x.com/JonahBlake/status/2105096382264836220
  - https://x.com/OnchainLens/status/2105081387099578420
  - https://x.com/cryptogle/status/2105072955470573821
  - https://x.com/cryptokevin2011/status/2104850703269367992
- 本地代码：[`event_tracking.py`](/mnt/d/Coding/OdAIly/backend/packages/hottopic/event_tracking.py)
- 自动追踪设计契约：[`热点话题长期追踪升级计划书.md`](/mnt/d/Coding/OdAIly/docs/讨论与修改日志/热点话题长期追踪升级计划书.md)
- 生产只读数据库：`/opt/OdAIly/data/runtime/hottopic.sqlite`
