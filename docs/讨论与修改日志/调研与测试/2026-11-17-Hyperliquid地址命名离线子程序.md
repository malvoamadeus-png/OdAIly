# Hyperliquid 地址命名离线子程序

`scripts/preview_hyperliquid_address_names.py` 接收调用方准备好的脱敏 JSON 画像，逐个请求 `gpt-6-luna` 生成地址特征名。脚本不查询产品 SQLite、不启动服务，也不读取链上数据；真实地址和画像范围由输入数据的提供方负责。

## 输入

输入可以是 JSON 数组，也可以是带 `samples` 数组的对象。每个样本必须包含：

- `sample_id`、`address_prefix`、`as_of`、`coverage`
- `assets`、`account_pnl`、`events`、`current_positions`

其中 `assets`、`events`、`current_positions` 是数组，`account_pnl` 是对象。画像条目用 `id` 标识；脚本也兼容 `evidence_id`、`event_id` 和 `evidence_ids`。AI 引用的 ID 会按输入中出现的 ID 校验。`sample_id` 必须唯一，`address_prefix` 必须是至少两位十六进制数字构成的 `0x` 前缀。不得把密钥、完整地址或其他不需要的个人信息放入画像。

## 输出与规则

每个样本独立成功或失败。成功结果包含主名称、1 至 3 个备选名、简短理由、证据 ID 和标题例子。AI 返回特征名、证据与理由；脚本在本地把前缀直接接在特征名后，再按最新事件的净变化或清仓结果组装一条标题样例，金额使用 Decimal 舍入。示例还覆盖大额平仓兑现、浮盈回吐和浮盈扩大；未知净方向不输出动作标题，不替代正式新闻格式层。输出顶层记录请求模型、响应模型（客户端不提供响应模型元数据时为 `null`）和运行秒数；调用或输出校验失败时保留 `status: failed` 与错误信息。

提示词版本 `highlight-name-v6` 要求称谓突出一个有证据的特长，不要求罗列优缺点；将胜场、轮数、持仓单位留在理由中，避免数据摘要式名字和“兑现者”等生硬称呼。多个亮点需比较显著性后再选主名，正面或中性特点优先。观察窗口和全历史、盈利和胜率、历史动作和当前敞口分别处理。提示词要求遵守 PnL freshness、usage 和 `eligible_for_current_total_pnl_copy` 等字段；过期累计 PnL 只能作历史背景。短期胜率称谓须保留“近期”，原油称谓统一简写“原油”，不区分布伦特/WTI；内部证据和动作金额继续按完整合约区分。标题模拟在 `as_of` 时点写作，优先描述净动作或兑现结果；历史时点由运行报告说明，标题不堆“回放/观察窗口”等内部过程词。没有全市场排名数据时不作最大、第一等比较。重试使用同一个模型，默认最多请求两次，每次超时 35 秒。程序只校验结构及引用 ID，不作完整事实审查。

## 运行

脚本从环境变量或 `--env-file` 指定的本地 `.env` 读取 LLM 配置，只接纳 `HL_NAMING_*`、`X_AGENT_OPENAI_*`、`ODAILY_LLM_*`、`OPENAI_*` 和 `LITELLM_MASTER_KEY`。其他键（包括数据库连接配置）不会载入环境。没有显式设置 API 风格时默认 `chat_completions`，与 X Agent 本地脚本一致。执行前确认已提供 API key 和服务端点；脚本不会显示这些配置。

```bash
python scripts/preview_hyperliquid_address_names.py --input /path/to/redacted_profiles.json --output /path/to/name_suggestions.json
```

只校验结构和可用证据 ID，不调用模型：

```bash
python scripts/preview_hyperliquid_address_names.py --input /path/to/redacted_profiles.json --output /path/to/name_dry_run.json --dry-run
```

本轮三样本保留各轮原始结果；逐步调整提示词及标题组装后，以同样输入实际调用同一Luna模型。首轮偏向数字摘要，后续补充简短称谓、时间限定和资产中文名；模型曾给出错误比较理由，因此理由也须人工核查。正式调度、跨次名称缓存与前缀冲突处理未接入，规则见[取数与调度规则](2026-11-17-Hyperliquid地址命名取数与调度规则.md)。

追加五地址试跑使用 `highlight-name-v6`：称谓中布伦特/WTI均简写“原油”，动作及统计仍区分具体合约；新增标普500、英伟达、闪迪展示名，以及浮盈不可写成已兑现盈利的约束。

五地址监督发现小额盈利也可能被模型称为“爆赚”，已补充绝对显著性规则；主名正确不代表备选全部可用。原始输出与人工监督结果分别保存，见[五地址监督试跑](2026-11-17-Hyperliquid追加五地址监督试跑.md)。
