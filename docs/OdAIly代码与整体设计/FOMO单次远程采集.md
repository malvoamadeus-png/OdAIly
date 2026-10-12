# FOMO 单次远程采集

HideOnBush 通过专用 SSH key 的 forced-command 调用 `packages.meme_scanner.fomo_bridge`，stdin 为一行 JSON，stdout 为一行 JSON。入口不接受 shell 命令、路径或任意 URL；支持 `health`、`trending`、`thesis`。运行命令为 `cd /opt/OdAIly && PYTHONPATH=backend /usr/bin/xvfb-run -a .venv/bin/python -m packages.meme_scanner.fomo_bridge`。

`health` 只检查登录 profile 存在。其他请求使用 Meme scanner 的共享 exclusive_browser 锁（最长等90秒）、保存的 FOMO profile 和短时 headful Chromium。Trending 通过成功 `/v2/users` 请求的会话认证订阅 BSC/Solana WebSocket 完整快照；Thesis 使用 FOMO 网页原生 fetch 模块。每次请求在 finally 关闭浏览器；不会启动 scanner、交易、推送或发布流程，不修改 OdAIly 产品数据库。外层调用应限制总运行时间。

失败返回 error 和非零退出码，不能当作空榜。JWT 仅保留于进程内存，不输出或持久化。专用 key 禁止端口转发、代理转发、X11 和 PTY，仅能调用该固定命令。登录维护仍使用原有 `meme fomo-login`。
