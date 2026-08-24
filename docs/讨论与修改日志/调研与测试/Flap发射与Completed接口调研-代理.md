# Flap 发射与 Completed 接口调研

> 调研日期：2026-08-20（Asia/Shanghai）  
> 范围：Flap 当前官网、官方 GitBook 文档、官网生产前端 JavaScript、BSC 公开接口。  
> 目的：确认是否存在“发射代币列表”和“已完成（completed）代币列表”的第一方接口，并记录可复现的请求契约。  
> 说明：本次只做公开只读请求，没有连接钱包、提交交易、上传文件或访问生产环境。

## 结论

1. **存在第一方 BSC 代币列表接口：**官网前端把 BSC 主网后端配置为 `https://bnb.taxed.fun`，看板使用 `GET /v3/board`。这是当前最接近“发射/代币列表”的第一方接口。
2. **没有发现名为 `completed` 的第一方列表路由或参数。**直接请求 `/v3/board/completed` 返回 HTTP `400`，响应为 `{"error":"invalid selector, expected one of: trending, worldcup, cat, seedAlpha, tag=<name>, vaultFactories=0x...,0x..."}`。官网前端拼接看板参数的代码也没有 `completed` 参数。
3. **“已完成”在返回记录中是状态字段组合，而不是接口分类名。**实测 `/v3/board` 返回多条 `listed: true`、`progress: "100.00"` 的记录；官网前端将 `listed` 映射为 `GRADUATED`，将未 listed 记录映射为 `BONDING`。因此可以从普通看板响应中筛选已毕业/已上 DEX 的记录，但不能把它称为官方提供的完整历史 completed 列表。
4. **存在发射中的列表：**`GET /v3/board/graduatinghot` 实测返回 `listed: false` 且 `progress` 在 0 到 100 之间的代币，例如返回首条记录的 `progress: "31.73"`。这代表正在 bonding / 接近毕业的看板，不是 completed 列表。
5. **发射本身不是 HTTP 发射列表 API：**官方开发者文档将发射定义为 Portal 合约的 `newTokenV6` / `newTokenV7` 链上调用；官方推荐用 Portal 的 `TokenCreated` 事件索引“每一次新代币发射”。官网的 `/launch` 是 UI 入口，`funcs.flap.sh/api/upload` 是元数据上传接口，不是发射代币列表接口。

## 官方来源

### 1. 官方入口与文档索引

- 官网：<https://flap.sh/>；BSC 看板：<https://flap.sh/board>；发射 UI：<https://flap.sh/launch>
- 官方文档入口：<https://docs.flap.sh/flap>
- 官方文档完整索引：<https://docs.flap.sh/flap/llms.txt>
- 官方链接页：<https://docs.flap.sh/flap/flap-official-links.md>。该页列出官网 `https://flap.sh/`。

### 2. 官方文档对发射的定义

- Portal 发射说明：<https://docs.flap.sh/flap/developers/token-launcher-developers/launch-token-through-portal.md>
  - 当前入口为 `newTokenV6`（`TOKEN_V2_PERMIT` 与当前支持的税收代币）和 `newTokenV7`（`TOKEN_V3_PERMIT`）。
  - `newTokenV6` Solidity 签名为 `function newTokenV6(NewTokenV6Params calldata params) external payable returns (address token)`。
  - `newTokenV7` Solidity 签名为 `function newTokenV7(NewTokenV7Params calldata params) external payable returns (address token)`。
  - 同一页明确：metadata 上传到 IPFS；上传 API 为 <https://funcs.flap.sh/api/upload>。该 API 的用途是图片/metadata，不是 token 列表。
- 官方快速开始：<https://docs.flap.sh/flap/developers/token-launcher-developers/quick-start-token-launcher-developers.md>
  - 先找 Portal 地址，再通过 Portal 或 VaultPortal 发射；没有描述 HTTP 的 token launch/list endpoint。
- 官方 TokenCreated 事件索引说明：<https://docs.flap.sh/flap/developers/wallet-and-terminal-and-bot-developers/index-token-created-events.md>
  - `TokenCreated` 对每一次 token launch 发出。
  - 事件字段：`ts`、`creator`、`nonce`、`token`、`name`、`symbol`、`meta`。
  - 官方建议监听 `TokenCreated`，再收集同一交易中的可选事件并保存 metadata CID。
- BNB 主网 Portal 地址：`0xe2cE6ab80874Fa9Fa2aAE65D277Dd6B8e65C9De0`。
- 官方单币检查说明：<https://docs.flap.sh/flap/developers/wallet-and-terminal-and-bot-developers/inspect-a-token.md>
  - 链上读取方法为 `getTokenV8` / `getTokenV8Safe`（BNB 主网和测试网），旧版本包括 `getTokenV7`。
  - `TokenStateV8` 含 `status`、`pool`、`progress` 等状态字段；这说明链上可判断单币状态，但不是历史列表接口。

## 官网前端 JS 证据

### 生产构建

