# OKX 美股周快讯数据程序化获取方案

> 调研日期：2026-09-09。程序化命令已接入本地代码，模块契约见 `docs/OdAIly代码与整体设计/OKX美股周快讯.md`；本文保留调研和口径演进记录。

## 结论

这条周快讯的数值可以通过 OKX 官方公开市场数据 API 程序化获取，不需要 API Key、Secret 或 Passphrase。本项目采用以下固定产品范围：

1. SWAP 中的 USDT 股票永续合约，例如 SNDK-USDT-SWAP、MU-USDT-SWAP、SPCX-USDT-SWAP、SOXL-USDT-SWAP、SKHY-USDT-SWAP。
2. SPOT 中的统一代币化股票现货，例如 XMU/USDT、XNVDA/USDT。现货资产用 X 前缀表示底层股票或 ETF。

OKX 还存在 USD/USDC/USDG 结算的 X-Perps（API 类型为 FUTURES），它们不属于本项目范围，不能混进总额或股票永续分项；否则会同时改变产品分项、币种单位和环比口径。[OKX 股票永续说明](https://www.okx.com/zh-hans/help/stock-perpetuals)、[统一代币化股票现货公告](https://www.okx.com/en-gb/help/okx-to-list-unified-tokenized-stocks-for-spot-trading)、[X-Perps 说明](https://www.okx.com/en-us/help/how-do-stock-and-commodity-x-perps-work)

核心实现方式是：先确定产品清单，再读取每个产品的日线 volCcyQuote，按底层股票代码归并后计算总额、分项额、Top 5 及其占比。环比候选统一使用“当前完整窗口 vs 前一个等长完整窗口”，按 30 日、7 日、1 日计算，采用有规则的择优顺序，而不是每周人工挑最大涨幅。

## 官方接口覆盖情况

| 需要的字段 | 官方来源 | 计算方式 | 结论 |
| --- | --- | --- | --- |
| 可交易的股票永续/现货清单 | GET /api/v5/public/instruments?instType=SWAP、SPOT | 结合产品类型、报价/结算币种和底层代码筛选 | 可获取；公开接口不需要鉴权 |
| 当前 24 小时成交额 | GET /api/v5/market/tickers?instType=... | 使用 volCcy24h，但只适合当前快照 | 可获取，不适合直接算过去 30 日 |
| 历史日/周/月成交额 | GET /api/v5/market/history-candles | 请求 bar=1D、1W 或 1M，累加 volCcyQuote | 可获取；历史 K 线接口支持近年数据，日线窗口足够 |
| 股票永续成交额 | SWAP 日线的 volCcyQuote | 仅保留 USDT 股票永续，逐日累加 | 可获取，单位为 USDT |
| 现货成交额 | SPOT 日线的 volCcyQuote | 仅保留 X<底层代码>-USDT 现货，逐日累加 | 可获取，单位为 USDT |
| 热门股票 Top 5 | 上述全部产品的日线成交额 | 按底层代码归并、降序取前五 | API 没有现成“30 日 Top 5”字段，但可稳定自行计算 |
| Top 5 占比 | 自行计算 | Top5成交额 / 美股相关产品总成交额 × 100% | 可获取，且与总额口径一致 |

官方 API 文档明确说明：市场数据接口不需要鉴权；tickers 提供最近 24 小时成交量；K 线返回 [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]，其中衍生品的 volCcyQuote 是报价币种成交额，USDT 交易对可以直接作为 USDT 金额使用。[OKX API Guide — Market Data](https://app.okx.com/docs-v5/en/)

推荐调用示例：

~~~text
GET https://www.okx.com/api/v5/public/instruments?instType=SWAP
GET https://www.okx.com/api/v5/public/instruments?instType=SPOT
GET https://www.okx.com/api/v5/market/history-candles?instId=SNDK-USDT-SWAP&bar=1D&limit=300
GET https://www.okx.com/api/v5/market/history-candles?instId=XMU-USDT&bar=1D&limit=300
~~~

1D 是 UTC+8 开盘的日线；也可以使用 1Dutc，但必须让所有窗口和发布时间都采用 UTC。对于中文周快讯，建议固定使用 1D，并将截止时间定义为北京时间当天 00:00，避免“截至某日”在周末或跨时区时产生歧义。官方文档还提醒，最新一根 K 线可能未完成，返回 confirm=0 时不能纳入已完成周期。[K 线参数与字段](https://app.okx.com/docs-v5/en/)

## 各项数字的计算口径

### 1. 产品清单与底层代码

不要只用 instId 是否包含某个字符串判断产品。建议每日或每次生成前刷新公开 instruments，并维护一份小型产品映射：

~~~text
{
  "SNDK-USDT-SWAP": {"product": "stock_perpetual", "underlying": "SNDK", "quote": "USDT"},
  "XMU-USDT":       {"product": "tokenized_spot",  "underlying": "MU",   "quote": "USDT"}
}
~~~

筛选规则：

- stock_perpetual：instType=SWAP，USDT 结算/报价，底层代码来自 OKX 股票永续产品清单。
- tokenized_spot：instType=SPOT，quoteCcy=USDT，资产为统一代币化股票，通常是 X 前缀；底层代码去掉首个 X 后归并。
- 股票和 ETF 都应保留在“美股相关产品”清单中，因为示例中的 SOXL 本身是 ETF；正文可称“股票及 ETF 相关产品”，也可以按 OKX 对外命名继续称“美股相关产品”。
- 不能把 DEX 代币页面上的同名资产计入 CEX 现货。OKX 的同名 SPCX 价格页明确标注其为 DEX 资产，不可访问 OKX Centralized Exchange；应以 public/instruments 返回的现货交易对为准。

当前产品范围会变化，不能永久硬编码。OKX 的官方公告显示，SPCXUSDT 曾从 Pre-IPO 合约转换为标准股票永续，且接口中的旧 SPACEX-USDT-SWAP 会过期、新 SPCX-USDT-SWAP 会进入可交易状态。[SPCX 转换公告](https://www.okx.com/zh-hans/help/okx-to-convert-spcxusdt-pre-ipo-contract-to-standard-equity-perpetual)、[API 变更记录](https://www.okx.com/docs-v5/log_en/)

### 2. 总成交额和两类分项

对每个纳入清单的交易对请求日线历史数据。每根已完成日线取 volCcyQuote，在时间窗口内求和：

~~~text
stock_perpetual_volume = Σ volCcyQuote(SWAP, window)
tokenized_spot_volume  = Σ volCcyQuote(SPOT, window)
total_volume            = stock_perpetual_volume + tokenized_spot_volume
~~~

对于 USDT 交易对，直接以 USDT 汇总。不要使用：

- vol：它是合约张数或现货底层资产数量，不是成交额。
- volCcy：衍生品是基础币种数量，不一定是 USDT 金额。
- 当前 volCcy24h 反复采样后累加：它是滚动 24 小时值，会重复计算，且不同市场数据服务存在缓存差异。

窗口建议定义为左闭右开：

~~~text
截止日 D 的近 30 日 = [D-30日 00:00, D 00:00)
截止日 D 的近 7 日  = [D-7日 00:00,  D 00:00)
截止日 D 的近 1 日  = [D-1日 00:00,  D 00:00)
~~~

这样“截至 2026 年 8 月 9 日”表示截至北京时间 8 月 9 日 00:00 的前 30 个完整日历日。若业务实际想表达“包含 8 月 9 日全天”，则应把发布时间放在 8 月 10 日之后，并把截止日设为 8 月 10 日 00:00；两者不要混用。

### 3. 热门股票和 Top 5 占比

先按底层代码归并两个产品类型，再排序：

~~~text
underlying_volume[SNDK] =
    Σ SWAP(SNDK-USDT-SWAP).volCcyQuote
  + Σ SPOT(XSNDK-USDT).volCcyQuote

Top5Share = Σ underlying_volume[Top5] / total_volume × 100%
~~~

Top 5 的排序窗口必须与总成交额一致，均为近 30 日；不能用当前 24 小时热门榜去填“近 30 日最热门股票”。建议保存每个代码的分项成交额，便于人工核查某个股票的成交额是否来自永续、现货或两者。

对 SPACEX → SPCX、Pre-IPO → 标准永续、股票拆分等情况，必须使用底层代码映射和合约生命周期记录，不能只按 API 返回的当前名称拼接。否则一个底层股票可能被拆成两个排名项，或历史成交额在改名日丢失/重复。

## “环比增长”如何挑选

### 候选值

对总成交额分别计算三组等长周期环比：

~~~text
growth_30d = volume[D-30,D) / volume[D-60,D-30) - 1
growth_7d  = volume[D-7,D)  / volume[D-14,D-7)  - 1
growth_1d  = volume[D-1,D)  / volume[D-2,D-1)   - 1
~~~

前一周期为 0 或缺失时，增长率为“不可计算”，不能显示为 100% 或无穷大。每个候选值都应保留：窗口、当前值、前值、增长率、覆盖天数和数据完整性。

### 推荐的默认选择规则

采用“30 日优先、必要时寻找更短周期好角度”的规则：

1. 两个比较周期的已完成日线覆盖率都至少 95%，且前一周期成交额高于可配置的最小基数。
2. 将绝对增长低于 5%视为基本持平，不用于标题中的“增长”；5% 是初始建议值，接入后应根据 8—12 周历史回放调整。
3. 先尝试 30 日环比；若 30 日没有达到正增长阈值，再尝试 7 日，最后尝试 1 日。选中的窗口必须在标题中明确写出，例如“近 7 日环比增长”。
4. 允许用 7 日或 1 日的正增长作为更好的新闻角度，但不能把它省略成看似 30 日增长，也不能把多个窗口中最大的涨幅直接当作默认值。
5. 如果 30 日、7 日、1 日都没有合适的正增长角度，则不写任何“环比增长”或“环比下降”，改用“总成交额 + 近 30 日热门股票 Top 3”作为保底模板。

建议结果结构如下：

~~~json
{
  "status": "positive",
  "selected_window": "7d",
  "growth_pct": 18.6,
  "current_volume": 123456789,
  "previous_volume": 104096000,
  "candidates": {
    "30d": {"growth_pct": -2.1, "quality": "complete"},
    "7d":  {"growth_pct": 18.6, "quality": "complete"},
    "1d":  {"growth_pct": 43.2, "quality": "complete"}
  }
}
~~~

标题如果选到非 30 日窗口，建议把口径写出来：

~~~text
OKX 美股近 30 日成交额超 871.7 亿 USDT，近 7 日环比增长 18.6%
~~~

总成交额的统计窗口始终是近 30 日；只有标题中的增长角度可以切换到近 7 日或近 1 日。若切换后仍无合适角度，使用以下保底句式：

~~~text
OKX 美股近 30 日成交额超 871.7 亿 USDT，近 30 日最热门股票为 SNDK、MU 和 SOXL。
~~~

### 为什么不直接取三个窗口里最大的涨幅

日度增长最容易被单日事件、周末流动性或低基数放大；每周选择最大正数会形成不可审计的择时和择口径。固定优先级既保留“没有月度增长时仍能找到较短期有效动量”的业务需求，也保证每周行为一致。所有候选值写入原始结果，后续可以回放和调整阈值。

## 数据质量、历史回补与异常反馈

### 建议的采集方式

- 每次周报生成时请求最近 60 日的 1D 历史 K 线，足够覆盖 30 日当前窗口、30 日前一窗口和环比候选。
- 同时每日保存 instruments 快照和已完成日线原始响应，保存 instId、底层代码、产品类型、请求时间、截止时间、OKX 响应时间戳和原始 JSON。
- 生产运行不依赖 Supabase；按仓库当前规则将原始数据和生成结果写入本地 SQLite/data/raw 等现有本地运行资产。
- API 文档给出的历史 K 线请求速率为每 IP 20 次/2 秒，公开 instruments 和当前 tickers 也有明确的 IP 限频；采集器应统一限速、重试和缓存，不要在每个交易对上无限并发。[OKX API Guide](https://app.okx.com/docs-v5/en/)

仅依赖周报当天回补会遇到一个问题：已改名或已下线的合约可能不再出现在当前 instruments 清单。每日快照、官方改名公告和生命周期映射应作为历史一致性的保障。首次接入时，需要对至少 8—12 周数据做回放，确认历史 K 线、产品清单和 OKX 页面展示的总额在同一截止时间下基本一致。

### 必须反馈人工的情况

以下情况不要静默填 0、不要自动换第三方源、也不要强行生成“增长”：

- 两个比较周期中任一周期覆盖率低于 95%。
- 前一周期成交额为 0 或无法计算。
- OKX 产品清单无法确认某个交易对是股票/ETF，而不是同名加密资产或 DEX 资产。
- 合约改名、rebasing、Pre-IPO 转标准永续导致底层代码无法连续映射。
- USDT 现货或永续数据缺失，导致总额只能覆盖部分市场。
- 30 日、7 日、1 日均无达到阈值的正增长时，进入 Top 3 保底模板，不阻塞整条快讯。
- 计算出的 Top 5 占比超过 100%、低于 0%，或产品分项之和与总额不一致。

人工反馈可以固定为：

~~~text
本期无法按“环比增长”发布：30日环比 X%，7日环比 Y%，1日环比 Z%；原因：____。已取得总成交额/分项成交额，但增长口径不满足自动发布条件。
~~~

## 已确定的业务口径

1. “美股相关产品”只包括 USDT 股票永续和 USDT 统一代币化股票现货；USD/USDC/USDG X-Perps 排除。
2. “近 30 日”按北京时间前 30 个完整日历日统计。
3. 增长角度按 30 日、7 日、1 日依次寻找达到阈值的正增长；选用 7 日或 1 日时，在标题中明确标注窗口。
4. 三个窗口都没有合适增长时，使用“近 30 日总成交额 + 近 30 日热门股票 Top 3”保底，不强行制造增长数据。

在以上口径下，数值获取、Top 3/Top 5 归并和增长角度选择都可以稳定程序化；不能保证的是每周一定存在正增长，这时直接使用保底模板即可。
