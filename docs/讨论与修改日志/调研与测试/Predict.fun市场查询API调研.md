# Predict.fun 市场查询 API 调研

> 调研日期：2026-11-17（Asia/Shanghai）
> 范围：Predict.fun 官方 REST API 文档、官方 OpenAPI、主网与测试网只读请求。
> 目的：确认能否获取市场列表、成交量排名和近期上新数据。
> 安全边界：只发起 GET 请求；没有连接钱包、签名、提交订单或写入外部服务。

## 结论

1. **Predict.fun 有官方公开文档的 REST API。**开发者文档独立于用户提供的[协议技术概览](https://docs.predict.fun/zh/developers/technical-overview)，位于 [Predict Developer Docs](https://dev.predict.fun/)，并发布了 [REST OpenAPI 文档](https://dev.predict.fun/openapi/rest.json)。
2. **可以直接查询成交量最高的市场。**`GET /v1/markets` 支持 `sort=VOLUME_24H_DESC` 和 `sort=VOLUME_TOTAL_DESC`；设置 `includeStats=true` 后，记录带有 `stats.volume24hUsd`、`stats.volumeTotalUsd` 等统计字段。
3. **可以直接查询最近发布的类目。**`GET /v1/categories` 默认按 `PUBLISHED_AT_DESC` 排序，也可显式传入；类目包含 `publishedAt`、`createdAt` 和嵌套的 `markets`。
4. **单个市场有 `createdAt`，但市场列表不支持按创建时间排序。**当前 `MarketSort` 枚举没有 `CREATED_AT_DESC`。若要精确按单市场创建时间取最新项，需要分页读取市场后按 `createdAt` 本地排序，或先取新发布类目再检查其 `markets`。
5. **读取主网数据需要 API key。**主网每个请求都必须带开发者控制台签发的 `x-api-key`；无需钱包 JWT 的只读查询仍然需要这个 key。本次无 key 请求主网 `/v1/markets` 返回 HTTP 401。测试网可不带 key，但本次读到的记录包括 QA fixture，不能当作主网实时行情。

## 推荐接口

### 市场 24 小时成交量排名

```bash
curl 'https://api.predict.fun/v1/markets?first=20&sort=VOLUME_24H_DESC&includeStats=true' -H 'x-api-key: YOUR_API_KEY'
```

响应列表位于 `data`，`cursor` 用于下一页。每条市场的 `stats.volume24hUsd` 是 24 小时成交量美元值。默认每页 20 条，最大 100 条。需要累计成交量排名时改为 `sort=VOLUME_TOTAL_DESC`，读取 `stats.volumeTotalUsd`。

### 最近发布的类目与市场

```bash
curl 'https://api.predict.fun/v1/categories?first=20&sort=PUBLISHED_AT_DESC&includeStats=true' -H 'x-api-key: YOUR_API_KEY'
```

响应 `data` 中每个类目带 `publishedAt` 与 `createdAt`，其 `markets` 数组列出类目下的市场。这个接口适合回答“最近上了哪些主题/类目”。若“上新”特指独立市场而非类目，检查各市场的 `createdAt`；官方没有为 `/v1/markets` 提供按 `createdAt` 排序的参数。

### 类目成交量排名

```bash
curl 'https://api.predict.fun/v1/categories?first=20&sort=VOLUME_24H_DESC&includeStats=true' -H 'x-api-key: YOUR_API_KEY'
```

类目排序支持 `VOLUME_24H_DESC` 和 `VOLUME_ALL_DESC`，统计字段为 `stats.volume24hUsd` 和 `stats.volumeTotalUsd`。注意这回答的是类目成交量排名，不是单个市场排名。

### 分页

首个请求使用 `first` 控制页面大小；后续请求把响应里的 `cursor` 原样传给 `after`，例如 `&after=<cursor>`。`/v1/markets` 与 `/v1/categories` 每页最多 100 条。返回 `cursor: null` 时没有下一页。

## API 契约要点

| 需求 | 官方端点 | 参数/字段 | 边界 |
| --- | --- | --- | --- |
| 市场列表 | `GET /v1/markets` | `first`、`after`、`status`、`tagIds`、`marketVariant` | 缺少 `sort` 时按类目分组及平台类目顺序返回，不是全市场时间倒序 |
| 24 小时成交量最高的市场 | `GET /v1/markets` | `sort=VOLUME_24H_DESC&includeStats=true`；`stats.volume24hUsd` | 当前排序口径是 24h；API 不提供在请求中选择任意时间窗口的市场排名 |
| 累计成交量最高的市场 | `GET /v1/markets` | `sort=VOLUME_TOTAL_DESC&includeStats=true`；`stats.volumeTotalUsd` | 使用 API 返回的累计成交量字段 |
| 最近发布的类目 | `GET /v1/categories` | `sort=PUBLISHED_AT_DESC`；`publishedAt`、`markets[]` | 按类目发布时间，不等同于每个子市场的创建时间 |
| 类目成交量排名 | `GET /v1/categories` | `sort=VOLUME_24H_DESC` 或 `VOLUME_ALL_DESC`，配合 `includeStats=true` | 类目级聚合统计 |
| 单市场最近上新 | `GET /v1/markets` | `createdAt` | 有创建时间字段，但没有创建时间排序枚举；需本地排序或借助近期类目缩小范围 |

## 鉴权与限流

- 主网 REST Base URL：`https://api.predict.fun`；测试网：`https://api-testnet.predict.fun`。路径以 `/v1` 开头。
- 文档说明主网所有请求都需要 `x-api-key`，从 [developer console](https://developers.predict.fun) 获取。只对钱包执行操作的路由才需要 `Authorization: Bearer <jwt>`；市场/类目 GET 不需要 JWT。
- 主网新建 app 的通用 REST 限额为每分钟 250 次；`/v1/search` 为每分钟 120 次。具体 app 限额应以控制台显示和响应中的 `RateLimit-*` headers 为准；超限返回 429。
- 测试网不需要 API key，按 IP 限流；它适合验证响应结构，不代表主网实时数据。

## 只读实测

测试时间：2026-11-17（Asia/Shanghai）。

- 主网请求 `GET https://api.predict.fun/v1/markets?first=3&sort=VOLUME_24H_DESC&includeStats=true`，不带 `x-api-key`，返回 HTTP 401，JSON 错误码 `unauthorized`。这与官方文档要求主网 key 一致。
- 测试网同类请求返回 HTTP 200；响应形状为 `{success, cursor, data}`，记录含 `stats.volume24hUsd`、`stats.volumeTotalUsd`、`createdAt`、`categorySlug` 等字段。返回样本包括 QA fixture，因此未把这些测试数据作为当前真实市场结果或排名证据。
- 测试网响应包含 `RateLimit-Limit: 240` 和 `RateLimit-Remaining`，与官方文档描述的未带 key 测试网默认每分钟限额相符。

## 指定网页市场 slug 实测

查询时间：2026-11-17 11:27（Asia/Shanghai）。使用主网 API key 发出只读请求：

```text
GET https://api.predict.fun/v1/categories/howard-lutnick-out-as-secretary-of-commerce-by-december-31?includeStats=true
GET https://api.predict.fun/v1/categories/jensen-huang-rejoins-nvidia-before-divorce?includeStats=true
```

两个请求均返回 HTTP `200`。这两个网页 URL 的 slug 可直接作为 category slug 查询，`data` 返回类目及其 `markets` 数组。

| 网页 slug | 类目 ID | `publishedAt` | 嵌套市场 |
| --- | ---: | --- | --- |
| `howard-lutnick-out-as-secretary-of-commerce-by-december-31` | `40329` | `2026-11-16T11:17:59.646Z` | `4590307`：Howard Lutnick ... by November 30?；`135533`：... by December 31? |
| `jensen-huang-rejoins-nvidia-before-divorce` | `771048` | `2026-11-16T11:16:47.759Z` | `4590359`：Jensen Huang rejoins NVIDIA before divorce? |

第一条网页路径对应的类目实际包含两个不同截止日的市场；其中 11 月 30 日市场 `createdAt` 为 `2026-11-16T10:10:45.117Z`，12 月 31 日市场的 `createdAt` 为 `2026-03-31T20:28:14.206Z`。因此类目的近期 `publishedAt` 不代表每个嵌套市场都是新创建的。第二条类目和其市场 `createdAt` 均为 `2026-11-16T10:15:03.959Z`。

传入 `includeStats=true` 后，类目 `stats` 有流动性和成交量统计；市场列表的 outcomes 还返回 Yes/No 的 `bestBid`、`bestAsk`。例如 Jensen 类目当时返回 `volume24hUsd: 51.03`、`volumeTotalUsd: 51.03`、`totalLiquidityUsd: 29744.56`。数据是查询时快照，会随市场变化。

再按文档化的 `GET /v1/markets/{id}?includeStats=true` 查询类目返回的三个市场 ID，均返回 HTTP `200`。该次单市场响应中的 `tradingStatus` 均为 `OPEN`；统计为：`4590307` 的 `volume24hUsd: 0`、`totalLiquidityUsd: 6001.50`；`135533` 为 `0`、`3853.87`；`4590359` 为 `51.02`、`29744.56`。同一 Jensen 类目的前一条请求返回 `51.03`，说明这些活跃统计会在请求间变化。

### 网页 GraphQL 请求

用户提供的浏览器 Network 信息显示页面向 `https://graphql.predict.fun/graphql` 发出 POST 并收到 HTTP `200`，这证明用户浏览器中的页面请求成功。但所提供内容只有请求头，没有 POST payload（GraphQL operation 与 variables）。本环境使用相同 Origin、Referer 和浏览器请求头发起只读 GraphQL introspection POST 时，Cloudflare 返回 HTTP `403`；因此未能独立复现网页操作，也不能从请求头反推出查询体。获取确切 GraphQL 查询需要 Network 面板中的 Payload / Request body。按 slug 查询这两个目标目前可直接使用上面的官方 REST 类目接口，无需依赖 GraphQL。

2026-11-17 再从官网当前生产 JS 确认，市场列表用 `GetCategories` GraphQL operation，`$sort` 类型为 `CategorySortInput`，传给 `categories(filter: $filter, sort: $sort, pagination: $pagination)`。响应包含类目 `startsAt`、`createdAt`、`slug`、状态、标签，以及带题目标题和状态的嵌套 `markets`。市场卡片的开始时间取类目 `startsAt`；一个类目下的多个子市场共享同一事件开始时间。用户截图中的“开始时间”是可排序列，前端提供 `STARTS_AT_ASC` 与 `STARTS_AT_DESC` 两个值，点击列标题会切换方向。截图本身未显示该列已被选中。

这两个 `STARTS_AT_*` 值属于网页 GraphQL 的排序枚举，不是公开 REST OpenAPI 的 `CategorySort` 或 `MarketSort`：REST `CategorySort` 目前列出 `POPULAR`、`VOLUME`、`VOLUME_24H_DESC`、`VOLUME_ALL_DESC`、`PUBLISHED_AT_ASC`、`PUBLISHED_AT_DESC`，没有开始时间排序。官网前端 bundle 中可核对 `GetCategories` 查询与排序控件：[GraphQL 查询及 `CategoryFeedCard`](https://predict.fun/_next/static/chunks/16wvzjdti38l6.js)、[开始时间排序控件](https://predict.fun/_next/static/chunks/1ad785uroytj6.js)。这些是随网站版本变化的内部实现，不是稳定的开发者 API 契约。

本环境实际尝试了带网页 Origin、Referer、Chrome User-Agent 等头的 GraphQL POST，以及 GraphQL GET；两者均返回 Cloudflare HTTP `403` HTML 拦截页，而不是 GraphQL JSON。用户提供的浏览器请求为 `200`，说明 endpoint 可由官网页面正常访问；但仅复制可见请求头不足以从当前脚本环境复现。若需在用户浏览器中确认具体请求体，应在“开始时间”列点击一次后，于 DevTools Network 中打开对应 GraphQL POST 的 Payload；该次请求会体现方向对应的 sort 和翻页 variables。网页内的 `fetch` / DevTools Replay 可复用该浏览器上下文；未经验证，不能保证把请求搬到服务器后也能绕过 Cloudflare。

用户随后补充了两个真实 payload。`GetLiveEventCategories` 只筛选 `isLive: true` 的类目，并返回 `id`、`parentId`，没有标题或开始时间，不适合作为题目筛选入口。`GetCategories` 才是目标列表查询：它返回类目卡片、`startsAt`、标题和嵌套市场。用户提供的变量仍是 `sort: MOST_POPULAR`；改用 `STARTS_AT_ASC` 或 `STARTS_AT_DESC` 时，应先移除原请求的 `pagination.after`，从新排序的第一页开始，因为旧 cursor 是按原 sort 产生的。后续页再使用新响应的 `pageInfo.endCursor`。把 `first` 提高到每页上限可减少翻页次数。

收到 payload 后，本环境再次发起 `GetCategories` 只读 POST，Codex 应用侧同样收到 Cloudflare HTTP `403`；没有获取到该查询的实时响应数据。

用户要求用 Playwright 模拟后，使用全新 Chromium 上下文加载官网页面，页面本身返回 HTTP `200` 并渲染成功。切换到表格视图并点击“开始时间”后，浏览器实际发出 `GetCategories`，变量为 `sort: STARTS_AT_DESC`、`filter.tags: ["3"]`、`status: OPEN`、`pagination.first: 16`，第一页没有 `after`。这验证了前端操作对应的查询和排序参数。该隔离上下文的 GraphQL POST 随后以 `net::ERR_FAILED` 结束，没有收到 HTTP response 事件，列表显示为空；因此仍未拿到 GraphQL 实时响应。此结果说明新建的 Playwright context 没能复用用户已有浏览器中的访问状态，不能据此判断接口在用户浏览器也不可用。

补充检查 Predict.fun 当日生产前端构建后，首页侧栏使用的 GraphQL operation 为 `GetSidebarCategories`，endpoint 为 `https://graphql.predict.fun/graphql`。其中 `newMarkets` 不是严格最新市场流：它以 `MOST_POPULAR` 从筛选后的候选中取 15 个类目，浏览器端再按类目 `createdAt` 排序，只展示前 5 个。筛选变体包含 `DEFAULT`、体育/电竞和 `TWEET_COUNT`，排除了 `CRYPTO_UP_DOWN` 与 `UP_DOWN`，但无法保证排除天气等 `DEFAULT` 类目。该查询未选择类目 `publishedAt`、slug 或单市场 `createdAt`，所以首页 “New” 不能作为完整、可增量消费的市场发现 API。该细节来自生产前端实现，可能随网站更新，不是稳定 API 契约。生产构建证据：[GraphQL endpoint 配置](https://predict.fun/_next/static/chunks/24449606sxsm3.js)、[`GetSidebarCategories` 查询](https://predict.fun/_next/static/chunks/30_nwox253e6o.js)、[前端排序与取前 5 项](https://predict.fun/_next/static/chunks/1nghb0xww-9rw.js)。

### `/markets/new` 页面查询

检查 [Predict.fun 最新市场页](https://predict.fun/zh-cn/markets/new) 的页面 hydration 数据和生产前端后，发现“最新”实际是类目标签，不是固定的时间排序。此前抓取到的一次服务端初始 query state 为 `filter.status=OPEN`、`filter.tags=["3"]`（tag id `3` 的名称为“最新”）、`sort=MOST_POPULAR`。用户随后提供的截图显示成交量列带有排序标记，说明当前浏览器状态可能已改为其他排序；该页面的 sort 可以独立切换。GraphQL endpoint 使用官网配置的 `https://graphql.predict.fun/graphql`。

该页一次快照返回 `totalCount: 837`，首屏响应有 `pageInfo` 游标和类目 `createdAt`，并嵌套标题与统计。页面的排序选项包括 `MOST_POPULAR`、24 小时/7 天/累计成交量、开始时间、奖励和价差；开始时间可升序或降序。页面没有类目或市场 `CREATED_AT_DESC`。`STARTS_AT_*` 是事件开始时间，不是市场创建时间。首屏数据也不是严格按创建时间倒序，例如第一项 `createdAt=2026-11-15T04:12:45.465Z`，后续项出现 `2026-11-16T04:40:02.000Z`。

这说明该页面的网络查询可作为一种预设标签筛选，但“最新”字样不表示结果按创建时间排列。Network 中实际 GraphQL POST 的 operation 名和完整 request body 不在服务端 hydration query key 内；本环境直接 POST 被 Cloudflare 拦截，因此本节依据页面 hydration state 和生产 JS 追踪出的 filter/sort，而不把推断包装成一份已成功复现的完整 GraphQL payload。

### 按开始时间发现未见市场

目标可定义为：从“最新”标签的开放类目中展开市场，保留事件 `startsAt` 落在当前时刻前 72 小时至后 72 小时的候选；只保留尚未记录过的市场 ID；最后按题材筛掉高频短周期模板，并把同一类目下的期限/选项市场合并展示。对已经开始的题目，还应要求类目和子市场仍为开放/可交易状态，否则会把已结束或已结算市场混进来。这里的“过去三天至未来三天”是对“过去已开始、未来将开始”的滚动窗口解释。

官网 GraphQL 已有 `STARTS_AT_ASC` / `STARTS_AT_DESC` 可用于列表排序并携带 `startsAt` 与题目；REST API 则没有相同的服务端排序，需要取候选后按 `startsAt` 本地过滤/排序。由于本环境的 GraphQL 原始请求被 Cloudflare 拦截，本次没有声称已从 GraphQL 成功跑出该窗口的实时名单。稳定实现可继续用有 key 的 REST API 分页、维护已见市场 ID 并在本地按时间过滤；若要直接复用网页顺序，则从官网浏览器上下文发起该 GraphQL 查询，并接受其为非稳定内部接口。

## 市场发现可行性实测

目的：验证能否通过官方 API 拉到新市场问题，再筛掉短周期重复市场。主网请求均为只读，使用 `GET /v1/categories?first=100&sort=PUBLISHED_AT_DESC` 并按 `cursor` 翻页；每个类目展开其 `markets[]`，以市场 `id` 去重，再按市场自身 `createdAt` 判断新旧。

一次最新页快照中，100 个类目带出 104 个市场；其中 93 个标题命中高频 Up-or-Down / 天气模式，剩余 11 个主要是体育类目，其中有的市场 `createdAt` 早于本次 `publishedAt` 数天。比如 Counter-Strike 类目 `publishedAt` 为 `2026-11-17T04:00:11.845Z`，其市场 `createdAt` 为 `2026-11-10T09:09:59.377Z`。因此不能只按类目 `publishedAt` 认定市场是新建的。

为覆盖当次 feed 最新发布时间往前 24 小时的市场，实测翻了 20 页、共 2,000 个类目，展开 2,908 个不同市场；其中 2,388 个市场的 `createdAt` 落在该窗口内。基于标题/slug 的简单模板和体育赛事规则过滤后，得到以下真实候选（`publishedAt` 为类目，`createdAt` 为市场；时间为 UTC）：

| 类目标题 | 市场问题示例 | 市场创建时间 | 判断 |
| --- | --- | --- | --- |
| Jensen Huang rejoins NVIDIA and market cap returns to $5T by ...? | Jensen Huang rejoins NVIDIA and market cap returns to $5T by December 31, 2026? 等 3 个期限市场 | `2026-11-16T10:51:28.133Z` | 科技/公司人物，适合关注 |
| Will Nvidia (NVDA) beat quarterly earnings? | 同标题 | `2026-11-16T14:13:29.701Z` | 财报事件 |
| Next Qwen Max Model (3.9+): Text Arena Debut? | 按模型分数阈值拆成 4 个市场 | `2026-11-16T14:05:48.815Z` | AI 产品发布 |
| Howard Lutnick out as Secretary of Commerce by December 31? | 当前新建的子市场问 Lutnick 是否于 11 月 30 日前离任 | `2026-11-16T10:10:45.117Z` | 政治人物；父类目本身很旧，需按子市场时间筛选 |
| Jensen Huang rejoins NVIDIA before divorce? | 同标题 | `2026-11-16T10:15:03.959Z` | 与用户给出的关注类型相符 |
| Will Jensen Huang join SpaceX by...? | 按 2026、2027 等期限拆成 3 个市场 | `2026-11-16T10:11:28.347Z` | 科技/人物 |
| SpaceX becomes a US mobile carrier by ___? | 2 个期限市场 | `2026-11-16T10:15:46.351Z` | 公司/行业事件 |
| Which coalition will form the next Swedish government? | 21 个组合市场 | `2026-11-16T10:19:59.555Z` | 政治事件 |
| Japan Core CPI YoY in 2026 | 按数值区间拆成 6 个市场 | `2026-11-16T10:16:49.947Z` | 宏观数据 |
| Grammys 2027: Album of the Year Winner | 15 个候选市场 | `2026-11-16T10:24:17.515Z` | 流行文化 |

这些结果证明核心目标可实现：API 不只返回栏目名，也返回可直接判断的 `question` / `title`、市场创建时间和状态。要找值得关注的题目，先用 `market.createdAt` 做增量和去重，再按 `marketVariant`、slug/title 模板排除重复流，最后对剩余自然语言问题做人工或模型筛选。多结果类目应按父类目合并展示，避免 21 个政府组合或 47 个 Google 搜索人选占满列表。`CRYPTO_UP_DOWN`、`UP_DOWN` 可优先排除；天气等仍可能落在 `DEFAULT`，需要标题规则或语义过滤。

用户给出的 `bitcoin-up-or-down-november-16-2026-10pm-et` 也可按类目 slug 查询，返回市场 `id: 4565753`，问题为 `Bitcoin Up or Down - November 16, 10PM ET`，状态 `RESOLVED` / `CLOSED`，其 `createdAt` 为 `2026-11-15T03:07:44.327Z`。该样本说明具体网页链接能定位市场，但在本次最新列表快照中它已不属于新市场。

### 增量扫描边界

- 官方类目 API 按类目 `publishedAt` 排序，而非按嵌套市场 `createdAt` 排序；市场列表 API 没有 `CREATED_AT_DESC`。实测 `GET /v1/markets?sort=CREATED_AT_DESC` 返回 HTTP `400`。
- 类目可能在市场创建后稍晚发布，也可能含有很早以前创建的其他市场。扫描端应持久化已见过的市场 ID，并按市场 `createdAt` 水位筛选；分页时保留重叠范围再去重，避免列表变化造成漏项。官方文档没有承诺分页读取期间的快照一致性或按市场创建时间过滤。
- 单页 100 类目通常不足以容纳完整 24 小时新市场：本次需要 20 页。API 的通用主网限额为每 app 每分钟 250 次，按需翻页即可。

### Linux 后台请求实测与运行方案

2026-11-17 在官方服务器 `odaily-official`、`/opt/OdAIly` 的 Python 3.12.13 环境进行只读验证：带 API key 的官方 REST 请求成功；不带 key 返回 HTTP `401`。按 `PUBLISHED_AT_DESC` 读取 60 页、共 6,000 个开放类目，覆盖当时约 72 小时的类目发布时间；展开后有 2,475 个类目的 `startsAt` 落在当前时刻前后 72 小时。以临时比较时间 `2026-11-17T03:27:00Z` 筛选市场首次观察时间，找到 232 个市场 ID；排除明显涨跌/天气模板后剩两个 Dota 2 市场。此扫描只证明真实数据可被服务器程序拉取，临时比较时间不等于持久化去重状态，这两个 Dota 项目也不代表已完成“有趣”判断。

服务器当时没有 Predict 专用环境变量或对应 systemd unit。key 通过 SSH 标准输入用于只读请求，没有回显或写入服务器；部署 worker 时需由运维配置 `PREDICT_FUN_API_KEY`。后台实现采用 REST，不依赖浏览器、Playwright 或 GraphQL：固定从类目 `PUBLISHED_AT_DESC` 开始分页，展开市场并按 ID 写入独立 SQLite；首轮建立基线，后续只报告首见市场，按类目 `startsAt` 筛选 ±72 小时窗口。默认每 10 分钟最多翻 80 页，低于每 app 每分钟 250 次的限额；API 仍未承诺分页快照一致性，因此要保留每轮从第一页开始的扫描和持久化 ID 去重。

业务筛选的第一阶段只排除明确的 `CRYPTO_UP_DOWN` / `UP_DOWN` 与天气模板，剩余题目保留给人工复核。真正“读者可能喜欢”的语义判断、同类目多市场聚合及自动写稿发布均不属于该 API 扫描器的保证能力，需另行定义业务规则。

## 官方来源

- [用户给出的技术概览](https://docs.predict.fun/zh/developers/technical-overview)：介绍协议的 CLOB、条件代币、结算和收益架构；REST 查询接口在独立的开发者文档中。
- [Developer Docs REST API 总览](https://dev.predict.fun/api)：主网/测试网 base URL、鉴权、分页、响应形状、`/openapi/rest.json`。
- [Get markets](https://dev.predict.fun/api/markets/get-markets)：`GET /v1/markets` 的筛选、排序、`includeStats`、响应结构。
- [Get categories](https://dev.predict.fun/api/categories/get-categories)：`GET /v1/categories` 的 `PUBLISHED_AT_DESC` 默认排序、成交量排序、时间字段和类目中的市场。
- [Get category by slug](https://dev.predict.fun/api/categories/get-category-by-slug)：按 slug 读取类目及其嵌套市场；本节记录两个给定网页 slug 的主网实测。
- [Get market by ID](https://dev.predict.fun/api/markets/get-market-by-id)：通过类目响应中的市场 ID 进一步查询市场详情与统计。
- [Rate limits](https://dev.predict.fun/resources/rate-limits)：按 app/IP 的请求限制与响应限流头。
- [REST OpenAPI JSON](https://dev.predict.fun/openapi/rest.json)：`MarketSort`、`CategorySort`、市场/类目统计字段的机器可读定义。
