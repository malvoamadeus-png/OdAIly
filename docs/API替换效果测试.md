# API 替换效果测试

> 本文是 2026-07-14 的历史测试记录，保留当时直连 DeepSeek / GPT relay 的测试参数。当前生产文本 LLM 已统一经本机 LiteLLM proxy 调用，生产配置以 `README.md`、`.env.example` 和各模块文档中的 `odaily-gpt-writer`、`odaily-deepseek-fast`、`odaily-deepseek-review`、`odaily-deepseek-auditor` 业务别名为准。

## 背景

2026-07-14 对判断者和审核者做一次 DeepSeek API 替换效果测试。目标是降低模型调用耗时，同时保留原 GPT API 代码和配置能力，便于效果不好时快速回滚。

本次替换只影响：

- 判断者：`judge_crypto`、`judge_ai`、`judge_jin10`。
- 审核者：`auditor-worker`。

不影响：

- 编写者1 / 编写者2。
- 发布者。
- 搜索者里的 embedding 和 AI 复核。
- 外媒标题领域判断。

2026-07-14 补充修正：初次上线时 `X_PROCESS_JUDGE_MODEL=deepseek-chat` 被搜索者 AI 复核复用，导致搜索阶段把 `deepseek-chat` 发到原 GPT relay 并出现 `model_not_found`。已将搜索 AI 复核拆成独立配置 `SEARCH_AI_REVIEW_MODEL` / `SEARCH_AI_REVIEW_REASONING_EFFORT`，并取消外媒领域判断、竞品事件复核对 `X_PROCESS_JUDGE_MODEL` 的隐式 fallback。

## 测速结果

### 测试1：判断者

使用 `X_JUDGE_PROMPT_TEMPLATE` 和判断者 JSON 输出结构，5 个自选案例覆盖 `regular`、`onchain`、`funding`、`discard`。

| 案例 | 历史 GPT 快速方案 | DeepSeek 非思考 | DeepSeek 快多少 | 判断一致性 |
| --- | ---: | ---: | ---: | --- |
| `regular_exchange_listing` | 4.949s | 0.758s | 6.53x | 一致 |
| `onchain_whale_transfer` | 8.253s | 0.867s | 9.52x | 一致 |
| `funding_round` | 4.561s | 0.693s | 6.58x | 一致 |
| `discard_pure_emotion` | 4.920s | 0.875s | 5.62x | 一致 |
| `discard_baseless_call` | 3.692s | 0.762s | 4.85x | 一致 |

汇总：

- 原方案平均耗时：5.275s。
- DeepSeek 平均耗时：0.791s。
- 平均速度倍率：6.67x。

### 测试2：审核者

使用审核者 Prompt 和 `AUDITOR_SCHEMA`，从历史 `auditor_checks.status = 'failed'` 中挑 5 条源内容。原方案按审核者默认 `gpt-5.5 + medium` 测试；DeepSeek 当时用 `deepseek-chat` 非思考模式并把 JSON Schema 约束追加到 prompt；当前生产审核者改为 `odaily-deepseek-auditor` 思考模式。

| auditor_check_id | 标题 | 原方案耗时 | DeepSeek 耗时 | DeepSeek 快多少 | 最终结果 |
| ---: | --- | ---: | ---: | ---: | --- |
| 7381 | 伊朗外长：美国在谅解备忘录中承诺不发动战争 | 5.476s | 1.085s | 5.05x | 两边均无问题 |
| 7267 | 福布斯：SpaceX IPO将造出括推特联创在内的9位亿万富豪 | 13.290s | 1.344s | 9.89x | 两边均识别标题漏字 |
| 4996 | MiniMax：拟于科创板上市 | 5.830s | 0.964s | 6.05x | 两边均无问题 |
| 4552 | 拉哪AI启动第二阶段测试，新增美股资产策略 | 4.318s | 1.006s | 4.29x | 两边均无问题 |
| 3620 | Bitmine过去一周买入111942枚ETH，总资产及持仓达123亿美元 | 9.600s | 0.855s | 11.23x | 两边均无问题 |

汇总：

- 原审核者方案平均耗时：7.703s。
- DeepSeek 平均耗时：1.051s。
- 平均速度倍率：7.33x。

注意：DeepSeek 只开 JSON object 模式但不追加审核者 JSON Schema 时，SpaceX 案例会漏 `severity/type/location` 等字段，现有解析器会过滤为无问题。因此审核者切 DeepSeek 必须开启 `AUDITOR_APPEND_JSON_SCHEMA_TO_PROMPT=true`。

## 本次替换方案

代码保持原 GPT API 路径不变，新增阶段级覆盖配置：

- 判断者使用 `X_PROCESS_JUDGE_OPENAI_*` 覆盖，仅判断阶段生效。
- 审核者使用 `AUDITOR_OPENAI_*` 覆盖，仅审核者服务生效。
- 全局 `OPENAI_API_KEY`、`X_PROCESS_OPENAI_BASE_URL`、写作和发布模型配置继续保留。

生产测试配置：

