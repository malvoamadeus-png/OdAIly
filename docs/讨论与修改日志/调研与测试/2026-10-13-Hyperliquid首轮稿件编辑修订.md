# Hyperliquid 首轮稿件编辑修订

核查对象：官方服务器 odaily-official / root / /opt/OdAIly，SQLite 任务 827705、827706、827707。原稿不覆盖，修订稿沿用原观察时点，仅以编辑预览展示。

## 确认的问题

- 三条原稿使用 prompt_template_versions 行 ID 41，但该专用 Prompt 的 version_number 为 1。控制台应关联版本表展示版本序号。此前小幅追加的编辑规则为第 2 版；本次全文重写另建版本，保留历史。
- 827705 命中 large_loss；HYPE 空单价值约 830.53 万美元，浮亏约 505.90 万美元，接口清算价 132.4500168714 美元。历史仅取得 10 笔 xyz:SKHY 成交，合计约 -2.15 美元；原稿的账户 -0.00 万美元既不相关也不是完整总收益。
- 827706 命中 large_position / large_profit；HYPE 多单价值约 1.1463 亿美元，浮盈约 6125.61 万美元。历史无成交，空集合相加得到 0 的实现错误，修正为 null。发现榜单中的日度/周度负 PnL 不属于该事件触发事实。
- 827707 仅命中 large_position；ETH 多单约 9869.71 万美元。取得 8922 笔成交但没有可重建的完整交易轮，不能把完整轮数 0 写成没有交易。负周榜排名只是地址发现来源，不作为新闻主线。

## 官方公开资料核查

2026-10-13 通过官方服务器读取以下第一方资料：

- https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/master/hyperliquid/info.py ：portfolio 接口返回多周期 account value history、PnL history、volume。
- https://hyperliquid.gitbook.io/hyperliquid-docs/llms-full.txt ：官方说明 unrealized pnl 以 mark price 和入场价格计算；Account value 包含 cross/isolated 仓位未实现收益以及 vault balances。清算价可能随资金费用、其他仓位浮盈亏等变化；mark price 用于估值、清算和未实现盈亏计算。

这些证据足以排除“账户周期 PnL 必然是纯已实现收益”的说法，但未取得排行榜计算实现，不能把所有差值归因于某一仓位。当前仓位从入场以来仍有大额浮盈，与最近期间账户收益下降可以同时成立。本次稿件直接省去无关周期成绩；相关明细留后台。

## 修订契约

写作事实按命中规则提供白名单，隔离发现来源、排名、技术元数据；专用模板改用多单/空单价值、浮盈/浮亏。市场占比不列分母金额。正文不写接口与统计免责声明，未知信息省略。浮亏或接近清算事件可写当前清算价及条件性的价格变动幅度。没有最新成交价格时不将 mark 改称现价。

固定尾部默认只留地址，高单资产胜率重要操作补该资产轮数、胜率及统计日期。旧稿和发布结果保持可追溯；编辑预览独立存储于 hl_news_metadata，不改变 task、队列与外部挂稿。

回归覆盖：无关负榜单不进入写作材料、空成交收益 null、实际清算价与方向、高胜率附加数据、Prompt 全文版本历史与重复激活、预览生成不修改任务、真实版本号关联。
