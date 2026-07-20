# 控制台-Prompt及规则管理

控制台左侧和页面标题显示为 `Prompt及规则管理`。页面分为 `可编辑 Prompt`、`内置规则` 与 `知名人物` 三个标签。`可编辑 Prompt` 保持现有版本发布和特色模式编辑能力；`内置规则` 通过认证接口 `GET /console/runtime-rules/get` 展示运行时代码中的来源判断 Prompt / Schema、排除词路径语义、标题策略、当前知名人物名单、编写者2替换与拦截、发布者默认规则和来源映射；`知名人物` 用于维护写作标题可使用 `人名：观点` 规则的姓名名单。

内置规则支持按模块筛选、全文搜索与展开原文，并显示代码位置及“代码只读 / 控制台可编辑”状态。接口内容从代码常量和当前知名人物配置实时组装，不读取或返回环境变量、数据库连接串、API key 或其他密钥。

## 知名人物

知名人物配置通过认证后台接口维护：

- `GET /console/known-title-subjects/get`
- `POST /console/known-title-subjects/save`

保存内容是 Linux 本地配置文件 `data/config/known_title_subjects.json`，路径可由 `KNOWN_TITLE_SUBJECTS_CONFIG_PATH` 覆盖；Supabase 不作为运行时读取源。默认种子为 `Rune、Cobie、Vitalik、CZ`。前端编辑区允许使用顿号、换行、逗号、分号分隔姓名；保存时后端统一去空格、去空项并按大小写不敏感去重。运行时规则页只展示简洁姓名名单，不展示逐人标题情景或复杂 JSON。

编写者1在写作阶段按需读取本地配置文件，只会把当前材料实际命中的姓名注入 Prompt，并使用通用规则提示：`当前材料命中知名人物：CZ、Vitalik。遇到这些人物观点时可使用“人名：观点”标题。`

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
