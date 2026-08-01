# SQLite 主存储迁移

## 目标与边界

生产业务数据库迁到单台 Linux 主机的本地 SQLite。Supabase 在切换点冻结，保留为可回切基线；SQLite 时期新增的数据不要求回灌 Supabase，也不采用持续双写。

本地 `searcher.sqlite` 是可重建的搜索与 embedding 缓存，不是第二份业务主库。它按短期文档 2 天、参考文档与向量 8 天清理；向量新写入 float32 BLOB，旧 JSON 向量在维护时转换。

## 实现状态

迁移版本已经完成：

- 唯一允许的活动后端为 `ODAILY_STORAGE_BACKEND=sqlite`；主库路径由 `ODAILY_SQLITE_PATH` 指定，存储世代由 `ODAILY_STORAGE_EPOCH` 标识。
- X 收集、主处理链、竞品、外媒提醒、非主流媒体、金十、鲸鱼与 Hyperliquid、审核者、编写者3、监督者、维护任务全部使用 SQLite 仓储。
- 本地队列按存储世代隔离；来源唯一键继续作为跨世代去重账本。
- 来源排除规则、控制台配置和流水线耗时的 SQLite 读写。
- 搜索缓存定期清理、JSON 向量转 BLOB、手工压缩命令。
- SQLite 在线备份、完整性校验、SHA-256 manifest。
- 网页和插件统一使用本地单账号认证；业务查询统一经过 `/console/data` 或插件 API，不再由浏览器直连数据库。
- `storage-import-legacy` 一次性流式导入冻结旧库，并逐表核对行数、执行 SQLite `integrity_check`。
- 导入器会把旧 `media_newsflash.title_key` 的历史空值规范化；同源同标题的重复旧行使用带旧 ID 的稳定后缀保留，避免唯一约束吞行。旧 `writer3_contexts.current_content` 空值转换为空字符串。

`psycopg` 仅在执行一次性旧库导入命令时临时需要，不属于新版本服务依赖。生产导入完成并移除旧数据库 URL 后，任何常驻服务都不连接旧库。

## 磁盘预算

新增 30 GB 可以先使用。主库迁移不复制 `searcher.sqlite` 的 embedding 数据；切换阶段仍需同时容纳主库、WAL、一次在线备份和临时压缩空间。运行时应保留至少 20% 文件系统空间，并在可用空间不足“主库大小的 2 倍加 5 GB”时禁止执行主库 VACUUM 或生成同盘备份。

## 搜索缓存操作

先预览：

```bash
python -m src.main search-cache-maintenance
```

执行清理与向量转换：

```bash
python -m src.main search-cache-maintenance --execute
```

低峰期需要实际归还文件空间时：

```bash
python -m src.main search-cache-maintenance --execute --compact
```

定时运行只删除过期行，不自动 VACUUM，避免在线业务出现长时间写锁。

## 回切契约

切换前停止所有写入，记录 Supabase 冻结时间、旧 commit 和旧环境文件，再导入新的 `sqlite-YYYYMMDD` 存储世代。极端失败时停止新版本全部服务，将生产 Git 工作树快进/检出到记录的旧 commit，恢复旧环境文件后启动旧版本服务。新版本本身没有 Postgres 开关；不得仅修改环境变量让新代码连接旧库，也不得把 SQLite 世代队列交给旧版本继续消费。

迁移预检与正式导入：

```bash
python backend/src/main.py storage-import-legacy
python backend/src/main.py storage-import-legacy --execute --truncate
```

正式导入必须在所有写入服务停止后执行。命令排除旧认证 session，改由本地单账号重新登录；其余已建模业务表逐表导入并校验行数。

SQLite 在线备份示例：

```bash
python -m src.main storage-backup --destination /var/backups/odaily/odaily-YYYYMMDD.sqlite
```

命令使用 SQLite backup API，能够包含已提交的 WAL 数据，并生成 `.manifest.json`。回切 Supabase 不依赖该备份；它用于 SQLite 自身故障恢复和迁移审计。
