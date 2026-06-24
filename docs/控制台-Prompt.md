# 控制台-Prompt

## 当前可见模板

- `x_regular_writer`
- `x_onchain_writer`
- `x_funding_writer`
- `mainstream_media_writer`
- `external_media_alert_domain_judge`
- `jin10_judge`

## 外媒与 AI信源模板口径

- `mainstream_media_writer` 是当前唯一在用的外媒和 AI信源写作模板
- 控制台应显示为“外媒快讯”
- 它同时服务：
  - `non_mainstream_media` 的统一外媒全文写作
  - `ai_source` 的 AI信源全文写作
  - 历史兼容 `mainstream_media`

## 隐藏但保留的历史模板

- `non_mainstream_media_writer`

它保留数据库记录与历史版本追溯，但不再出现在控制台模板列表里，也不再被新任务选择。

## 金十判断模板

- `jin10_judge` 显示为“判断者-金十”
- 模板用于 `judge_jin10` 阶段，不用于写作。
- 该模板推荐用自然语言维护主题白名单，不需要编辑固定分类枚举。
- 模型输出只允许 `publish` 或 `discard`；输出结构由后端约束。

## 种子文件

- 当前在用：`docs/主流外媒快讯模板.txt` -> `mainstream_media_writer`
- 历史保留：`docs/外媒模板.txt` -> `non_mainstream_media_writer`
- 标题提醒领域判断：`docs/外媒标题领域判断模板.txt` -> `external_media_alert_domain_judge`
- 金十判断：`docs/判断者-金十模板.txt` -> `jin10_judge`

## 相关文档

- `编写者1.md`
- `判断者-领域分类.md`
- `判断者.md`
- `收集者-金十.md`
