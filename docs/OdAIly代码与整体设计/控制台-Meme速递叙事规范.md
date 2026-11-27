# Meme速递叙事规范

本文件定义 Meme速递的读者正文契约：最终输出必须是具体炒作角度，不是 Telegram 监控摘要。

两个栏目共用的固定正文结构、材料取舍、Grok整合与文本外壳见 [Meme栏目共用写作框架](Meme栏目共用写作框架草案.md)。`primary_type` 仅作内部分类，不再决定正文段落结构。

没有可用角度时，正文必须为空，任务进入 no_usable_narrative；外部 API、模型、JSON 和最终校验异常不归入该原因，而是持久化阶段诊断并进入重试。

## 最终输出

读者正文使用固定两句式：第一句简要说清代币关联的梗、人物、事件、活动或项目；第二句以 `核心逻辑是` 开头，说明材料支持的炒作逻辑，并合并最相关的传播、参与或催化事实及其归属。第二句之后不得再重复“代币借用的炒作点是……”等同义总结。

Meme名称或起源可以用一个短分句交代，不展开无关背景。歌词含义、情绪感受、项目定义或人物动作本身不能自动替代核心逻辑；如果某个直接字眼或人物动作本身就是炒作理由，第二句应明确说明它为什么构成该逻辑。人物仅表示将采取行动时，不得写成行动已经发生。

程序在最终校验后统一添加固定开头和结尾。结尾必须是：“「Meme 速递」由 Odaily 独家 AI 模型筛选社区热议潜力标的。内容基于公开信息整理，不构成投资建议，请自行甄别并注意 Meme 币高波动风险。”旧版免责声明会在归一化时替换为该文案。

不能写：

- 今日在 Telegram 社群出现集中提及。
- 多条消息重复提及、来自多个群组的多名用户。
- 用户情绪、惊叹、价格反应或“真能飞”。
- 税务、官网、行情、机器人卡片、CA 或链接本身。
- 模型自行补充的影响力、传播预期、上涨因果、项目背景或价值判断。

出现频次、群组数和发送者数只用于触发门槛、审计和控制台统计，不得被改写成炒作角度。

## 材料分层

内部审计区分 source_materials、angle_materials 和 supplemental_information。三组都可以为空，不能为了填满某一组而用频次、情绪或模型常识补写。材料按正文作用分类，不按工具或来源分类。`angle_materials` 可以为空，但非空正文仍须写出“核心逻辑是……”；若直接字眼或人物动作本身已构成理由，就在第二句明确写出这一关系。

正文归属按来源区分：Telegram 使用“群聊 A 表示/提到……”或“多个群聊表示……”；X 使用“X 上有人/多名用户表示/提到……”；FOMO Thesis 材料在正文中统一匿名写作“某信源表示……”，不得出现 FOMO、Thesis、产品名、作者名或账号名。社区说法不能被改写为未经归属的确定事实。

## 运行顺序

Telegram watcher 负责真人 CA 命中、去重、20 分钟候选触发和任务门槛；最终叙事材料不直接复用 watcher 的触发摘要。OdAIly 本地 collector 使用独立 Telegram session，按精确 CA 在白名单中检索上下文，只将精确 CA 命中的具体消息作为写作材料；上下文完整保留在审计中。

FxTwitter、Telegram 与 FOMO Thesis 由 OdAIly 本地快速材料模块并行采集，保持 `2026-09-01` 版本化 `evidence` bundle，不经过 HideOnBush HTTP 接口或内部密钥。Telegram 和 FxTwitter 不启动浏览器。FOMO 只在实际叙事任务中取得共享浏览器锁、使用独立运行时 profile 启动一次 Playwright Chromium，并由页面自有模块取得当次认证；采集完成或异常时始终关闭 context/browser。每次启动前都会检查本地材料总时限；若 Playwright 初始化已耗尽时限，则不再拉起 Chromium。权限 `0700` 的服务器本地 profile 仅由浏览器保存认证状态供下次按需使用；应用不读取、打印、导出或写入 Cookie、JWT、用户身份和上游原始响应。

FOMO profile 缺失、被重定向到登录页或收到明确认证失败时，source diagnostic 记录 `code=login_required`。已有 Telegram/X 材料时，该路只是 `partial`，仍可写作；三路没有正文时，任务每 5 分钟进入一次 `retry_wait`，直至普通 1 小时队列时限到期，并发送冷却后的 Telegram 运维提醒。不会驻留 FOMO 浏览器、daemon、systemd service 或公开远程调试端口；收到提醒后由值班人通过一次性 `meme fomo-login` 维护命令更新服务器运行时 profile。该命令在 `xvfb-run` 中短暂启动浏览器，自动点击唯一可识别的 FOMO 登录、Google 登录及已登录 Google 账号选项；验证码、身份挑战、多个账号或页面结构变化交由值班人通过 loopback DevTools 和 SSH 本地转发接管。DevTools 仅绑定 `127.0.0.1`；profile、Cookie 和 JWT 不得复制、打印或提交。

