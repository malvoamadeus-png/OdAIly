# Meme速递叙事规范

本文件定义 Meme速递的读者正文契约：最终输出必须是具体炒作角度，不是 Telegram 监控摘要。

没有可用角度时，正文必须为空，任务进入 no_usable_narrative；外部 API、模型、JSON 和最终校验异常不归入该原因，而是持久化阶段诊断并进入重试。

## 最终输出

读者正文只回答：炒作者拿什么来源、字眼、事件、身份或具体说法去炒这个代币。

可以写原始来源确实提到的字眼、事件、人物动作、具体关系，以及社区成员明确提出的新增炒作理由。

不能写：

- 今日在 Telegram 社群出现集中提及。
- 多条消息重复提及、来自多个群组的多名用户。
- 用户情绪、惊叹、价格反应或“真能飞”。
- 税务、官网、行情、机器人卡片、CA 或链接本身。
- 模型自行补充的影响力、传播预期、上涨因果、项目背景或价值判断。

出现频次、群组数和发送者数只用于触发门槛、审计和控制台统计，不得被改写成炒作角度。

## 材料分层

内部审计区分 source_materials、angle_materials 和 supplemental_information。三组都可以为空，不能为了填满某一组而用频次、情绪或模型常识补写。原始字眼或明确人物动作本身已经足够时，角度可以为空。

## 运行顺序

Telegram watcher 负责真人 CA 命中、去重、20 分钟候选触发和任务门槛；最终叙事生成不使用 watcher 的 CA-only 摘要作为唯一输入，而是按 CommunityMonitor V2 对精确 CA 做白名单全历史 Telegram 搜索。程序全局选择最早 20 条和最新 20 条命中，并对每个命中回读前 2 条、后 15 条上下文。机器人在保存前过滤，原话按 `chat + message_id` 去重。

Grok 分两路执行：第一路用精确 CA 搜索最多 30 条 X 原帖，保留原文、作者、链接和时间；第二路只用精确 CA 做独立研究，输出 `source_actions`、`narrative_materials`、`supplemental_information` 和非最终 `type_hypothesis`。最终 writer 只接收这些结构化材料和 Telegram 原话，不接收搜索过程摘要。

两路 Grok 请求共用同一个 xAI/CLIProxyAPI 代理时必须串行占用请求槽；X 搜索和独立研究仍是两个独立阶段，但不能并发打到同一 OAuth 代理账号。阶段请求失败时保留具体诊断并按阶段重试。

meme tg-discover 是白名单维护辅助命令：它使用 Telegram 全局搜索 0x，排除当前白名单实体，输出群组汇总和样本；它不自动修改白名单，也不直接把发现结果写入 Meme 触发库。

## 确定性校验

生成后必须拒绝 Telegram 集中提及、消息重复提及、多个群组或多名用户的统计摘要；也必须拒绝只有情绪反应的句子，以及把税务或官网链接作为炒作理由的句子。

拒绝后不能改写成另一句占位正文，直接按 no_usable_narrative 处理。

## 叙事审计

叙事流程继续复用 `jobs.narrative_json`，不新增表。每次流程至少记录：

- `status`：`success`、`empty` 或 `error`。
- `failure_stage`：`telegram_collection`、`x_ca_collection`、`grok_ca_research`、`telegram_entity_extraction`、`grok_entity_lookup`、`final_writer`、`final_validation` 等。
- `failure_code`、`failure_message`、`material_counts`、`decision_code`、`decision_reason`。
- Telegram 最老/最新命中及每条命中的前 2、后 15 条上下文；X 原帖；Grok `source_actions`、`narrative_materials`、`supplemental_information`、`type_hypothesis`；实体补充、性能和调用诊断。
- 最终 `primary_type`、`source_materials`、`angle_materials`、`supplemental_information`、`used_material_ids`、`discarded_material_ids` 和 `reader_text`。

叙事生成器返回的 `output_path` 必须是字符串路径，并且返回值要包含写入文件的完整审计对象；worker 会把生成结果作为 JSON 写入任务状态，不能返回 `Path` 等非 JSON 类型，也不能用精简的最终正文对象覆盖审计文件。输出文件已经写成功但进程随后返回非零时，worker 仍会按阶段异常重试，因此生成器必须在写文件和返回结果两处都保持成功。

`decision_code` 使用确定性值：`no_materials`、`materials_but_no_type`、`type_selected_but_empty_reader_text`、`writer_returned_empty`、`no_usable_angle`、`final_validation_error`、`completed`。`no_usable_narrative` 只表示最终确实没有可用叙事材料；阶段异常保留具体阶段并触发重试。

控制台列表只读叙事摘要，详情通过 `GET /console/meme/detail?id=<job_id>` 懒加载。详情页默认全部收起，顶层分组单开；Telegram 内部按最老 20 条/最新 20 条分组，单条命中再展开上下文。旧任务没有 `narrative_json` 时显示暂无审计详情，不回补历史材料。

## 本次坏例

以下三类句子都不是最终炒作角度：

1. “TSHIRT 今日在 Telegram 社群出现集中提及，相关消息来自两个群组的多名用户。”这是命中统计和来源数量，不是被炒的具体理由。
2. “Telegram 中多条消息重复提及 bStonkBroker，并出现一条指向税务信息页面的链接。”这是重复计数和链接描述，没有说明具体字眼、事件或关系。
3. “群聊 A 表示它是‘知了’，也说‘就是大金啊’；还有人感叹‘卧槽真能飞啊’。”其中只有明确字眼可能成为材料；后半句只是用户反应，不能作为炒作理由。

正确处理是：从上下文中的原话提取明确的字眼、梗的来历、人物动作、事件或具体关系；如果提取不到这些内容，正文留空。`telegram_contexts`、`telegram_messages`、`x_posts` 和 `grok_research` 必须保留在审计 JSON，便于复核最终正文使用了什么输入。
