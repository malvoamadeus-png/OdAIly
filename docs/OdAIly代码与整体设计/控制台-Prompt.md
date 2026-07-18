# 控制台-Prompt

Prompt 页面分为 `可编辑 Prompt` 与 `内置规则` 两个标签。前者保持现有版本发布和特色模式编辑能力；后者通过认证接口 `GET /console/runtime-rules/get` 只读展示运行时代码中的来源判断 Prompt / Schema、排除词路径语义、标题策略、知名主体、编写者2替换与拦截、发布者默认规则和来源映射。

内置规则支持按模块筛选、全文搜索与展开原文，并显示代码位置及“代码只读 / 控制台可编辑”状态。接口内容从代码常量实时组装，不读取或返回环境变量、数据库连接串、API key 或其他密钥。

## 当前可见模板

- `x_regular_writer`
- `x_onchain_writer`
- `x_funding_writer`
- `mainstream_media_writer`
- `external_media_alert_domain_judge`
- `jin10_judge`

## Crypto信源与 AI信源模板口径

- `mainstream_media_writer` 服务 `non_mainstream_media` 的统一 Crypto信源全文写作，以及历史兼容 `mainstream_media`。
- `mainstream_media_writer` 同时服务 `tasks.source = 'ai_source'` 的 AI信源全文写作。
- 标记为 `AI信源` 的 X 账号保留后仍使用 `x_regular_writer`，以保持 X 发言人写作口径一致。

## 隐藏但保留的历史模板

- `non_mainstream_media_writer`
- `ai_source_writer`

它保留数据库记录与历史版本追溯，但不再出现在控制台模板列表里，也不再被新任务选择。

## 金十判断模板

- `jin10_judge` 显示为“判断者-金十”
- 模板用于 `judge_jin10` 阶段，不用于写作。
- 该模板推荐用自然语言维护主题白名单，不需要编辑固定分类枚举。
- 模型输出只允许 `publish` 或 `discard`；输出结构由后端约束。

## 种子文件

- Crypto信源全文：`docs/OdAIly代码与整体设计/主流外媒快讯模板.txt` -> `mainstream_media_writer`
- AI信源全文：`docs/OdAIly代码与整体设计/主流外媒快讯模板.txt` -> `mainstream_media_writer`
- 历史保留：`docs/OdAIly代码与整体设计/外媒模板.txt` -> `non_mainstream_media_writer`
- 标题提醒领域判断：`docs/OdAIly代码与整体设计/外媒标题领域判断模板.txt` -> `external_media_alert_domain_judge`
- 金十判断：`docs/OdAIly代码与整体设计/判断者-金十模板.txt` -> `jin10_judge`

## 相关文档

- `编写者1.md`
- `判断者-领域分类.md`
- `判断者.md`
- `收集者-金十.md`
