# Hyperliquid 市值与单资产统计数据源核验

核验日期：2026-10-13（Asia/Shanghai）。只读取公开官方文档和公开 HTTP 接口，未读取任何凭证、未运行产品服务、未连接生产服务器。以下区分文档确认与实际接口请求成功；文档示例不是实盘数据。

## 结论

- 市值优先采用 CoinGecko 免费 Demo API：官方明确提供 `market_cap`，可以批量查询。当前机器能读取官方文档，但实际 `api.coingecko.com` 请求超时，尚未确认本机直连可用。
- CoinMarketCap 确有免费 Basic API，也有官方免密 Public API，可以作为后备。当前免费方案已明确写商业使用，不应沿用旧版本“免费只供个人使用”的说法。实际免密数据请求本次超时，未完成实盘响应核验。
- 没有核验到可直接接入的“Hyperliquid 地址 × 单资产 × 时间窗口”的胜率接口。Hyperbot 官方宣传地址胜率及 Trader Profile API，但不能据此认定提供单资产胜率；详细 API 文档本次不可达。Hypurrscan 公开 OpenAPI 未出现单资产胜率接口；CoinGlass 可确认的接口是持仓、浮盈亏和鲸鱼发现，不是历史单资产胜率。
- 单资产高胜率暂按官方成交记录重建完整交易轮实现；必须标明资产、统计区间、完整交易轮数和历史覆盖状态。账户整体胜率不替代单资产胜率。

## CoinGecko：首选免费市值来源

一手来源：

