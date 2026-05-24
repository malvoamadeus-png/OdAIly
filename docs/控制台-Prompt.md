# 控制台-Prompt

## 当前可见模板

- `x_regular_writer`
- `x_onchain_writer`
- `x_funding_writer`
- `mainstream_media_writer`
- `external_media_alert_domain_judge`

## 外媒模板口径

- `mainstream_media_writer` 是当前唯一在用的外媒写作模板
- 控制台应显示为“外媒快讯”
- 它同时服务：
  - `non_mainstream_media` 的统一外媒全文写作
  - 历史兼容 `mainstream_media`

## 隐藏但保留的历史模板

- `non_mainstream_media_writer`

它保留数据库记录与历史版本追溯，但不再出现在控制台模板列表里，也不再被新任务选择。

## 种子文件

- 当前在用：`docs/主流外媒快讯模板.txt` -> `mainstream_media_writer`
- 历史保留：`docs/外媒模板.txt` -> `non_mainstream_media_writer`
- 标题提醒领域判断：`docs/外媒标题领域判断模板.txt` -> `external_media_alert_domain_judge`

## 相关文档

- `编写者1.md`
- `判断者-领域分类.md`