```dotenv
DEEPSEEK_API_KEY=<服务器已有或新增的 DeepSeek key>

X_PROCESS_JUDGE_MODEL=deepseek-chat
X_PROCESS_JUDGE_REASONING_EFFORT=high
X_PROCESS_JUDGE_OPENAI_BASE_URL=https://api.deepseek.com
X_PROCESS_JUDGE_OPENAI_API_STYLE=chat_completions
X_PROCESS_JUDGE_OMIT_REASONING_EFFORT=true
X_PROCESS_JUDGE_CHAT_RESPONSE_FORMAT_MODE=json_object
X_PROCESS_JUDGE_APPEND_JSON_SCHEMA_TO_PROMPT=true

AUDITOR_MODEL=odaily-deepseek-auditor
AUDITOR_REASONING_EFFORT=max
AUDITOR_OPENAI_BASE_URL=
AUDITOR_OPENAI_API_STYLE=
AUDITOR_OMIT_REASONING_EFFORT=false
AUDITOR_CHAT_RESPONSE_FORMAT_MODE=json_object
AUDITOR_APPEND_JSON_SCHEMA_TO_PROMPT=true

SEARCH_AI_REVIEW_MODEL=deepseek-chat
SEARCH_AI_REVIEW_REASONING_EFFORT=low
SEARCH_AI_REVIEW_OPENAI_BASE_URL=https://api.deepseek.com
SEARCH_AI_REVIEW_OPENAI_API_STYLE=chat_completions
SEARCH_AI_REVIEW_OMIT_REASONING_EFFORT=true
SEARCH_AI_REVIEW_CHAT_RESPONSE_FORMAT_MODE=json_object
SEARCH_AI_REVIEW_APPEND_JSON_SCHEMA_TO_PROMPT=true
```

说明：

- `X_PROCESS_JUDGE_REASONING_EFFORT=high` 和 `AUDITOR_REASONING_EFFORT=max` 保留为本次效果测试的配置标签。
- `SEARCH_AI_REVIEW_REASONING_EFFORT=low` 继续保留为搜索复核的业务标签；当接口不接收 reasoning 参数时，由 `SEARCH_AI_REVIEW_OMIT_REASONING_EFFORT=true` 在请求层省略。
- 审核者 DeepSeek 思考模式会发送 `AUDITOR_REASONING_EFFORT=max`；判断者与搜索者复核仍可通过 `*_OMIT_REASONING_EFFORT=true` 省略 reasoning 参数。
- `*_CHAT_RESPONSE_FORMAT_MODE=json_object` 配合 `*_APPEND_JSON_SCHEMA_TO_PROMPT=true`，用于兼容 DeepSeek chat completions 的结构化输出。

## 回滚方案

如果判断者或审核者效果不好，保留代码不动，只回滚服务器 `.env`：

```dotenv
X_PROCESS_JUDGE_MODEL=odaily-deepseek-review
X_PROCESS_JUDGE_REASONING_EFFORT=low
X_PROCESS_JUDGE_OPENAI_BASE_URL=
X_PROCESS_JUDGE_OPENAI_API_STYLE=
X_PROCESS_JUDGE_OMIT_REASONING_EFFORT=false
X_PROCESS_JUDGE_CHAT_RESPONSE_FORMAT_MODE=json_schema
X_PROCESS_JUDGE_APPEND_JSON_SCHEMA_TO_PROMPT=false

AUDITOR_MODEL=gpt-5.5
AUDITOR_REASONING_EFFORT=medium
AUDITOR_OPENAI_BASE_URL=
AUDITOR_OPENAI_API_STYLE=
AUDITOR_OMIT_REASONING_EFFORT=false
AUDITOR_CHAT_RESPONSE_FORMAT_MODE=json_schema
AUDITOR_APPEND_JSON_SCHEMA_TO_PROMPT=false
```

然后重启：

```bash
systemctl restart odaily-local-pipeline.service odaily-auditor.service
```

## 观察指标

- `judge_failed`、`audit failed` 是否下降，尤其是超时、空响应、缺字段。
- 判断者的 `route` 分布是否异常偏向 `discard` 或某一类。
- 审核者 `flagged` 比例是否异常升高或降低。
- 信息流插件里审核者提醒的接受 / 拒绝反馈。
- `pipeline_worker_heartbeats` 中 `local_pipeline`、`auditor` 是否持续正常。

## 性能与复杂度权衡

1. 当前方案最大的性能瓶颈：原 GPT 判断者和审核者在高 reasoning 或长内容下耗时明显，审核者历史失败样本可超过 13 秒。
2. 数据量扩大 100 倍后最先出现的问题：模型调用耗时会导致判断者和发布后审核队列堆积，恢复补扫时尤其明显。
3. 可优化方案及预期收益：先把判断者切到 DeepSeek 非思考模式，审核者切到 DeepSeek 思考模式，按当时非思考测试预期模型调用耗时降到约 1 秒；边界样本后续可再设计 GPT 复核。
4. 当前 Trade-off：DeepSeek 速度优势明显，接入成本低；但样本量有限，正式长期使用前应继续观察 24-72 小时真实流量下的误判、误报和结构化输出稳定性。
