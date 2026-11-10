# Solana 免费报价 API 调研

截至 2026-11-03，目标是为 Solana Meme 代币的 Dexscreener 市值突破增加独立行情交叉核验。这里的“报价 API”按是否能提供可用于判别价格的现价、池子与活跃度信息评估；不同服务可能覆盖不同 DEX，任何单一聚合器都不等于链上真值。

## 候选对比

| 服务 | 免费条件 | 可用数据 | 适合作用 | 限制 |
| --- | --- | --- | --- | --- |
| GeckoTerminal Public API（CoinGecko Onchain） | 公共 v2 API 无需 API key；公开文档列出的速率为约 30 次/分钟 | Solana token pools、池级价格、流动性、FDV/市值、成交量和交易数；池数据带最近交易时间 | 最适合作为 Dexscreener 的第二家聚合源；可以校验是否只有单一池报出异常市值，也可检查池子是否长时间无成交 | 数据仍由 DEX 池聚合而来；池子报价可能受低流动性、错误储备或新池影响。必须比较池地址、储备、成交量和最近交易时间，不能只取 token 汇总市值 |
| Jupiter Price API V3 | 无 key 可用，低速率约 0.5 RPS；注册免费 key 后约 1 RPS | Solana mint 的当前 USD 价格，单次最多 50 个 mint | 作为独立价格交叉检查；检查另一套 Solana 行情源是否也报出同等级价格 | 只给现价，不给市场 cap、流动性或 24h 成交量；没有历史序列，需自行采样。价格参考不等于可按该价成交。不能把 `price × total supply` 称为流通市值 |
| Birdeye Data API | 有免费 Standard 方案，每月 30,000 CU；需要 API key | `/defi/price` 可查 token 价格；另有 Solana token 市场数据接口 | 可作为另一家聚合服务的备选，用价格及市场数据做交叉检查 | CU 消耗和端点套餐权限需要按账户/接口确认；免费额度不是无限请求。应先在生产频率下核算 CU 和限速 |

## 推荐用法

1. 优先试接 GeckoTerminal Public API 的 Solana token pools 查询。针对同一 CA 取多个池，而不是让 API 自动给出一个 token 汇总值；记录每个池的地址、价格、USD 储备、最近交易时间、成交量及买卖笔数。与 Dexscreener 指向的池子不一致时，暂停自动触发并标记人工复核。
2. 再以 Jupiter Price API V3 取 mint 现价作独立交叉检查。Jupiter 没有 market cap，适合判断价格是否也支持 Dexscreener 的价格，不适合直接验证市值字段。
3. 突破触发应要求价格在短间隔第二次确认，并检查最新成交时间与近端成交活跃度。24 小时累计成交量不能证明触发时仍有真实交易；高水位只在确认通过后更新，避免一次异常值永久改变后续档位判断。
4. 对市值使用精确定义：`FDV = token price × total supply`；“流通市值”还依赖可信流通供应量。不同 API 的 `market_cap` 字段可能缺失、以 FDV 代填或使用各自供应量口径，不能直接视作同一指标。

## 官方资料

- Jupiter Price API V3：<https://developers.jup.ag/docs/price-api/v3>
- Jupiter API 计划与速率限制：<https://developers.jup.ag/docs/portal/rate-limits>、<https://developers.jup.ag/docs/portal/plans>。官方文档说明无 key 的低速率请求可用，免费 key 提供更高速率；价格 API 只返回当前价格，最多 50 个 mint。
- GeckoTerminal DEX API：<https://www.geckoterminal.com/dex-api>
- CoinGecko Onchain token 地址数据：<https://docs.coingecko.com/reference/token-data-contract-address>。字段包括 USD 价格、market cap/FDV 等，并提供 `last_trade_timestamp`，可用于识别长时间没有成交的行情。
- CoinGecko Onchain token 顶级池：<https://docs.coingecko.com/reference/top-pools-contract-address>
- Birdeye 价格接口：<https://docs.birdeye.so/reference/get-defi-price>
- Birdeye 套餐说明：<https://birdeye.so/data-api>。官方文档说明 Standard 免费层每月 30,000 CU；具体端点权限与速率应以当前账户套餐为准。

