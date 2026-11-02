# 控制台-AI审阅

## 边界

AI 审阅是控制台一级工作区，包含事件复盘和 X Agent 账号候选。它只复用主 SQLite、HotTopic SQLite 和现有 X Capture 采集结果，不发起 X 抓取、外部网页搜索或发布操作。

AI 审阅队列、证据快照、人工决定和用量记录保存在 **data/runtime/ai_review.sqlite**。主 SQLite 和 hottopic.sqlite 对候选扫描只读。人工认可或驳回只作为后续同类判断的有限示例，不写入 X Capture 或 HotTopic 账号目录；账号由管理员在既有管理界面手动维护。

## 每日运行

odaily-hottopic.service 的既有轮询在北京时间每天 06:00 后检查审阅任务，不增加常驻 worker。事件和账号发现都回看最近 3 天；稳定项目键阻止重复入队。超过单批 500 条的分析会在后续轮询继续，直到队列完成或触及当日 token 上限。AI 审阅自己的 SQLite 保存扫描日期、候选、决定和预算，进程重启不会重新分析已完成建议。

账号扫描合并主 SQLite tasks 和 HotTopic inbox 中最近 3 天的 X 原帖，只纳入仍处于跟踪状态且至少打开一个 X Agent 模块的账号。Inbox 只使用跟踪作者自己的正文，跳过纯转发，引用帖不拼入被引用人的正文。标的词表从近期 x_agent_sentiment_results 和 x_agent_project_observations 维护；英文 ticker 使用完整词边界匹配，例如 BTC 不匹配 BTCXYZ。市场情绪候选要求至少 3 个不同已知标的各自出现作者态度表达。项目推介候选要求至少 3 条不同的 Crypto/链上原帖证据，且至少一条包含项目逻辑表达。已打开对应 X Agent 订阅的账号不重复推荐。

事件候选只来自已归并 newsflash_events 和 newsflash_event_sources，且必须包含至少一个当前未追踪的 X 账号线索；没有后续可手动评估的账号线索时不入队。已追踪域名（包括 Cointelegraph、PANews 等）会从线索中排除，域名不单独触发事件入队。Hyperbot 来源的事件全部跳过。

- 竞品先发布、Odaily 随后由 AI 发布。按发布任务状态标出 auto_published 直发或 ready_review 挂后台；没有可关联的竞品来源 AI 发布任务时不入队。
- 至少两家不同竞品发布、没有 Odaily 来源。
- 只有 Odaily 来源。

事件 AI 只读取已有来源原文，判断是否值得持续跟踪主题或信源，而不是判断单条新闻是否重要。一次性申领通知、单次上线或孤立观点不能单独支持长期追踪建议。按“优先考虑 / 可考虑 / 不建议跟进”给出判断和理由；“不建议跟进”和尚未完成分析的候选不显示在审阅队列。来源正文中的 X handle 与 URL 域名使用正则提取，线索仅供人工核对，不提供添加账号操作；网站域名仅作提示。

## 用量与失败

账号挖掘和事件研究分别每日最多 **50,000,000 tokens**。分析前按本地 token 估算和输出上限预留预算；接口返回 usage 时按输入、输出 token 结算，缺失时估算。预算耗尽的队列保留到下一北京时间日期，不丢候选。默认模型为 gpt-6-luna，思考等级为 none；模型路由或返回失败时建议仍留在队列并记录失败，最多尝试三次。

同类别最多两条人工决定可作为下一次提示示例；不自动改规则或执行微调。AI 不会更改事件、账号配置或发布状态。

## 接口

所有操作使用管理员会话保护的 POST /console/ai-review：

- action=list：按 type、category、status、offset 和 limit 分页读取；同一响应含当日账号与事件预算。
- action=detail：按稳定 item_key 读取一项建议和证据。
- action=decide：事件标为 accepted、rejected 或 deferred；账号建议可标为 rejected。

建议状态为 pending / accepted / rejected / deferred；分析状态另存 queued / completed / failed。事件可认可、驳回或暂缓；账号建议可认可或驳回。认可只记录反馈，不修改账号配置；不存在 AI 审阅账号采纳接口。重复扫描按稳定候选键幂等。

事件复盘决定只作为同类后续判断示例，不会写入快讯选题或发布流水线。普通 X 信源仍由既有流水线和发布者处理；AI 信源遵守既有发布者规则。

## 控制台

AI 审阅页面支持事件/账号、各自类别和状态筛选、分页、AI 判断、原文证据、X 账号与域名线索、人工决定。仅显示 AI 已完成且建议跟进的项目。账号建议可标记认可或驳回，新增账号由管理员手动维护。进入页面或主动刷新时读取快照，不自动轮询。
