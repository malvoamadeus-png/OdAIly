# FOMO 单次远程采集

HideOnBush 通过专用 SSH key 的 forced-command 调用 `packages.meme_scanner.fomo_bridge`，stdin 为一行 JSON，stdout 为一行 JSON。入口不接受 shell 命令、路径或任意 URL；支持 `health`、`trending`、`thesis`。使用独立显示编号177（本机xvfb-run自动编号存在清理退出码问题），外层flock串行化入口，内层仍共享scanner浏览器锁；运行命令为 `cd /opt/OdAIly && timeout 240 flock -w 60 data/processed/fomo_bridge.lock env PYTHONPATH=backend /usr/bin/xvfb-run -n 177 -s '-screen 0 1440x900x24' .venv/bin/python -m packages.meme_scanner.fomo_bridge`。Thesis只回传作者、时间和公开正文，剔除嵌套的行情和交易数据。

`health` 只检查登录 profile 存在。其他请求使用 Meme scanner 的共享 exclusive_browser 锁（最长等90秒）、保存的 FOMO profile 和短时 headful Chromium。Trending 监听网页自身已认证WebSocket的完整snapshot，仅接受topicId明确覆盖BSC/Solana的消息；不依赖已变更的Authorization header。Thesis 使用 FOMO 网页原生 fetch 模块。每次请求在 finally 关闭浏览器；不会启动 scanner、交易、推送或发布流程，不修改 OdAIly 产品数据库。外层调用应限制总运行时间。

失败返回 error 和非零退出码，不能当作空榜。JWT 仅保留于进程内存，不输出或持久化。专用 key 禁止端口转发、代理转发、X11 和 PTY，仅能调用该固定命令。登录维护仍使用原有 `meme fomo-login`。
