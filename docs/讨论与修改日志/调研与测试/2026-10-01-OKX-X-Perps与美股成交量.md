# OKX X-Perps 与美股成交量调研

> 调研日期：2026-10-01。本文只使用 OKX 官方产品说明、官方 API 文档和无需鉴权的公开 REST API；接口返回是动态数据，下面的样例是本次调研时的现场响应。未读取或输出任何密钥。

## 结论

1. **X-Perps 不应按 OKX API 的 `SWAP` 永续合约处理。** OKX 官方帮助页将 X-Perps 定义为“USD-margined expiry futures”（USD 保证金到期型期货）；API 文档将 `SWAP` 定义为 Perpetual Futures，将 `FUTURES` 定义为 Expiry Futures。
2. **X-Perps 的确有“永续式”产品规则，但这不是 API 的 `instType=SWAP`。** API 文档对 `ruleType=xperp` 的说明是“perpetual-style futures”，并明确它只适用于部分 `FUTURES` 合约。因此业务描述可以说“永续式 X-Perps”，但产品分类、筛选和成交额统计必须使用 `instType=FUTURES`。
3. **当前公开接口存在 SNDK 的 X-Perps。** 2026-10-01 查询到：`SNDK-USD_UM_XPERP-310627`。它是股票类别、USD 结算、`ruleType=xperp`，且有 `expTime`。同时也存在另一类产品 `SNDK-USDT-SWAP`，它才是 `instType=SWAP` 的 USDT 股票永续。
4. **历史成交额使用 `GET /api/v5/market/history-candles`。** 以 `bar=1D` 请求日线，使用每根 K 线数组中的 `volCcyQuote` 求和；它是报价币种成交额。X-Perps 的样例单位是 USD，SNDK-USDT-SWAP 的样例单位是 USDT。`confirm=0` 的最新未完成 K 线不能计入已经结束的统计窗口。

## 官方来源