## 结论

对这次误报，优先组合是 **GeckoTerminal 池级数据 + Jupiter mint 价格**，并保留 Dexscreener 原始交易对快照。两家返回一致只能提高可信度，不能证明市值真实；如果链上近期没有成交、各池价格差异很大或流动性明显不足，应拒绝自动发布，而不是用另一家 API 的单一汇总值直接替换。

## Walter CA 三源实时复核

2026-11-03 11:17:34（北京时间）同时查询 CA `CHjgAurRkfkcoJpiFCVhpU7uxbm1jKkDcNcnG64xpump`：

| 来源 | 价格 / 估值 | 流动性或储备 | 近 5 分钟成交量 | 备注 |
| --- | ---: | ---: | ---: | --- |
| Dexscreener | 价格 `$0.001082`；market cap `$1,037,016` | liquidity `$367,303.99` | `$653.25` | 交易对 `GCV4wWbvAoLagcGXEThkqqftYD1ZBqVgEXbU5gr8DJzR` |
| GeckoTerminal | 价格 `$0.00001623747`；FDV `$15,828.41`，market cap 未提供 | reserve `$9,130.43` | `$546.70` | 返回同一个 `GCV4...` PumpSwap 池 |
| Jupiter Price API V3 | 价格 `$0.00001550503` | liquidity `$3,666.88` | 不提供 | 响应 block `464743860`；不提供市值或成交量 |

Dexscreener 价格约为 GeckoTerminal 的 **66.6 倍**、Jupiter 的 **69.8 倍**；GeckoTerminal 与 Jupiter 价格相差约 **4.7%**。Dexscreener 返回的 liquidity 数值约为 GeckoTerminal `reserve_in_usd` 的 40 倍；这两个流动性字段口径可能不同，比例只表示异常幅度，不代表严格等值比较。三个请求在同一轮并行发出，池级价格与成交量可以直接交叉比较；估值字段需区分，GeckoTerminal 明确返回 FDV 且 `market_cap_usd` 为空，不能称为流通市值。

本次结果强烈支持前一条快讯使用的 Dexscreener 价格/市值异常：同一池的 GeckoTerminal 价格接近 Jupiter 报价，而 Dexscreener 独自高出约 67 倍。该接口对照无法单独证明链上真值，但足以说明该任务不应只凭 Dexscreener 那一条行情触发。以后应保留池地址、储备、成交和各源时间/区块，并在主要报价源显著分歧时拒绝自动发布。

## 现有生产调用量与升级方案

### 当前规模

2026-11-03 对官方服务器 `odaily-official:/opt/OdAIly` 的生产 SQLite 和 `odaily-meme-scanner.service` 做只读核查。当前 active 跟踪记录按链和周期分布如下：

| 链 | 15 分钟周期 | 4 小时周期 | 按周期折算的 Dexscreener 单币请求/日 |
| --- | ---: | ---: | ---: |
| BSC | 6 | 114 | 1,260 |
| Robinhood | 10 | 69 | 1,374 |
| Solana | 60 | 1,973 | 17,598 |
| 合计 | 76 | 2,156 | 20,232 |

估算公式为 `15 分钟记录数 × 96 + 4 小时记录数 × 6`。这是基于调度周期的名义需求，不是按账单或完整 HTTP 成功日志核实的实际调用数。现有 `MEME_TOKEN_INFO_MIN_GAP_SECONDS=3` 把单 worker 理论上限限制在 28,800 次/日；生产日志会记录 `token_info backlog` 和失败，但没有记录每次成功 HTTP 请求的独立计数器，因此目前不能从日志准确分拆成功、失败和发现批次请求。SQLite 保留了成功 `token_info` 快照，但本次未将其汇总数冒充完整请求数。OKX 每 5 分钟发现时另有按链分批的 Dexscreener 请求；其数量依赖每批 token 数和去重，不能从当前 active 记录推算。

