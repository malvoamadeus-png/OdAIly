# Hyperliquid 地址赋名：数据能力核查

日期：2026-11-17。范围：只读本地代码、模块文档及官方公开文档；没有连接生产服务器、读取数据库或修改程序。本记录说明代码具备什么能力，不代表生产样本已经满足每一种命名条件。

## 结论

现有底层足以支持“单资产高胜率”“某资产高盈利”“当前某资产重仓多头/空头”等事实型称谓，也有条件进一步分析“BTC 交易盈利突出、其他资产交易亏损”。更复杂的“波段交易”“长期偏空”“擅长某类资产”需要新增跨轮统计和时间稳定性检验；“最大多头”“最大亏损多头”没有现成的全市场比较依据。AI 适合从可靠的多维证据中选出最有辨识度的特征并压缩成称谓，不能替代统计、补齐缺失历史或自行扩大比较范围。

## 已具备的数据与边界

| 维度 | 当前实现 | 可以支持什么、还缺什么 |
| --- | --- | --- |
| 单资产胜率 | 按同一合约完整持仓轮计算胜负、轮数，排除缺失开头、数量断裂和无法还原顺序的轮；正式高胜率门槛为至少 5 轮、70% | 可写近期某资产高胜率；5 轮只是现有新闻准入，不能直接当作“长期稳定”“超高胜率”的命名证据。需要另定称谓强度、样本量和有效期。 |
| 单资产收益 | `assets[coin].closed_pnl_less_fees`；动作前另算近 30 天收益和实际首末成交时间 | 可比较同一窗口、同一口径的各资产交易盈亏。该数为所观察合约成交的 `closedPnl-fee`，不含资金费；不是完整历史资产收益。 |
| 账户收益 | 官方 Portfolio `perpDay/perpWeek/perpMonth/perpAllTime.pnlHistory`；另有观察成交的账户加总收益 | 官方合约账户收益包含未实现盈亏，与成交自算收益必须分开。不能把有限 BTC 成交盈亏除以官方累计账户盈亏，宣称“账户利润主要来自 BTC”。 |
| 当前持仓 | 合约、方向、数量、价值、估值价格、浮盈亏、清算价；逐 DEX 查询，失败保留旧基线 | 可表达当前重仓某资产、当前大额空单。跨资产仓位占比可以新增计算，但应明确分母是观察到的合约仓位价值，不是全部资产或净资产。 |
| 完整轮行为 | 紧凑历史 episode 保存方向、起止时间、开平数量/金额、手续费、已实现盈亏和连续性 | 可新增持仓时长分布、多空分层盈亏、反复交易同一资产等统计。现有写作材料并未直接提供这些画像。 |
| 变化与风险 | 现有检测保存前后仓位、实际成交动作、可比轮次浮盈峰值及回吐等 | 可写一次具体加仓、反手、回吐；要概括“经常浮亏加仓”“反复回吐”，还需跨事件统计和足够重复次数，不能据一次行为命名。 |
| 排名 | 官方收益榜用于有界发现；当前代码没有覆盖全部地址的逐资产多空持仓榜 | 已观察地址内第一不能改写为“Hyperliquid 最大多头”，更不能写“链上最大多头”。 |

依据：[history.py](../../../backend/packages/hyperliquid_news/history.py:60)、[detector.py](../../../backend/packages/hyperliquid_news/detector.py:13)、[models.py](../../../backend/packages/hyperliquid_news/models.py:18)、[account_monitor.py](../../../backend/packages/hyperliquid_news/account_monitor.py:37)、[service.py](../../../backend/packages/hyperliquid_news/service.py:244)、[episodes.py](../../../backend/packages/hyperliquid_news/episodes.py:11)、[模块文档](../../OdAIly代码与整体设计/Hyperliquid关键地址与新闻事件.md)。

## 对复杂特征的可行提取方式

以“BTC 盈利很多、其他资产亏损”为例，先在相同观察窗口内分别列出每种合约的交易净盈亏，计算：BTC 盈利额、其他合约盈利之和、其他合约亏损绝对额之和、净额、完整轮数、数据覆盖和费用完整性。判断主要盈利来源时，比较“BTC 盈利 / 各资产正收益合计”更清楚；直接除以账户净额可能超过 100%，也容易因盈亏相抵而失真。最终如只能证明观察窗口，应写“近期 BTC 高盈利巨鲸”等有限称谓，命名优先突出这一资产特长，不需要补齐其他资产的负面表现，也不采用“偏科”等反差称谓；若突出盈利足以支持，也可考虑“近期BTC爆赚巨鲸”。

时长和方向已有 episode 基础，但当前高效统计查询会把完全落在窗口内的分段折叠成 `round_summaries`，省略轮次明细；原 segment `payload` 仍保存 episode，可以新增专项聚合或按需读取，不能直接把现有汇总结果当作完整时长样本。原始逐笔成交在压缩后会删除，所以逐次加仓时序、每轮最大仓位和过程中的风险偏好不一定还能恢复，应在压缩前另行保存需要的统计。[store.py](../../../backend/packages/hyperliquid_news/store.py:277)、[compact_history.py](../../../backend/packages/hyperliquid_news/compact_history.py:97)。