- 页面：<https://flap.sh/board>
- 观察到的 Next.js 看板 chunk：<https://flap.sh/_next/static/chunks/app/board/page-ee78fcfc7f263828.js?dpl=dpl_6nfhEDUdyna9kxfwQhm4zn1LGCNT>
- 以下代码证据来自当日页面加载的生产 JS 集合；请求封装和看板函数位于该构建加载的公共 chunk 模块中。

### 后端主机配置

在官网生产 JS 的链配置对象中，BSC 主网配置为：

```js
{
  id: "bnb",
  slug: "bnb",
  chainId: 56,
  backend: "https://bnb.taxed.fun",
  metaBase: "https://flap.sh"
}
```

证据是生产 JS 中的 BSC 配置片段，其中 `backend:f(p.env.NEXT_PUBLIC_BNB_BACKEND,"https://bnb.taxed.fun")`；没有发现部署页面注入 `NEXT_PUBLIC_BNB_BACKEND` 覆盖这个默认值。

### 看板请求函数与参数

生产 JS 中的 `getBoardV3` 逻辑等价于：

```js
async function getBoardV3(options = {}, chain) {
  const backend = chain.backend || "https://bnb.taxed.fun";
  const path = options.category && options.category !== "trending"
    ? "/" + options.category.replace(/^\/+/, "")
    : "";
  return fetch(backend + "/v3/board" + path + buildBoardQuery(options));
}
```

`buildBoardQuery` 明确设置的 query 参数：

| 参数 | 前端条件 | 含义/备注 |
| --- | --- | --- |
| `quoteToken` | 非空且不是 `ALL` 时 | 计价代币地址，前端转小写 |
| `sortBy` | `options.sortBy` | 前端排序值：`marketcap`、`volume24h`、`holders`、`liquidity`、`5m`、`1h`、`4h`、`24h` |
| `order` | `options.order` | `asc` 或 `desc` |
| `limit` | `options.limit` | 页面传入的数量参数 |
| `cursor` | `options.cursor` | 分页游标，来自响应 `nextCursor` |
| `isInnovation` | true 时 | `isInnovation=true` |
| `isLowRisk` | true 时 | `isLowRisk=true` |
| `_refresh` | 每次请求 | 当前生产构建硬编码为 `20260627` |

请求方法是 **GET**；代码没有 `POST` body、API key 或登录 session 要求。前端请求结果读取 `response.json()`，并取 `category`、`sort`、`nextCursor`、`items`。

### 官网前端对“完成/毕业”的映射

生产 JS 的 token view-model 映射逻辑等价于：

```js
listed: coin.listed,
stage: coin.listed ? "GRADUATED" : "BONDING",
progress: coin.listed ? 100 : coin.progress
```

该代码说明页面把 `listed=true` 显示为 `GRADUATED`，把 `listed=false` 显示为 `BONDING`；没有将任何字段或 selector 命名为 `completed`。

## 公开请求实测

测试时间：2026-08-20。请求均为只读 `GET`。为模拟官网跨域请求，成功请求带有：

```text
Origin: https://flap.sh
Referer: https://flap.sh/board
Accept: application/json
```

不带这些来源头时，本环境的 Cloudflare 保护返回 `403` HTML；带来源头后接口返回正常 JSON。这是访问环境/防护行为，不应解释为接口需要登录。

### A. 普通 BSC 看板

```text
GET https://bnb.taxed.fun/v3/board?limit=5&_refresh=20260627
```

实测：HTTP `200`，`Content-Type: application/json; charset=utf-8`。

顶层响应字段：

```json
{
  "category": "trending",
  "sort": "volume24h_desc",
  "nextCursor": "934574.86115|0x72e69a60925643c8a31c99a93b5d2f772c257777|17",
  "items": [
    {
      "coin": {"address": "0x...", "name": "...", "symbol": "...", "image": "..."},
      "listed": true,
      "quoteToken": "0x...",
      "price": "...",
      "marketCap": "...",
      "fdv": "...",
      "volume24h": "...",
      "holders": 35075,
      "liquidity": "...",
      "change5m": "...",
      "change1h": "...",
      "change4h": "...",
      "change24h": "...",
      "progress": "100.00",
      "tax": {"hasTax": true, "buyTaxBps": 100, "sellTaxBps": 100},
      "vault": null,
      "isInnovation": false,
      "isLowRisk": false,
      "createdAt": 1786593570
    }
  ]
}
```

实际响应中还出现了 `vault` 对象字段：`vault`、`vaultFactory`、`vaultFactoryCategory`、`vaultFactoryCategoryZh`。`tax` 对非税收代币也可能出现 `hasTax:false` 且税率为 `null`。

该响应同时包含已毕业样本，例如 `listed:true`、`progress:"100.00"`；因此普通看板可作为“当前看板上的已毕业代币”来源，但它不是按 completed 状态命名或保证全量历史的接口。

### B. 发射中/接近毕业看板

```text
GET https://bnb.taxed.fun/v3/board/graduatinghot?limit=5&_refresh=20260627
```

