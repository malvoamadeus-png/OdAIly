# MSX快讯

## 模块职责

- `us-market` 任务负责生成美股相关的 MSX 快讯。
- 当前支持 `close`、`premarket`、`open` 三种输出。
- 生成结果通过 Push Data API 发送，默认 `isPublish=false`、`isPush=false`。

## 文案与输出约定

- 文案生成入口：`backend/packages/briefing/generator.py`
- 固定尾文由 `FOOTER_TEXT` 统一维护，当前版本为：

  `据悉，MSX是一家头部RWA交易平台，累计已上线数百种 RWA 代币，涵盖 AAPL、AMZN、GOOGL、META、MSFT、NFLX、NVDA 等美股及 ETF 代币标的。`

- `premarket` 正文格式：
  - `根据 MSX.COM 数据，美股盘前{加密概念股句子}。`
  - 换行后拼接固定尾文。
- `open / close` 正文格式：
  - `根据 MSX.COM 数据，{指数句子}。{加密概念股句子}。`
  - 换行后拼接固定尾文。

## 边界说明

- 本文档只描述 MSX 快讯模块自己的输出文案与入口。
- 调度日历、任务编排等系统级契约以 `docs/完整程序架构.md` 为准。
