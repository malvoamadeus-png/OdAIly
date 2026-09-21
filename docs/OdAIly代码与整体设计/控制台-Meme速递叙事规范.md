# Meme速递叙事规范

本文件定义 Meme速递的读者正文契约：最终输出必须是具体炒作角度，不是 Telegram 监控摘要。

没有可用角度时，正文必须为空，任务进入 no_usable_narrative；外部 API、模型、JSON 和最终校验异常不归入该原因，而是持久化阶段诊断并进入重试。

## 最终输出

读者正文只回答：炒作者拿什么来源、字眼、事件、身份或具体说法去炒这个代币。

可以写原始来源确实提到的字眼、事件、人物动作、具体关系，以及社区成员明确提出的新增炒作理由。

程序在最终校验后统一添加固定开头和结尾。结尾必须是：“「Meme 速递」由 Odaily 独家 AI 模型筛选社区热议潜力标的。内容基于公开信息整理，不构成投资建议，请自行甄别并注意 Meme 币高波动风险。”旧版免责声明会在归一化时替换为该文案。

不能写：

- 今日在 Telegram 社群出现集中提及。
- 多条消息重复提及、来自多个群组的多名用户。
- 用户情绪、惊叹、价格反应或“真能飞”。
- 税务、官网、行情、机器人卡片、CA 或链接本身。
- 模型自行补充的影响力、传播预期、上涨因果、项目背景或价值判断。

出现频次、群组数和发送者数只用于触发门槛、审计和控制台统计，不得被改写成炒作角度。

## 材料分层

内部审计区分 source_materials、angle_materials 和 supplemental_information。三组都可以为空，不能为了填满某一组而用频次、情绪或模型常识补写。原始字眼或明确人物动作本身已经足够时，角度可以为空。

正文归属按来源区分：Telegram 使用“群聊 A 表示/提到……”或“多个群聊表示……”；X 使用“X 上有人/多名用户表示/提到……”；FOMO Thesis 材料在正文中统一匿名写作“某信源表示……”，不得出现 FOMO、Thesis、产品名、作者名或账号名。社区说法不能被改写为未经归属的确定事实。

## 运行顺序

Telegram watcher 负责真人 CA 命中、去重、20 分钟候选触发和任务门槛；最终叙事材料不直接复用 watcher 的触发摘要。OdAIly 本地 collector 使用独立 Telegram session，按精确 CA 在白名单中检索上下文，只将精确 CA 命中的具体消息作为写作材料；上下文完整保留在审计中。

FxTwitter、Telegram 与 FOMO Thesis 由 OdAIly 本地快速材料模块并行采集，保持 `2026-09-01` 版本化 `evidence` bundle，不经过 HideOnBush HTTP 接口或内部密钥。Telegram 和 FxTwitter 不启动浏览器。FOMO 只在实际叙事任务中取得共享浏览器锁、使用独立运行时 profile 启动一次 Playwright Chromium，并由页面自有模块取得当次认证；采集完成或异常时始终关闭 context/browser。每次启动前都会检查本地材料总时限；若 Playwright 初始化已耗尽时限，则不再拉起 Chromium。权限 `0700` 的服务器本地 profile 仅由浏览器保存认证状态供下次按需使用；应用不读取、打印、导出或写入 Cookie、JWT、用户身份和上游原始响应。

FOMO profile 缺失、被重定向到登录页或收到明确认证失败时，source diagnostic 记录 `code=login_required`。已有 Telegram/X 材料时，该路只是 `partial`，仍可写作；三路没有正文时，任务每 5 分钟进入一次 `retry_wait`，直至普通 1 小时队列时限到期，并发送冷却后的 Telegram 运维提醒。不会驻留 FOMO 浏览器、daemon、systemd service 或公开远程调试端口；收到提醒后由值班人通过一次性 `meme fomo-login` 维护命令更新服务器运行时 profile。该命令在 `xvfb-run` 中短暂启动浏览器，DevTools 仅绑定 `127.0.0.1`，只能经 SSH 本地转发访问；profile、Cookie 和 JWT 不得复制、打印或提交。

OdAIly 使用 `gpt-5.6-terra` 对三路材料做一次结构化写作，继续输出 `source_material_ids`、`angle_material_ids`、`supplemental_information_ids`、使用/丢弃材料和正文。该链路不调用 Grok、Grok X Search、Grok 实体补充或 GMGN。

meme tg-discover 是白名单维护辅助命令：它使用 Telegram 全局搜索 0x，排除当前白名单实体，输出群组汇总和样本；它不自动修改白名单，也不直接把发现结果写入 Meme 触发库。

## 确定性校验

生成后必须拒绝 Telegram 集中提及、消息重复提及、多个群组或多名用户的统计摘要；也必须拒绝只有情绪反应的句子，以及把税务或官网链接作为炒作理由的句子。

拒绝后不能改写成另一句占位正文，直接按 no_usable_narrative 处理。

## 叙事审计

叙事流程继续复用 `jobs.narrative_json`，不新增表。每次流程至少记录：

- `status`：`success`、`empty` 或 `error`。
- `failure_stage`：`local_fast_evidence`、`final_writer`、`final_validation`。
- `failure_code`、`failure_message`、`material_counts`、`decision_code`、`decision_reason`。
- OdAIly 本地 collector 返回的 Telegram、X、FOMO Thesis 规范化材料、三路安全错误码与性能诊断。
- 最终 `primary_type`、`source_materials`、`angle_materials`、`supplemental_information`、`used_material_ids`、`discarded_material_ids` 和 `reader_text`。

叙事生成器返回的 `output_path` 必须是字符串路径，并且返回值要包含写入文件的完整审计对象；worker 会把生成结果作为 JSON 写入任务状态，不能返回 `Path` 等非 JSON 类型，也不能用精简的最终正文对象覆盖审计文件。输出文件已经写成功但进程随后返回非零时，worker 仍会按阶段异常重试，因此生成器必须在写文件和返回结果两处都保持成功。

`decision_code` 使用确定性值：`no_materials`、`materials_but_no_type`、`type_selected_but_empty_reader_text`、`writer_returned_empty`、`no_usable_angle`、`final_validation_error`、`completed`。`no_usable_narrative` 只表示最终确实没有可用叙事材料；阶段异常保留具体阶段并触发重试。

控制台列表只读叙事摘要，详情通过 `GET /console/meme/detail?id=<job_id>` 懒加载。详情页默认全部收起，顶层分组单开；Telegram 内部按最老 20 条/最新 20 条分组，单条命中再展开上下文。旧任务没有 `narrative_json` 时显示暂无审计详情，不回补历史材料。

## 本次坏例

以下三类句子都不是最终炒作角度：

1. “TSHIRT 今日在 Telegram 社群出现集中提及，相关消息来自两个群组的多名用户。”这是命中统计和来源数量，不是被炒的具体理由。
2. “Telegram 中多条消息重复提及 bStonkBroker，并出现一条指向税务信息页面的链接。”这是重复计数和链接描述，没有说明具体字眼、事件或关系。
3. “群聊 A 表示它是‘知了’，也说‘就是大金啊’；还有人感叹‘卧槽真能飞啊’。”其中只有明确字眼可能成为材料；后半句只是用户反应，不能作为炒作理由。

正确处理是：从三路材料中提取明确的字眼、梗的来历、人物动作、事件或具体关系；如果提取不到这些内容，正文留空。`fast_evidence`、`telegram_messages`、`x_posts` 和 `fomo_materials` 必须保留在审计 JSON，便于复核最终正文使用了什么输入。