实测：HTTP `200`，JSON 顶层结构仍为 `category`、`sort`、`nextCursor`、`items`。首条样本为 `listed:false`、`progress:"31.73"`，其他样本也为未 listed 的 bonding 状态。

注意：后端对非法 selector 的错误文本只列出 `trending`、`worldcup`、`cat`、`seedAlpha`、`tag=<name>`、`vaultFactories=...`，没有列出 `graduatinghot`；但本次同样请求实际返回 HTTP `200`。这说明 `graduatinghot` 是当前兼容/隐藏 selector，不能据错误文本推断其不存在。

### C. `completed` 路由

```text
GET https://bnb.taxed.fun/v3/board/completed?limit=5&_refresh=20260627
```

实测：HTTP `400`，JSON：

```json
{
  "error": "invalid selector, expected one of: trending, worldcup, cat, seedAlpha, tag=<name>, vaultFactories=0x...,0x..."
}
```

结论：当前公开第一方 API 没有证据表明存在 `/v3/board/completed`。

### D. 目标地址实测

对 `0x64aafe4d6c4436362a2b072c683f2e8c01fe7777` 请求：

```text
GET https://bnb.taxed.fun/v3/coin/0x64aafe4d6c4436362a2b072c683f2e8c01fe7777
```

2026-08-20 从官方服务器读取到的关键字段为：

```json
{
  "address": "0x64aafe4d6c4436362a2b072c683f2e8c01fe7777",
  "name": "国内唯一合法虚拟货币",
  "symbol": "哈夫币",
  "listed": true,
  "marketCap": "88647.000000000000000000",
  "progress": "100.00",
  "pool": "0x8b1b1d88e396e2ae63bffa273829f5d6cd4e4fc6"
}
```

同一时刻 `/v3/board?limit=20` 返回 20 条记录，其中 20 条 `listed=true`，目标地址在第一页出现。该市值会随市场波动，不能用这一次返回值解释此前的 `$500k` 观察；但 `listed=true` 与 `progress=100.00` 直接证明 Flap 自己已经把该地址标记为毕业态。

### E. 其他可能的旧路由

只读探测 `/v2/board`、`/v1/board`、`/api/board`、`/openapi.json`、`/swagger.json`、`/docs`、`/api-docs`、`/v3/openapi.json`、`/v3/docs`，没有发现公开 OpenAPI 文档或另一套 completed 列表契约；受保护端点多数返回 Cloudflare `403`，不能据此断言后端不存在内部路由。

## 可使用的接口契约

### 获取 BSC 当前看板

```text
GET https://bnb.taxed.fun/v3/board
```

推荐参数示例：

```text
?quoteToken=0x0000000000000000000000000000000000000000
&sortBy=marketcap
&order=desc
&limit=20
&_refresh=20260627
```

分页时读取响应 `nextCursor`，再以 URL 编码后作为 `cursor` 传回。不要假定 `limit` 一定等于返回条数；本次返回表现受服务端候选集/分页实现影响，应该以 `items` 实际长度和 `nextCursor` 为准。

### 获取单币第一方详情

官网 JS 还明确使用：

```text
GET https://bnb.taxed.fun/v3/coin/{address}?_refresh=20260627
```

失败时前端回退到：

```text
GET https://bnb.taxed.fun/v2/coin/{address}
```

这个接口用于单个地址详情，不是列表；返回数据可补充状态、税率、池子、价格等字段。

## 对 OdAIly 的含义

- 若目标是**当前看板上的 BSC 新发/发射中代币**，可优先调用 `/v3/board/graduatinghot`，再按 `listed`、`progress` 和业务阈值筛选；它不是官方 `completed` 语义。
- 若目标是**当前看板上已经毕业/上 DEX 的代币**，可调用 `/v3/board`，筛选 `listed=true` 或 `progress="100.00"`；必须明确这是当前看板集合，不是完整历史发射数据库。
- 若目标是**不漏掉全部历史发射**，官方文档给出的可靠来源是 BSC 上 Portal 的 `TokenCreated` 事件索引，再用链上 `getTokenV8`/`getTokenV8Safe` 或详情接口补充状态；不能把 `/v3/board` 当作全量历史源。
- 本调研没有修改代码、生产环境或其他文档，因此上述结论只记录为接口调研，不改变现有实现。

## 证据局限

- 官网与文档是第一方来源；当前 API host `bnb.taxed.fun` 由官网生产 JS 明确配置，但 API 没有公开 OpenAPI/Swagger 文档。
- API 请求对本次命令行环境的无来源请求返回 Cloudflare `403`；使用官网 `Origin`/`Referer` 后能得到 `200 JSON`。后续生产调用应遵循官网前端的来源/缓存行为，并对 403、400 和 schema 变化做容错。
- 没有找到第一方声明“completed = listed/progress=100”的文字定义；`GRADUATED` 映射来自官网前端，`TokenCreated`/链上状态语义来自官方开发者文档。因此文中把 `listed=true` 称为“已毕业/已上 DEX 样本”，没有把它升级为官方 completed 历史全集。