两个栏目同时请求Grok Bot Routine；请求持久化并以15秒间隔非阻塞读取回调，最多等待5分钟，自动回调登记同步有效5分钟；失败或超时后使用本地材料继续写作。Grok继续作为跨平台调研来源，但不再强制单独成段。写作者优先消费其 `what_it_is`、`primary_narrative`、`narratives`、`evidence` 和 `caveats`，并与本地材料按炒作逻辑合并；仅这些结构化字段缺失时才使用旧版 `summary`。重复或无关信息可以省略。采用的事实优先归因到Grok结果中的原始来源，没有原始来源信息时归因于Grok调研。

OdAIly 使用 `gpt-6-luna` 对本地材料及可用Grok研究结果做一次结构化分类与写作，输出 `source_material_ids`、`angle_material_ids`、`supplemental_information_ids`、使用/丢弃材料和正文的 `what_it_is`、`core_logic` 字段。程序以这两个字段固定组装两句正文。Grok整个研究结果作为一项材料传入，保留其对象说明、叙事、证据及 caveats；只有结构化字段缺失时才把旧版 `summary` 当作研究材料。该链路通过Webhook调用Grok Bot，不调用Grok X Search、Grok实体补充或GMGN叙事。GMGN仅用于今日热门Meme榜单发现。

meme tg-discover 是白名单维护辅助命令：它使用 Telegram 全局搜索 0x，排除当前白名单实体，输出群组汇总和样本；它不自动修改白名单，也不直接把发现结果写入 Meme 触发库。

## 确定性校验

生成后必须拒绝 Telegram 集中提及、消息重复提及、多个群组或多名用户的统计摘要；也必须拒绝只有情绪反应的句子，以及把税务或官网链接作为炒作理由的句子。固定正文结构由写作 Prompt 约束；确定性校验继续负责材料 ID、来源归属和格式契约，不声称能自动判断炒作逻辑质量。

拒绝后不能改写成另一句占位正文，直接按 no_usable_narrative 处理。

## 叙事审计

叙事流程继续复用 `jobs.narrative_json`，不新增表。每次流程至少记录：

- `status`：`success`、`empty` 或 `error`。
- `failure_stage`：`local_fast_evidence`、`final_writer`、`final_validation`。
- `failure_code`、`failure_message`、`material_counts`、`decision_code`、`decision_reason`。
- OdAIly 本地 collector 返回的 Telegram、X、FOMO Thesis 规范化材料、三路安全错误码与性能诊断。
- `grok_routine`：请求ID、状态、开始时间和回传原始JSON；`discovery`：榜单命中条件与首次入选时间。
- 最终 `primary_type`、`source_materials`、`angle_materials`、`supplemental_information`、`used_material_ids`、`discarded_material_ids` 和 `reader_text`。

叙事生成器返回的 `output_path` 必须是字符串路径，并且返回值要包含写入文件的完整审计对象；worker 会把生成结果作为 JSON 写入任务状态，不能返回 `Path` 等非 JSON 类型，也不能用精简的最终正文对象覆盖审计文件。输出文件已经写成功但进程随后返回非零时，worker 仍会按阶段异常重试，因此生成器必须在写文件和返回结果两处都保持成功。

`decision_code` 使用确定性值：`no_materials`、`materials_but_no_type`、`type_selected_but_empty_reader_text`、`writer_returned_empty`、`no_usable_angle`、`final_validation_error`、`completed`。`no_usable_narrative` 只表示最终确实没有可用叙事材料；阶段异常保留具体阶段并触发重试。

控制台列表只读叙事摘要，详情通过 `GET /console/meme/detail?id=<job_id>` 懒加载。详情页默认全部收起，顶层分组单开；Telegram 内部按最老 20 条/最新 20 条分组，单条命中再展开上下文。旧任务没有 `narrative_json` 时显示暂无审计详情，不回补历史材料。

## 本次坏例

以下句子都不是完整的最终正文：

1. “TSHIRT 今日在 Telegram 社群出现集中提及，相关消息来自两个群组的多名用户。”这是命中统计和来源数量，不是被炒的具体理由。
2. “Telegram 中多条消息重复提及 bStonkBroker，并出现一条指向税务信息页面的链接。”这是重复计数和链接描述，没有说明具体字眼、事件或关系。
3. “群聊 A 表示它是‘知了’，也说‘就是大金啊’；还有人感叹‘卧槽真能飞啊’。”其中只有明确字眼可能成为材料；后半句只是用户反应，不能作为炒作理由。

正文应先用一句说明“是什么”，再以“核心逻辑是……”说明炒作理由。以现有「慌啥子嘛」材料为例，可写：“「慌啥子嘛」取自赵本山演唱的《慌啥子嘛，慢慢来》。核心逻辑是抖音二创带来的中文梗传播，X 上有用户称歌曲出现大量二创，也有人将其传播与「牛来」「中国人会飞」相提并论。”不要再追加“这枚代币借用的炒作点是……”的重复句。

以 SWJ 材料为例，可先说明它围绕要求黄仁勋回归英伟达的请愿，再以“核心逻辑是……”写材料支持的参与或催化信息。马斯克“表示会签署”必须保留为意向；用户对 CZ 可能签名的猜测不能写成实际参与。没有签名人数或已参与者名单时，不得补出具体规模。

`fast_evidence`、`telegram_messages`、`x_posts` 和 `fomo_materials` 必须保留在审计 JSON，便于复核最终正文使用了什么输入。