当前观察状态表按地址覆盖更新，不是完整的持仓时间序列；要证明“连续多周重仓”“长期偏多”“近期从多头转为空头”，应新增有界画像快照或直接累计时间统计，不能只依赖最新状态和零散新闻事件。[store.py](../../../backend/packages/hyperliquid_news/store.py:99)、[commit_observation](../../../backend/packages/hyperliquid_news/store.py:442)。

## 官方接口复核

本日读取官方 Info 文档和 SDK，确认以下事实：

- `userFillsByTime` 每次最多返回 2,000 条，只能取得最近 10,000 条成交；请求按地址与时间筛选，没有 `coin` 参数。分页结束仅表示取得了接口当前保留的数据，不证明全历史完整。`aggregateByTime` 合并部分成交，不等于完整持仓轮，也没有承诺扩大保留范围。[Info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#retrieve-a-users-fills-by-time)
- `clearinghouseState` 官方返回 `leverage`、`marginUsed`、`cumFunding` 等字段，但当前 `Position` 没有保留这些字段。高杠杆、保证金占比和资金费画像需要扩充采集及口径后再使用；统一账户或组合保证金的账户余额也不能直接套用旧保证金摘要。[官方永续账户摘要](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals#retrieve-users-perpetuals-account-summary)、[Position](../../../backend/packages/hyperliquid_news/models.py:72)
- 官方 SDK 的 `portfolio()` 返回多周期账户价值、PnL 和成交量，未提供“单资产全历史收益贡献表”。该表需要自行基于一致且完整的数据另算，不能从账户曲线推断。[官方 Python SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/info.py)

## 现有备注入口与接入含义

系统已有地址备注：`save_address_note` 将人工备注保存到 `address_note:<address>`，事件通过 `with_address_notes` 带出；定稿在生成后把备注插入正文首个“某地址”之后。备注目前是字面文本，不是给模型的指令，也不是结构化、带证据和有效期的地址名称。

因此正式命名应另有“称谓、证据、统计窗口、生成时刻、失效条件、历史版本”等资料，不能只把 AI 生成的名字塞进备注字段就视为完成。既有备注可继续作为编辑提供的线索，但应与经验证据、自动称谓和人工锁定名称区分。[store.py](../../../backend/packages/hyperliquid_news/store.py:558)、[copy.py](../../../backend/packages/hyperliquid_news/copy.py:370)。

接入还涉及两层现有限制：场景 Prompt 规定正文以“某地址”引出；`copy.py` 定稿也围绕这一字面主体插备注、补充账户金额和检查标题。写作输入 `_highlight_material` 主要提供当前事件合约且已命中准入条件的亮点，不是整个账户的多维履历。因此需要独立画像材料及称谓字段，并同步调整各场景写作和定稿校验，不能仅改一句 Prompt。新称谓不能绕过原有账户总盈利标题的官方 allTime 证据要求。[模块写作契约](../../OdAIly代码与整体设计/Hyperliquid关键地址与新闻事件.md:185)、[copy.py](../../../backend/packages/hyperliquid_news/copy.py:350)、[_highlight_material](../../../backend/packages/hyperliquid_news/copy.py:668)。

一次“平仓赚取某金额”“单轮巨亏”可以成为带时间限定的历史事件称谓；它证明的是已经发生的结果。要使用“波段交易巨鲸”“擅长做空”“高胜率巨鲸”等能力或风格称谓，还需多轮行为、窗口间一致性及反例审查。应单独保留“历史事件仍真实”和“当前风格仍成立”两种判定，避免一次成功永久变成能力标签。

## 建议交给 AI 的证据范围

每个地址提供多窗口资产盈亏表、正负收益来源、各资产完整轮数和胜率、当前主要持仓、可证明的多空偏好、持仓时长分布、近期转变、反例与数据缺口；每条资料都带合约身份、范围、观察时间和证据来源。AI 优先选择最突出的正面特长或中性特点；只有缺少此类亮点、仅有突出负面特点时才考虑负面称谓，不要求全面评价。AI 同时返回候选称谓、选择理由、所引用事实、为何优于其他特征、现有称谓是否仍成立。程序只检查事实资格、时效、数字和禁用的比较范围，命名选择仍由 AI 完成。

必须允许“没有足够独特且稳定的特征”：这类地址可以暂用更中性的称谓和地址缩写，后续补充画像；强行给每个地址起一个鲜明名号，容易把短期巧合写成交易能力。

按用户第二轮要求，读者称谓采用“特征称谓＋地址短前缀”，如“近期SOL超高胜率巨鲸0x96”“ETH大空头0xa7”（格式示意）。短前缀由程序从真实地址提取，默认`0x`加前两位十六进制字符；同名同前缀冲突时可延长并保存复用，尾部继续列完整地址。
