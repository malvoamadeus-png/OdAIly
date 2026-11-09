from __future__ import annotations

BUSINESS_REGISTRY: tuple[dict[str, object], ...] = (
    {"key": "x", "label": "X快讯", "kind": "write_flow", "sources": ["x", "x_capture"], "entry": "X账号订阅", "scenarios": [
        {"key": "regular", "label": "普通账号", "steps": ["收集公开帖子并保存来源链接", "判断是否值得写成快讯", "查找相同事件与近期稿件", "生成并整理稿件", "按发布策略直发或挂后台", "已发布稿件进入异步审查"]},
        {"key": "onchain", "label": "链上写作", "steps": ["收集公开帖子", "判断为链上事件并保留链上规则版本", "查重并核验材料", "按链上模板写作", "格式整理后进入发布策略", "保存实际投递回执"]},
        {"key": "funding", "label": "融资写作", "steps": ["收集公开帖子", "判断为融资事件", "查重", "按融资模板写作", "格式整理后进入发布策略"]},
        {"key": "ai_source", "label": "AI信源标签", "steps": ["读取账号当时的AI信源标签", "走AI判断规则", "使用X常规模板", "查重并整理稿件", "进入发布策略"]},
        {"key": "discard", "label": "正常停止", "steps": ["收集帖子", "判断为重复、排除或材料不足", "保存停止原因，不创建发布步骤"]},
    ]},
    {"key": "binance_square", "label": "币安广场", "kind": "write_flow", "sources": ["binance_square"], "entry": "币安广场账号订阅", "enabled": False, "scenarios": [
        {"key": "disabled", "label": "未启用", "steps": ["总开关或服务未启用", "不产生采集任务；显示为未启用"]},
        {"key": "manual", "label": "启用后新帖", "steps": ["读取账号新帖并按帖子ID去重", "复用Crypto判断和查重", "复用快讯模板写作与格式整理", "发布者强制挂后台", "保留投稿结果"]},
    ]},
    {"key": "crypto_media", "label": "Crypto信源", "kind": "write_flow", "sources": ["non_mainstream_media", "mainstream_media", "blockbeats", "panews", "jinse", "msx", "event_tracking"], "entry": "站点与竞品采集", "scenarios": [
        {"key": "article", "label": "文章全文", "steps": ["获取原文、原链接与发布时间", "按来源执行排除与查重", "判断事件价值和写作类型", "写作并整理稿件", "直发或挂后台", "保留来源、版本和回执"]},
        {"key": "title_alert", "label": "标题提醒", "steps": ["采集标题和原始链接", "按标题提醒规则判断", "查重并提醒", "正常停止时记录排除或重复原因"]},
        {"key": "competitor", "label": "竞品报道", "steps": ["保留竞品原文链接并关联同一事件", "判断是否已由Odaily覆盖", "需要跟进时进入共享写作流程", "无需跟进时记录正常停止"]},
    ]},
    {"key": "ai_media", "label": "AI信源", "kind": "write_flow", "sources": ["ai_source"], "entry": "AI信源订阅", "scenarios": [
        {"key": "article", "label": "全文处理", "steps": ["采集文章全文", "执行AI信源价值判断", "查重", "写作与格式整理", "进入发布策略"]},
        {"key": "title_alert", "label": "标题提醒", "steps": ["采集标题", "按标题规则判断并查重", "生成提醒或正常停止"]},
    ]},
    {"key": "mixed_media", "label": "混合信源", "kind": "write_flow", "sources": ["mixed_source"], "entry": "混合站点订阅", "scenarios": [
        {"key": "crypto", "label": "Crypto分支", "steps": ["抓取站点文章", "轻量分类为Crypto", "进入Crypto判断、查重、写作和发布"]},
        {"key": "ai", "label": "AI分支", "steps": ["抓取站点文章", "轻量分类为AI", "进入AI信源判断、查重、写作和发布"]},
        {"key": "skip", "label": "不符合范围", "steps": ["抓取站点文章", "分流不符合已配置类别", "记录正常停止，不进入写作"]},
    ]},
    {"key": "title_alert", "label": "标题提醒", "kind": "alert_only", "sources": ["external_media_alert", "ai_source_alert"], "entry": "标题提醒站点", "scenarios": [
        {"key": "match", "label": "标题命中", "steps": ["采集标题、来源和链接", "执行领域规则与重复判断", "生成提醒并记录送达结果"]},
        {"key": "miss", "label": "未命中", "steps": ["采集标题", "规则判断不符合提醒范围", "记录停止原因"]},
    ]},
    {"key": "jin10", "label": "金十", "kind": "write_flow", "sources": ["jin10"], "entry": "金十快讯接口", "scenarios": [
        {"key": "publish", "label": "通过主题判断", "steps": ["读取金十新快讯", "按主题白名单判断", "查重", "常规快讯写作与格式整理", "按金十渠道挂后台或发布"]},
        {"key": "discard", "label": "主题外", "steps": ["读取金十新快讯", "主题判断不通过", "记录正常停止"]},
    ]},
    {"key": "business_news", "label": "商务快讯", "kind": "internal_work", "sources": ["business_telegram", "business_news"], "entry": "商务客户群消息", "scenarios": [
        {"key": "draft", "label": "生成并挂后台", "steps": ["接收客户群消息", "按客户和分类路由", "Writer4脱水并生成稿件", "定稿后挂后台", "记录客户、稿件和处理状态"]},
        {"key": "stop", "label": "无需处理", "steps": ["接收客户群消息", "检查客户配置和消息类型", "记录跳过原因并正常结束"]},
    ]},
    {"key": "hyperliquid_news", "label": "Hyperliquid新闻", "kind": "write_flow", "sources": ["hyperliquid_news"], "entry": "已批准场景监测", "scenarios": [
        {"key": "alert", "label": "普通提醒", "steps": ["读取地址活动和状态基线", "核对变化、覆盖完整性与提醒门槛", "去重后发送提醒", "不进入快讯写作"]},
        {"key": "news", "label": "新闻场景", "steps": ["按注册场景读取地址与事件材料", "通过确定性门槛生成候选", "候选创建任务并关联来源", "复用查重、写作与格式整理", "强制挂后台", "区分正式运行与预览测试"]},
    ]},
    {"key": "whale", "label": "巨鲸提醒", "kind": "alert_only", "sources": ["whale_watch", "whale_watch_hyperliquid"], "entry": "链上与Hyperliquid地址", "scenarios": [
        {"key": "chain", "label": "链上活动", "steps": ["轮询启用地址", "识别大额转账并检查单次门槛", "应用聚合窗口与去重", "发送提醒并保存回执"]},
        {"key": "hyperliquid", "label": "Hyperliquid活动", "steps": ["读取地址成交和账户变化", "比较基线并检查数据完整性", "应用提醒门槛与去重", "发送提醒；不上虚构的写稿步骤"]},
    ]},
    {"key": "meme", "label": "Meme速递", "kind": "write_flow", "sources": ["meme_scanner", "meme_tg", "daily_hot"], "entry": "迁移 / TG热议 / 今日热门", "scenarios": [
        {"key": "migration", "label": "迁移发现", "steps": ["监听迁移或新池事件", "通过流动性和安全准入", "采集行情和项目材料", "等待Grok叙事分析", "超时按降级规则继续或暂停", "生成稿件并挂后台"]},
        {"key": "telegram", "label": "TG热议", "steps": ["读取TG中CA与讨论", "校验代币身份并做准入", "获取行情和关联材料", "等待Grok分析或按超时降级", "生成稿件、挂后台或记录失败过期"]},
        {"key": "daily_hot", "label": "今日热门", "steps": ["按榜单扫描并检测首次达标", "采集行情、材料并完成准入", "等待Grok分析或按策略降级", "生成稿件并记录投递结果"]},
    ]},
    {"key": "gate_market", "label": "Gate行情播报", "kind": "market_broadcast", "sources": ["gate_market"], "entry": "Gate阈值监测", "scenarios": [
        {"key": "threshold", "label": "阈值触发", "steps": ["按周期读取行情", "比较触发值与市场基线", "检查模板键和防重复状态", "按运行模式直发或挂后台", "保存接口响应与不确定状态"]},
    ]},
    {"key": "stock_market", "label": "股票行情播报", "kind": "market_broadcast", "sources": ["stock_market"], "entry": "Binance股票阈值监测", "scenarios": [
        {"key": "merge", "label": "多资产合并", "steps": ["批量读取股票行情", "检测各资产阈值和防重复状态", "在合并窗口聚合同批资产", "组装稿件并执行实际直发或挂后台", "逐项关联事件与投递回执"]},
    ]},
    {"key": "scheduled_brief", "label": "定时行情简报", "kind": "scheduled_brief", "sources": ["gate_tradfi", "us_market", "okx_stock_weekly"], "entry": "既有简报调度", "scenarios": [
        {"key": "brief", "label": "定时生成", "steps": ["读取既有调度时间与日历条件", "生成Gate传统市场、美国市场或OKX周报材料", "保存最近执行结果与稿件", "链接统一信息流和实际发布状态"]},
    ]},
    {"key": "x_agent", "label": "X Agent研究", "kind": "internal_research", "sources": ["hot_topic", "market_sentiment", "project_promotion", "investment_leads", "auto_newsflash"], "entry": "X Agent订阅与事件", "scenarios": [
        {"key": "topic", "label": "热点追踪", "steps": ["发现和合并相关帖子", "按热点规则持续跟踪进展", "生成热点摘要并保留来源证据", "内部研究完成为正常终点"]},
        {"key": "auto_newsflash", "label": "热点自动快讯", "steps": ["筛选热点事件", "组织官方账号与进展材料", "生成候选稿并保存投递/后台结果"]},
        {"key": "sentiment", "label": "市场情绪", "steps": ["收集样本", "完成情绪分析", "保存内部研究结果，不视为漏发"]},
        {"key": "promotion", "label": "按头操作", "steps": ["收集帖子", "分别判断直接推介与有明确获利来源的操作建议", "保存两类判断和原帖证据"]},
        {"key": "investment", "label": "投资线索", "steps": ["发现候选帖子", "关联后续材料并检查证据", "完成组合分析或明确等待", "保存线索结论和来源链"]},
    ]},
    {"key": "editor_plugin", "label": "编辑插件辅助", "kind": "editor_assist", "sources": ["editor_plugin"], "entry": "编辑器生成请求", "scenarios": [
        {"key": "generate", "label": "人工辅助生成", "steps": ["编辑提交生成请求", "保存请求与模型结果或失败", "标识为人工辅助", "无官方发布映射时显示发布状态未知"]},
    ]},
    {"key": "post_publish_review", "label": "发布后审查", "kind": "post_publish_review", "sources": ["text_audit", "fact_audit", "official_mirror"], "entry": "已发布稿件异步审查", "scenarios": [
        {"key": "text", "label": "文本审查", "steps": ["按确切发布版本和策略排队", "异步检查中文质量", "保存证据和人工反馈，不阻断发布"]},
        {"key": "fact", "label": "事实审查", "steps": ["关联原始稿件版本与来源", "检查关键事实并保存外部证据", "将审查问题与原稿关联，不计为新产出"]},
        {"key": "mirror", "label": "官方镜像", "steps": ["按官方ID同步发布稿", "对齐实际提交稿和镜像版本", "镜像差异作为原稿后续事件展示"]},
    ]},
)

OBSERVABILITY_DISABLED_SOURCES = frozenset({"x_capture", "jin10", "non_mainstream_media", "whale_watch"})

SOURCE_TO_BUSINESS = {
    str(source): str(entry["key"])
    for entry in BUSINESS_REGISTRY
    for source in entry["sources"]  # type: ignore[union-attr]
}

SOURCE_LABELS = {
    str(source): str(entry["label"])
    for entry in BUSINESS_REGISTRY
    for source in entry["sources"]  # type: ignore[union-attr]
}
