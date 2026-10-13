# Hyperliquid 单资产历史接口核查

核查日期：2026-10-13（Asia/Shanghai）。只读取官方公开文档，不连接生产、不读取凭证。本记录回答历史接口能力，具体地址少计交易轮的原因以生产成交核验为准。

## 结论

官方公开 `info` 文档没有提供“地址 × ETH 的历史持仓轮列表 / 已平仓轮胜率”接口。它提供当前资产持仓、账户级盈利曲线、逐笔成交及历史订单；单资产完整交易轮需要从成交重建，或接入已经整理好这些轮次的数据源。不能因为第三方展示了 ETH 多空交易轮，就认定官方有同名接口，也不能在未核验第三方数据链路前认定其一定只用 `info` 重建。

**10,000 是 `userFillsByTime` 可取得的最近成交保留范围，2,000 是单次响应上限；不是一次能查 10,000。** 对交易频繁的地址，2,000 条可能只覆盖一天，10,000 条也可能不足以覆盖开仓起点。它是地址成交接口，文档没有 `coin` 筛选参数；按 ETH 筛选需在取得成交后处理，不能靠资产筛选绕过地址保留窗口。[来源：官方 Info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#retrieve-a-users-fills-by-time)。

## 已确认的接口边界

| 接口 | 官方提供的数据 | 历史与单资产边界 |
| --- | --- | --- |
| `userFills` | 最新最多 2,000 条成交，包含 `coin`、`time`、`startPosition`、`sz`、`side`、`closedPnl`、`fee` | 不直接返回完整交易轮、每轮开平时间或胜率；无资产筛选参数 |
| `userFillsByTime` | 按 `startTime` / `endTime` 查询成交，单响应最多 2,000 条 | 官方明确只有最近 10,000 条成交可用；时间分页不能取回已超出保留窗的成交；无资产筛选参数 |
| `historicalOrders` | 最新最多 2,000 条历史订单及状态 | 订单状态不是完整持仓轮；订单不等于成交，不能用它直接代替每轮净盈亏 |
| `clearinghouseState` | 当前 `assetPositions`，包含资产、持仓量、成本价、清算价、浮盈亏及累计资金费 | 请求没有历史时间参数；当前持仓不是历史持仓快照列表 |
| `portfolio` | `day/week/month/allTime` 及 `perp*` 的账户价值与 PnL 曲线 | 是账户 / 合约账户级曲线，没有单资产维度或每轮列表，不能替代 ETH 胜率 |

来源：[成交、时间成交、历史订单、portfolio 文档](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)；[当前永续账户摘要](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals#retrieve-users-perpetuals-account-summary)。

`aggregateByTime=true` 可合并一个主动订单与多个挂单成交产生的部分成交；被多个主动订单成交的挂单，仅在同一块内合并。它不是按完整持仓轮汇总。文档没有承诺它能扩展 10,000 条保留范围，因此不能把这个参数当成完整历史保证。[来源：成交参数说明](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#retrieve-a-users-fills)。

## 分页与限流

官方通用分页段落写：时间范围响应最多返回 500 个元素或不同的数据块，应把最后返回时间作为下一次 `startTime`。成交端点另明确单响应最多 2,000 条；实现应同时尊重端点上限和时间块截断，而非一律只依据条数判断完整。`startTime`、`endTime` 均为包含边界，分页需处理同时间戳重叠及成交去重；跨页出现非零初始持仓，不意味着这一轮已经从开仓被完整观察。[来源：Pagination 与 userFillsByTime](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)。

每 IP 每分钟总权重 1,200；`userFills`、`userFillsByTime` 基础权重 20，并按返回的每 20 个项目加权。补齐历史不是免费的高频无限翻页操作，应在测试挖掘和正式地址属性查询中控制请求预算。[来源：官方限流](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)。

## 超过 10,000 条的官方历史路径

“官方只能提供最近 10,000 条”仅适用于上述公开成交查询端点，不能扩展为“官方没有更老的成交数据”。官方 Historical data 页面另外列出：

- `s3://hl-mainnet-node-data/node_fills_by_block`：非验证节点按块输出的成交。
- 旧格式的 `node_fills` / `node_trades`；其中 `node_fills` 与 API 格式匹配，`node_trades` 不匹配。
- `misc_events_by_block`：转账、质押、资金费等非成交事件。

这些是批量归档，需要按块读取、过滤地址并建立自己的索引，不是直接查询某地址 ETH 已平仓轮的轻量接口。页面明确请求方承担数据传输成本；数据可用性、覆盖日期及访问成本仍需实际核验，不能以文档列出归档就宣称该地址已可完整补齐。[来源：官方 Historical data](https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data)。

同页还推荐由 Enigma 团队独立维护的 [额外交易历史导出](https://trade-export.hypedexer.com/?v=1)。这是官方文档介绍的第三方服务，不是官方 `info` 的资产历史端点。

## 对当前测试案例的处理建议

先把该地址已取得的 2,000 条样本扩展为 `userFillsByTime` 可取的最近成交，核对 ETH 两次清仓是否均有完整开仓起点；若取回缺失起点，应按真实轮次更新统计和统计区间。历史事件的胜率使用该事件动作发生前已经完成的轮次，不能混入之后的平仓。

用户要求单资产高胜率至少 5 个完整交易轮。该地址即使确认为两次完整平仓，也不再满足 5 轮门槛；不要为了填案例将其保留在高胜率方向。新闻标题应突出实际准入亮点，例如“ETH 交易胜率达 100% 的地址开立……”，并在固定数据中列实际轮数与统计时间。公开快讯不堆接口限制说明，管理后台保留历史覆盖证据供审核。


## 本地址生产核验

地址 `0xbd1c84113c6deb5044be0c2221f9e9403811ff0e` 的原测试缓存恰为 2,000 条，ETH 最早记录时间 `1791733361724`（北京时间 10 月 11 日 23:42:41），是持仓仍有 8,390.4891 ETH 的平多成交。第一轮多单的开仓被截断，不能作为完整轮；第二轮空单可完整重建，因此原统计为 1 轮。旧事件的末尾时间为动作前最后成交，并非页面刷新时间。

生产公开接口分页核验取得更早记录，最早时间 `1791473662449`（北京时间 10 月 8 日 23:34:22），连续路径可识别 2 个完整盈利轮，胜率 100%，无仓位断裂或同时间排序歧义。最早记录本身仍非零仓位，因此不声称这是该地址全部历史。两轮终止时间分别为 `1791733368711` 与 `1791815998368`。本系统扣费净结果与用户第三方展示的净结果未完全一致，未宣称已核验第三方费用/资金费算法。

按新的至少 5 轮要求，该地址不再属于单资产高胜率方向；大仓开仓动作可以独立保留。