- [OKX：How do Stock and Commodity X-Perps work?](https://www.okx.com/help/how-do-stock-and-commodity-x-perps-work)：官方页面摘要将 X-Perps 定义为 USD 保证金到期型期货，并说明其为特定 TradFi 标的提供价格敞口。
- [OKX V5 API 文档](https://www.okx.com/docs-v5/en/)：官方 API 参考；本调研使用其中的 [Public Data / Get instruments](https://www.okx.com/docs-v5/en/#public-data-rest-api-get-instruments) 和 [Market Data / Candlesticks history](https://www.okx.com/docs-v5/en/#order-book-trading-market-data-get-candlesticks-history) 章节。
- [OKX 公共接口：Get instruments](https://www.okx.com/api/v5/public/instruments?instType=FUTURES&instId=SNDK-USD_UM_XPERP-310627)：本次核查到的 SNDK X-Perp 公开响应。
- [OKX 公共接口：Get instruments（SNDK SWAP）](https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId=SNDK-USDT-SWAP)：本次核查到的 SNDK 股票永续公开响应。
- [OKX 公共接口：History candlesticks（SNDK X-Perp）](https://www.okx.com/api/v5/market/history-candles?instId=SNDK-USD_UM_XPERP-310627&bar=1D&limit=3)：本次核查的历史日线成交量响应。

## X-Perps 是否属于永续

需要区分产品宣传语和 API 产品分类：

| 口径 | OKX 官方定义 | 本项目应如何处理 |
| --- | --- | --- |
| `instType` | `SWAP` = Perpetual Futures；`FUTURES` = Expiry Futures | X-Perps 归入 `FUTURES`，不能按 `SWAP` 查询或汇总 |
| `ruleType` | `xperp` = perpetual-style futures，只适用于部分 `FUTURES` | 可翻译为“永续式期货”，但不能据此改写 `instType` |
| 到期属性 | `FUTURES` 的 `expTime` 是自然交割/行权时间；X-Perp 实例有 `expTime` | 统计时保留具体 `instId` 和生命周期，不把它当作无到期的 SWAP |

因此，若“永续”指 OKX API 的正式产品类型，答案是**否**；若只是指价格交易方式接近永续，答案是**有永续式规则**。对于接口筛选、文档和成交额口径，前一个答案优先。

## API 字段和实例

### 字段含义

官方 `Get instruments` 文档和响应字段给出：

- `instType`：产品类型；当前文档枚举 `SPOT`、`MARGIN`、`SWAP`、`FUTURES`、`OPTION` 等。
- `instCategory`：`instId` 第一段基础资产的资产类别；`1` 是 Crypto，`3` 是 Stocks，`4` 是 Commodities，`5` 是 Forex，`6` 是 Bonds，空字符串表示不可用。
- `settleCcy`：结算和保证金币种；只适用于 `FUTURES`、`SWAP`、`OPTION` 等衍生品。
- `instId`：具体可交易合约 ID。
- `instFamily`：合约族；同一族可对应多个期限实例。
- `uly`：标的；本次 X-Perp 响应为 `SNDK-USD`。
- `ruleType`：当前 X-Perp 响应为 `xperp`；普通 SNDK 股票永续响应为 `normal`。
- `expTime`：到期时间；X-Perp 有值，SWAP 为空。

### SNDK X-Perp 的现场响应字段

请求：

```text
GET https://www.okx.com/api/v5/public/instruments?instType=FUTURES&instId=SNDK-USD_UM_XPERP-310627
```

从响应 `data[0]` 提取的字段：

```json
{
  "instType": "FUTURES",
  "instId": "SNDK-USD_UM_XPERP-310627",
  "instFamily": "SNDK-USD_UM_XPERP",
  "uly": "SNDK-USD",
  "instCategory": "3",
  "settleCcy": "USD",
  "ctValCcy": "SNDK",
  "ruleType": "xperp",
  "expTime": "1940313600000",
  "state": "live"
}
```

`expTime=1940313600000` 对应 `2031-06-27 08:00:00 UTC`；`instId` 的 `310627` 也体现了该期限。这里的重点不是把日期后缀当作永恒命名规则，而是：当前实例是 `FUTURES`，有到期时间，并以 `USD` 结算。

### SNDK-USDT-SWAP 的对照响应

请求：

```text
GET https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId=SNDK-USDT-SWAP
```

关键字段：

```json
{
  "instType": "SWAP",
  "instId": "SNDK-USDT-SWAP",
  "instFamily": "SNDK-USDT",
  "uly": "SNDK-USDT",
  "instCategory": "3",
  "settleCcy": "USDT",
  "ctValCcy": "SNDK",
  "ruleType": "normal",
  "expTime": "",
  "state": "live"
}
```

所以不能只用底层股票代码 `SNDK` 判断产品，也不能只按页面上是否出现 “Perps” 判断；应以 `instType`、`ruleType`、`settleCcy`、`instId` 和 `expTime` 联合判断。

## 如何取得历史成交额

### 官方接口和请求参数

使用公开市场数据接口，不需要 API Key、Secret 或 Passphrase：

```text
GET https://www.okx.com/api/v5/market/history-candles?instId=SNDK-USD_UM_XPERP-310627&bar=1D&limit=300
GET https://www.okx.com/api/v5/market/history-candles?instId=SNDK-USDT-SWAP&bar=1D&limit=300
```

官方文档定义的关键参数：

- `instId`：必填，具体合约 ID。
- `bar`：可选；`1D` 是 UTC+8 开盘日线，`1Dutc` 是 UTC+0 开盘日线。一个统计任务应固定使用一种时区。
- `limit`：单次最多 300 条，默认 100 条。
- `after` / `before`：按时间戳分页，以回补更早或更新的数据。

官方返回的每根 K 线数组固定为：

```text
[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
```

其中：

- `vol`：衍生品合约张数，不能直接当作报价币种成交额。
- `volCcy`：衍生品基础币种数量。
- `volCcyQuote`：报价币种成交额；SNDK X-Perp 的报价/结算口径是 USD，SNDK-USDT-SWAP 是 USDT。
- `confirm`：`0` 表示当前 K 线尚未完成，`1` 表示已完成。

### 本次现场响应示例

X-Perp 请求返回的一根日线为：

```json
[
  "1790784000000",
  "1742.5", "1796.2", "1725.1", "1789.2",
  "222.148", "222.148", "389390.6593", "0"
]
```

按字段位置解释：`volCcyQuote=389390.6593` USD，`confirm=0`，所以该最新日线只能作为实时参考，不能计入已结束的统计窗口。前一根已完成日线的 `volCcyQuote=492744.7936` USD。

SNDK-USDT-SWAP 的现场返回示例中，最近一根日线的 `volCcyQuote=338635524.7547` USDT，且同样需要根据 `confirm` 判断是否已完成。

### 统计公式和注意事项

对固定产品清单和固定完整时间窗口：

```text
historical_turnover = sum(candle[7] for candle in candles if candle[8] == "1")
```

实践中还应：

1. 先调用 `public/instruments` 刷新当前产品清单，再按 `instType` 和 `settleCcy` 分组；X-Perps 不要混入 `SWAP`。
2. 用 `after` / `before` 分页回补超过 300 根的日线，并按 `ts` 去重。
3. 只汇总 `confirm=1` 的完整 K 线；窗口边界固定为同一时区的完整日线。
4. USD 和 USDT 是不同单位，不能直接相加；若业务要合并，必须先规定汇率或明确只做同币种分项。
5. `market/tickers` 的 `volCcy24h` 是滚动 24 小时快照，只适合当前快照，不应通过反复采样累加成历史成交额。

## 对现有“美股成交量”口径的影响

- 若统计“USDT 股票永续”，使用 `instType=SWAP`、`instCategory=3`、`settleCcy=USDT` 的产品，例如 `SNDK-USDT-SWAP`，并以日线 `volCcyQuote` 汇总。
- 若统计 OKX 的 X-Perps，应另列 `instType=FUTURES`、`ruleType=xperp`、`instCategory=3` 的产品，例如 `SNDK-USD_UM_XPERP-310627`；单位是 USD，且按合约期限记录。
- 不应把 X-Perps 的 USD 成交额直接并入 USDT 股票永续总额；除非业务明确规定换算方法，并在结果中保留原始币种和换算口径。