Solana 单币跟踪约占名义单币请求量的 87%。不过不应让 GeckoTerminal 与 Jupiter 跟随这约 17,598 次/日请求，否则会无谓放大调用量，也会把低于首档的币纳入额外校验。

### 建议方案

采用“Dexscreener 负责发现候选，GeckoTerminal + Jupiter 只复核 Solana 里程碑候选”的方案：

1. 保持普通观察调用不变。只有 Dexscreener 观察到 Solana 代币可能首次跨过一个未消费的市值档位时，才建立待确认记录；不要提前更新 `highest_market_cap`，也不要先写入已消费的 `market_cap_milestones`。
2. 串行调用 GeckoTerminal token pools，并按 Dexscreener 的 pool address 对齐同一个池；同时调用 Jupiter Price API V3。保留各来源原始价格、池地址、储备/流动性、成交时间和交易量。GeckoTerminal 无 `market_cap_usd` 时，不从其 FDV 冒称流通市值；只用来源一致的价格作为报价交叉验证。
3. 等待 30–60 秒后重查报价。只有主报价源之间差异在设定容差内、GeckoTerminal 对应池有足够储备且近期有真实成交，才确认跨档；确认后再推进最高水位、消费档位并入叙事队列。二次确认仍异常或成交不活跃时，拒绝该次候选并保留诊断，不生成快讯。
4. GeckoTerminal/Jupiter 超时、限频或无数据时，将候选留在有界重试状态；达到重试上限后转人工复核/拒绝，不允许静默回退到 Dexscreener 单源发布。辅助源请求只在待确认阶段发生，且经 CA+档位幂等，避免 worker 重启造成重复调用。
5. `completed` 批量发现与列表外 `token_info` 跟踪必须进入同一个确认闸门。否则五分钟发现批次仍可能绕过复核逻辑。TG 热议当前是另一类触发，应在产品口径决定后再明确是否纳入同一行情确认规则。

按每个候选执行一次 GeckoTerminal + 一次 Jupiter，额外调用量为 `2 × Solana 待确认候选数`；假设每天 100 个候选，约 200 次/日，远低于两家接口的公开速率上限。每候选的延时复查会额外消耗请求；如复查三源，则预算按 `3 × 候选数` 估算。先记录候选量，再决定是否采用免费额度/API key。

### 上线阶段

1. **补监控**：为每家行情源记录 request count、成功/失败、延迟、HTTP 429、空数据、候选数与分歧比例；同时记录 Dexscreener 发现批次请求数。不得记录 API key。
2. **影子运行一周**：不改变发布决策，只对潜在跨档候选调用两家辅助源并保存复核结果，统计价差、池匹配率、近期成交缺失率、每日日调用量和 API 限流。
3. **灰度拦截**：用环境开关仅对 Solana 普通市值里程碑启用确认；候选价差超过初始容差（建议先从 20%–30% 评估）、池储备低于下限或最近交易超时就不自动入队。阈值要根据影子数据校准，不应把建议数字直接当永久规则。
4. **复盘后推广**：检查误拦、漏拦和限频，再决定是否扩展到 TG 热议或其他链。确认逻辑应以明确的待确认任务状态表达，不让控制台把尚未确认的价格显示成已触发档位。

### 实施注意点

- 当前 `evaluate_market_token` 通过 `highest_market_cap` 计算跨档，并在 `process_due_token_info` 的观察写入中推进最高值；确认闸门必须调整提交顺序，避免异常行情被高水位吸收后永远无法重试。
- `process_due_token_info` 是每次只查询一枚到期代币；completed 扫描则按 Dexscreener 30 个地址批量限制分批。复核请求应只用单个候选，不把发现批次全部扩展到三家 API。
- 新增持久化待确认状态后，需要补 Meme scanner 模块文档、控制台任务状态/字段说明和迁移兼容说明。整体 worker 边界仍在现有 scanner 内时，无需改写系统架构文档；若拆成独立 worker，再更新完整程序架构。
- 免费接口是无 SLA 的公共服务，不能因第二来源不可用而回退放行；拒绝自动发布是本方案的故障关闭策略。
