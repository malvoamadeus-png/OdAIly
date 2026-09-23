# OKX 美股周快讯

## 目标

`okx-stock-brief` 是一个只读、可重复执行的周快讯数据命令。它直接从 OKX 公开 API 读取产品清单和历史日线，输出固定三段式中文快讯，不调用 AI，也不依赖 Supabase、主 SQLite 或发布接口。

## 命令

```bash
tools/dev python backend/src/main.py okx-stock-brief
tools/dev python backend/src/main.py okx-stock-brief --as-of-date 2026-09-09
tools/dev python backend/src/main.py okx-stock-brief --as-of-date 2026-09-09 --json
```

`--as-of-date` 是正文展示的北京时间日期，也是统计的排他上界：统计使用该日期前已经完成的 30 个北京时间日。例如传入 `2026-09-09`，统计区间是 8 月 10 日至 9 月 8 日。省略时使用当前北京时间前一天，避免把未完成日线纳入结果。

## 产品范围

- 股票永续：`instType=SWAP`、`state=live`、`instCategory=3`、`settleCcy=USDT`，不要求必须存在对应现货。
- 代币化股票现货：`instType=SPOT`、`state=live`、`instCategory=3`、`quoteCcy=USDT`，且 `baseCcy` 以 `X` 开头；去掉首个 `X` 后作为底层股票代码。
- `instCategory=1` 的加密资产永续、`instCategory=4` 的商品/指数类产品、USD/USDC 等非 USDT 结算产品均排除。
- 不能用“永续和现货的交集”作为产品集合。只上市永续、暂时没有对应现货的股票必须保留，这是此前漏算的根因。

## 数据与校验

- 每个产品请求 `GET /api/v5/market/history-candles?bar=1D`，使用第 8 列 `volCcyQuote` 作为 USDT 成交额。
- 只接收 `confirm=1` 的日线，按 OKX 日线的 UTC+8 边界映射为北京时间日期。
- 产品清单和历史日线请求统一限速、重试；任一产品历史数据请求失败，命令拒绝生成正文，不用 0 填充。
- 30 日窗口必须有 30 个完整聚合日，且总额必须等于股票永续与现货分项之和。
- `--json` 除正文数据外输出产品数量、窗口日期、Top 3、30/7/1 日环比候选、覆盖天数和失败产品，供审计和回放使用。

## 输出

正文固定为三段：

```text
据 OKX 市场数据，截至 YYYY 年 M 月 D 日，过去 30 天 OKX 美股相关产品成交额达 X 亿 USDT。

其中，股票永续成交额为 Y 亿 USDT，现货成交额为 Z 亿 USDT。

近 30 日最热门股票为 A、B 和 C，存储仍是最热门板块。
```

Top 3 按近 30 日股票永续和代币化现货的底层代码合并后排序。若 Top 3 中存储相关标的占其成交额至少 50%，输出“存储仍是最热门板块”；否则输出“当前未形成单一主导板块”。

30/7/1 日环比只作为 JSON 诊断字段，不自动写入正文。这样即使单日因交易时段或单个合约出现异常波动，也不会自动生成误导性的“环比增长”。

## 测试

```bash
tools/dev pytest backend/test_okx_stock_brief.py
```
