# MSX快讯

## 模块职责

- `us-market` 任务负责生成美股相关的 MSX 快讯。
- 当前支持 `close`、`premarket`、`open` 三种输出。
- 生成结果通过 Push Data API 发送，默认 `isPublish=false`、`isPush=false`。
- 当前股票池切换为 AI 概念股池；指数仍保留 `SPX`、`IXIC`、`DJI`、`VIX` 用于开盘/收盘正文。

## 文案与输出约定

- 文案生成入口：`backend/packages/briefing/generator.py`
- 固定尾文由 `FOOTER_TEXT` 统一维护，当前版本为：

  `据悉，MSX是一家头部RWA交易平台，累计已上线数百种 RWA 代币，涵盖 NVDA、GOOGL、MSFT、AMZN、META、TSM、AMD 等热门美股及 ETF 代币标的。`

- AI 概念股默认 watchlist 由配置与代码共同维护，当前默认包含：

  `NVDA、GOOGL、MSFT、AMZN、AVGO、TSM、META、MU、AMD、ASML、ORCL、ARM、PLTR、IBM、KLAC、DELL、MRVL、PANW、ANET、SAP、CRWD、ISRG、NOW、CDNS、ACN、ADBE、SNPS、SNOW、NXPI、TER、ALAB、CRWV、ON、ROK、BIDU、IRM、WDAY、TWLO、SMCI、SYM、TEAM、CGNX、TTD、AVAV、TEM、TTEK、PATH、EPAM、SOUN、AMBA`

- `premarket` 正文格式：
  - `根据 MSX.COM 数据，美股盘前{AI概念股句子}。`
  - 换行后拼接固定尾文。
- `open / close` 正文格式：
  - `根据 MSX.COM 数据，{指数句子}。{AI概念股句子}。`
  - 换行后拼接固定尾文。

- 标题格式：
  - `美股开盘AI概念股普涨/普跌，{领涨或领跌个股}涨超/跌超{幅度}`
  - `美股收盘AI概念股普涨/普跌，{领涨或领跌个股}涨超/跌超{幅度}`
  - `美股盘前AI概念股普涨/普跌，{领涨或领跌个股}涨超/跌超{幅度}`

- 质量校验：
  - 配置项 `min_valid_ai_stocks` 控制至少需要多少只有效 AI 概念股行情才能成稿，默认 `10`。
  - 为兼容旧配置，worker 仍接受 `min_valid_crypto_stocks` 作为同一阈值的旧字段名，但文档与新配置统一使用 `min_valid_ai_stocks`。

## 边界说明

- 本文档只描述 MSX 快讯模块自己的输出文案与入口。
- 调度日历、任务编排等系统级契约以 `docs/完整程序架构.md` 为准。
