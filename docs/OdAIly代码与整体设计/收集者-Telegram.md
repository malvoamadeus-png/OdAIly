# 收集者-Telegram

`telegram-channels-worker` 使用独立 Telethon 授权会话监听代码中预置的频道，生产服务为 `odaily-telegram-channels.service`。`TELEGRAM_CHANNEL_SESSION_PATH` 必须指向独立会话，不能同时由其他进程使用；API 凭证沿用 Meme Telegram 配置。首次启动以每个频道的最新消息建立游标，不回填上线前消息。新消息事件立即唤醒对应频道，空闲时每 30 秒补查以覆盖连接间隙。频道分别处理；进程失败后由 systemd 重启，从数据库游标继续补抓。

`apewirenewsCN` 和 `trad_fin_cn` 直接写入插件高频区，保留非空文本行与 Telegram 原文链接。前者移除末尾 `@apewirenews` 或相应 `t.me/apewirenews` 署名；其他正文及链接保留。无文本消息跳过。消息以频道及消息 ID 幂等写入插件。

投机之路（内部频道 ID `-1002631914757`）及 `durov` 写入 `tasks.source=telegram_channel`，按判断、搜索、编写者1、编写者2、发布者顺序处理，原文链接传入发布链路。首次写入前读取 `telegram_channel_names` 中的写作名并保存在任务元数据；控制台只允许修改两个预置频道的写作名。任务按频道及消息 ID 去重，成功提交本地流水线后才推进游标；提交失败时重启重试同一消息，不创建重复任务。插件写入成功后才推进聚合频道游标。