- [免费 Demo 认证说明](https://docs.coingecko.com/demo/reference/authentication)
- [Demo 币种市场数据接口](https://docs.coingecko.com/demo/reference/coins-markets)
- [注册与创建 API Key](https://docs.coingecko.com/docs/setting-up-your-api-key)
- [官方错误与限流说明](https://docs.coingecko.com/docs/errors-and-rate-limits)
- [定价页](https://www.coingecko.com/en/api/pricing)

已确认：

1. 免费方案名称为 Demo，根地址 `https://api.coingecko.com/api/v3/`；建议认证头 `x-cg-demo-api-key`，查询参数认证名称 `x_cg_demo_api_key`。
2. `GET /coins/markets?vs_currency=usd&ids=bitcoin,ethereum` 返回 `id`、`symbol`、`market_cap`、`fully_diluted_valuation`、`last_updated` 等。新闻中的市值使用 `market_cap`，不能把 FDV 当成市值。
3. 单次最多 250 个 CoinGecko IDs；Demo/Keyless 数据缓存更新频率为 60 秒。可以按被监测币种批量缓存，没必要每个地址重复查询。
4. 2026-10-13 官方限流文档写 Demo **100 calls/min**；免密请求按 IP 共享限流。所有请求（包括错误响应）计入分钟限流；认证说明另写 HTTP 200 消耗一个月度 credit。
5. 定价页本次返回 403 人机验证，未确认当前月度免费 credits 数量、展示署名和许可细则。不要写死过去常见的 10,000/月或 30/min 为本次已验证事实。每小时一次批量、每月 30 天约 720 次，是请求预算估算，不是确认的月度额度。

实测：官方 Demo 文档返回 200；`/ping`、两币种 `/coins/markets` 请求连接超时。没有用户 API Key，本次未进行认证请求。免费 Demo 支持能力已由文档确认，真实数据链路仍需连接成功后确认。

实现建议：显式维护 Hyperliquid 合约标识到 CoinGecko ID 的映射，避免符号重名、`k` 倍数币和 HIP-3 命名误匹配。市值为 null、为零、响应失败或过期时跳过市值占比事件，绝对持仓和 OI 占比仍可独立评估。美股、贵金属、原油不套用某个同名代币市值；CoinGecko 新增 RWA 类接口也不能自动证明返回的是我们希望使用的底层资产分母。

## CoinMarketCap：免费后备已经存在

一手来源：

- [当前官方定价](https://coinmarketcap.com/api/pricing/)
- [官方 Keyless Public API](https://coinmarketcap.com/api/documentation/pro-api-reference/keyless-public-api)
- [API 总览](https://coinmarketcap.com/api/documentation/)

官方定价页返回 200，2026-10-13 页面写 Basic Free：15,000 API credits/月、50 requests/min、60 秒更新、每次一种换算货币、Commercial use。credits 不等同所有接口都一请求一个 credit，应按端点计费说明核算。

官方免密根地址为 `https://pro-api.coinmarketcap.com/public-api`，无需注册或 header；公开文档列出 `/v3/cryptocurrency/quotes/latest`、`/v3/cryptocurrency/listings/latest`、ID map 等。可按 ID、slug 或 symbol 查询。免密按 IP 限流，具体数字该页面未写；生产可换免费 Key，用 `X-CMC_PRO_API_KEY` header，移除 `/public-api`。

本次免密 `/v3/cryptocurrency/quotes/latest?symbol=BTC,ETH&convert=USD` 请求超时，没有拿到真实响应。结论是“免费服务及市场数据能力由官方文档确认”，不是“当前机器已跑通备用数据链路”。按用户优先顺序，CoinGecko 作为首选，CMC 保留后备；两者已有免费能力，无需现在继续广泛寻找其他市值供应商。

## 单资产胜率与盈利：第三方接口边界

### Hypurrscan

一手来源：[公开 OpenAPI](https://api.hypurrscan.io/openapi.json)。本次返回 200，API 标题 Hypurrscan API、版本 0.1；说明全局 1,000/min/IP，并按端点权重计算，experimental 端点可能随时改变。

检查全部公开 paths：有地址标签、别名、地址详情、TWAP、转账、桥接、持币者等，**没有出现历史合约单资产胜率或单资产已实现盈利统计接口**。`/rank/{address}` 的官方描述是各 HIP-1 token 持币排名，不是交易盈利排名；不能拿它作为账户盈利榜单。

这只说明本次公开 OpenAPI 未提供所需接口，不证明 Hypurrscan 网站或其私有后端一定没有相关数据。没有调用 `addAddress`、`issues/report` 等会改变第三方状态的接口。

### Hyperbot

一手来源：

- [官方 Data API 介绍](https://docs.hyperbot.network/official-links/hyperbot-data-api)
- [官方产品介绍](https://docs.hyperbot.network/)
- [产品指向的详细 API 文档](https://openapi-docs.hyperbot.network/)
- [Data API 入口](https://hyperbot.network/data-api)

官方介绍页返回 200，称提供按 Realized PnL、ROI、Win Rate 排名的地址发现、Top 10 高胜率地址和 Trader Profile API；写 API 根地址 `https://openapi.hyperbot.network/api/upgrade/v2`，AccessKey、SignatureNonce、Timestamp 和 HMAC 签名认证。标准模式说明 1,200 RPM，机构模式 8,000+ RPM；这些是供应商宣传规格，未实测。

**该介绍没有确认 coin/asset 参数、按单资产胜率返回的字段、交易轮口径和统计窗口。** 详细 API 文档和 data-api 入口本次连接超时，费用与免费可用性也未确认。因此只能列为待核验供应商，不能说“Hyperbot 单资产胜率接口已可用”，更不能拿它的整体地址胜率代替用户要求。

### CoinGlass

一手来源：

- [官方文档索引](https://docs.coinglass.com/llms.txt)
- [Hyperliquid Whale Position](https://docs.coinglass.com/reference/hyperliquid-whale-position)
- [按资产查询地址持仓](https://docs.coinglass.com/reference/hyperliquid-position)
- [鲸鱼动作提醒](https://docs.coinglass.com/reference/hyperliquid-whale-alert)
- [定价入口](https://www.coinglass.com/pricing)

官方索引返回 200，说明 whale-position 提供超过 100 万美元的持仓、whale-alert 约最近 200 条提醒；按 coin 持仓接口包含地址、仓位和未实现盈亏。whale-position 的 Markdown/OpenAPI 返回 200，根地址 `https://open-api-v4.coinglass.com`，认证头 `CG-API-KEY`，字段包括 user、symbol、position_value_usd、unrealized_pnl 等。

这些接口可以帮助圈定大仓候选地址，但本次索引**没有找到地址 × 单资产历史胜率接口**。未取得 API Key，定价入口超时；免费额度、所需方案及 HIP-3 原油/黄金/美股覆盖均未知。不能从“按 symbol 查询”推导出支持全部 HIP-3 资产，也不能从浮盈亏推导出历史单资产累计盈利。

## 对后续实现的直接约束

1. 官方盈利榜单先用于发现账户整体盈利突出地址；榜单数值保留原有窗口和来源，不能冒称自算净已实现交易利润。
2. 在受限候选地址集合中按需查询官方持仓及成交，计算单资产胜率和单资产盈利；不遍历全网地址，不生成账户整体高胜率新闻标签。
3. 第三方供给如果将来确认单资产 API，必须保存供应商、资产、窗口、分母/交易口径和历史完整性；与自算统计分开保存。
4. 当前可确认的是免费市值接口能力和第三方大仓发现能力；第三方单资产胜率可自动获取尚未确认。数据端点失败不能推断功能不可实现，应在能力表记录“未完成实测/等待数据源核验”。


## 官方盈利榜单实测补充

主实现线程实际只读取得 [Hyperliquid 官方公开排行榜 JSON](https://stats-data.hyperliquid.xyz/Mainnet/leaderboard)：本次包含 47,215 条 `leaderboardRows`，每条以 `ethAddress` 为地址，`windowPerformances` 包含 `day / week / month / allTime`，各窗口提供 `pnl / roi / vlm`。只记录结构和行数，不将真实地址写入仓库。

因此可以直接按周/月等周期分别取盈利和亏损端的候选，不需要遍历这些地址后再计算榜单。新代码保留官方 PnL、周期、排名和观测时间，也将其作为独立官方盈利亮点，不能混称为自行计算的已实现盈亏。榜单整体账户属性不能替代某资产专项表现。

额外核验 [官方 Python SDK 的 `portfolio`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/info.py)，实际返回 `day / week / month / allTime / perpDay / perpWeek / perpMonth / perpAllTime` 及 `pnlHistory / accountValueHistory / vlm`。本次样本的 `perp*` 与排行榜窗口数字不能直接对齐；未进一步确认 builder DEX 覆盖和榜单的口径对应关系。因此当前直接保留公开榜单原始 PnL，不用 `portfolio` 换算出一个声称等价的榜单收益。

## 官方服务器免费市值接口补验

2026-10-13，odaily-official / root / /opt/OdAIly 通过现有 CoinGeckoMarketCaps 适配请求免费 coins/markets（未提供 API key），精确映射 bitcoin/ethereum/hyperliquid，成功取得三个币种 market_cap 和 last_updated，且通过缓存时效验证。此前本机超时不再作为市值能力未验证结论。真实映射仍需逐币确认；不把美股、贵金属、原油映射成同名代币。
