# SQLite 本地主存储架构

## 当前结论

Linux 主机上的 `data/database/odaily.sqlite` 是全部业务事实的唯一主库。采集、处理、审核、Writer3、巨鲸、监督、维护、控制台和插件均不得在常驻路径连接 Supabase/Postgres。`data/runtime/local_pipeline.sqlite`、`editor_plugin_local.sqlite`、`pipeline_timing.sqlite` 与 `gate_market.sqlite` 是面向队列或读模型的本地运行资产；`searcher.sqlite` 是按 2/8 天保留、可重建的搜索缓存。

网页和插件只访问 `editor-plugin-api-server`：本地单账号认证签发不透明 Bearer session，控制台业务表由 `/console/data` 白名单接口访问主 SQLite，插件信息流由本地 feed store 提供。浏览器不持有数据库地址或数据库密钥。

所有主库连接启用 WAL、30 秒 `busy_timeout`、外键约束和 `synchronous=FULL`。WAL 模式只在数据库尚未进入 WAL 时设置，避免每次建连重复争用模式切换锁；仓储方法退出连接上下文时同时提交或回滚并关闭文件句柄，禁止常驻 worker 累积闲置连接。

## 一次性迁移

切换时停止所有旧写入服务，使用 `storage-import-legacy --execute --truncate` 从冻结旧库流式导入已建模业务表，逐表核对行数并执行 `PRAGMA integrity_check`。旧认证 session 不导入；新版本使用部署内置的单操作者 bcrypt 密码哈希。

## 回退边界

旧 Supabase 数据、切换前 commit 与旧环境文件共同组成回切基线。新版本没有 Postgres 运行开关；灾难回切必须停止全部新服务并恢复完整旧版本。SQLite 接管后的新增记录无需回灌旧库。

具体操作、磁盘门槛、备份和搜索缓存维护见 `SQLite主存储迁移.md`。
