# PostgreSQL 全面迁移实施清单

日期：2026-11-18。状态：用户已授权低峰迁移，代码实施中；生产 PostgreSQL 基础设施已就绪，但产品仍运行 SQLite，尚无业务数据库导入或后端切换。本地代码实施不连接生产、不读取密钥。本清单与 [ADR 0003](../../adr/0003-local-postgresql-primary-storage.md) 配套。

## 目标约束

- 官方服务器本机 PostgreSQL 是目标；旧 Supabase 不参与新运行路径。新连接只读 `ODAILY_POSTGRES_DSN`。
- 一个 `odaily` 数据库，业务域按逻辑 schema / migration component 管理。已移植主业务当前使用 `main`，迁移账本用 `(schema_name, component, version)`，checksum drift 必须失败。
- 连接由共享有界 psycopg 3 pool 管理；每个操作短 checkout，`autocommit=True`，涉及读改写或多表原子性的操作显式 `postgres_transaction`。禁止长持连接、隐式事务和静默 SQLite 回退。
- 一次低峰停机完成最终一致快照导入并切为单写。不做无限双写；未完成完整校验前不切换生产。
- 数据完整性优先：保留来源 ID 与去重、稿件版本、队列/租约、配置、审核/事实核查及模型尝试、外部投递结果、人工审阅和审计。外部副作用不能因重试而重复触发。

## 本地代码盘点

初始检索发现至少 30 个非测试 Python 文件直接调用 `sqlite3.connect`，另有统一 `connect_sqlite`、多种独立连接封装和独立文件库。粗略代码检索计数为 563 个 SQLite 专属 SQL/API 命中：142 `PRAGMA`、121 `INSERT OR IGNORE`、179 `julianday`、68 `rowid`、65 `sqlite_master`、16 触发器定义、15 `lastrowid`、15 `INSERT OR REPLACE`、28 `json_extract`、10 `json_each`。这些是代码命中数而非独立 SQL 语句数；仍须 AST/逐仓储审计。

首次扫描未发现 FTS virtual table 声明；观测检索采用 `system_information_search_positions` 自建索引表。明确的 BLOB 包括 X processing gzip 模型响应和 small evidence 的 gzip payload；搜索 embedding 编码仍需查明是否持久化为独立缓存 BLOB。SQLite 触发器在主业务、观测投影、Whale Watch 和 Hyperliquid 聚合中使用；迁移须分别转为 PG trigger 或显式事务逻辑，不能丢状态事件/汇总约束。

SQLite 专属语义重点：`INSERT OR IGNORE/REPLACE`、`lastrowid`、`rowid`、`sqlite_master`、`PRAGMA table_info/index_list/foreign_keys/integrity_check`、`julianday/strftime`、JSON1、SQLite `ON CONFLICT`、事务 `BEGIN IMMEDIATE`、`SQLITE_BUSY/LOCKED` 分类、backup API、动态 schema 变更和触发器。替换为原生 PG SQL/JSONB/timestamptz/RETURNING/目录查询/行锁，按业务方法迁移，不用正则兼容层。

## 生产存储对象清单（只读输入，不是本机测得）

以下容量来自监督代理对生产的只读核查；迁移窗口开始时须复核路径、SQLite checkpoint 状态、表行数、freelist、文件系统和服务 owner。文件容量不能直接当 PostgreSQL 空间预算，索引、WAL 和导入空间都需另算。用户要求不创建备份、不扩容，也不在根盘放迁移临时文件；分块导入、PG 临时数据和 WAL 均留在 `/data`。

| 路径 / 对象 | 责任 | 目标处置 | 当前状态 |
| --- | --- | --- | --- |
| `/opt/OdAIly/data/database/odaily.sqlite`，约 11.8 GB | 来源、主任务/写作流水线、去重、配置、审核和发布账本 | PostgreSQL `main`；完整导入、计数和哈希通过后删除原 SQLite 文件组 | 目标移植中 |
| `/opt/OdAIly/data/runtime/hottopic.sqlite`，约 2.44 GB | X Agent 目录、热点、事件、投资线索共享对象 | PostgreSQL 按 `hottopic`/相关 component；全量保留后删除原 SQLite 文件组 | 未迁 |
| `/data/OdAIly/system-observability/system_information.sqlite`，约 30.53 GB | 全量信息流、状态投影、审阅索引和证据引用 | PostgreSQL 观测域；全量保留记录和证据引用 | 未迁，容量最大风险 |
| `/data/OdAIly/hyperliquid/hyperliquid.sqlite`，约 3.89 GB | Hyperliquid 地址、成交、压缩历史、状态、候选和投递 | PostgreSQL 对应域，全部历史和压缩口径均保留 | 未迁 |
| `/data/OdAIly/system-observability/system_information/small_evidence.sqlite` | 小证据内容寻址及固定留存 | PostgreSQL；逐 ref 对账并保留 pin 保护 | 未迁 |
| `data/processed/meme_scanner.sqlite3`，约 545 MB | Meme scanner durable job、观察和发布状态 | PG Meme 域 | 未迁 |
| `data/processed/searcher/searcher.sqlite`，约 92 MB | 短期文档和 embedding 缓存 | PostgreSQL 或兼容原有缓存读取的导入；保留现有数据和过期策略 | 未迁 |
| `data/runtime/local_pipeline.sqlite`，约 44 MB | 本地队列、租约和重试 | PostgreSQL durable queue；不得丢 pending/running | 未迁 |
| `data/runtime/business_news.sqlite`，约 1.2 MB | 商务 Telegram 来源/配置 | PG 对应域 | 未迁 |
| `data/runtime/gate_market.sqlite`，约 1.7 MB | Gate 行情任务及去重 | PG 对应域 | 未迁 |
| `data/runtime/stock_market.sqlite`，约 9.6 MB | 行情任务、状态、样本 | PG 对应域 | 未迁 |
| `data/runtime/macro_newsflash.sqlite`，约 44 KB | 宏观快讯轮询/发布账本 | PG 对应域 | 未迁 |
| `data/runtime/editor_plugin_local.sqlite`，约 1.4 MB | 插件本地阅读/接收状态 | PG 对应域 | 未迁 |
| `data/runtime/pipeline_timing.sqlite`，约 6.3 MB | 流水线耗时本地读模型；历史归档 | 独立 PostgreSQL `pipeline_timing.pipeline_timing_snapshots`；主库中同名历史表保持原域，分别迁移、分别核验 | 原生仓储已实现；独立文件历史导入待办 |
| `data/runtime/ai_review.sqlite`，约 3.7 MB | AI review 队列/结果 | PG 对应域，内容及版本全保留 | 未迁 |
| `data/runtime/predict_market_discovery.sqlite`，约 11 MB | 预测市场发现状态 | PG 对应域 | 未迁 |
| `data/runtime/predict_html_discovery.sqlite`，约 1.5 MB | HTML 发现状态 | PG 对应域 | 未迁 |
| `data/runtime/msx_notice.sqlite`，约 32 KB | MSX 通知/处理游标 | PG 对应域 | 未迁 |
| `data/runtime/ai_text_self_upgrade.sqlite`，约 94 KB；观测目录副本约 794 KB | prompt 自升级及观测材料 | 先确认两个库的读写 owner、复制关系和主键，再分别迁 | 活动状态待核 |
| `data/processed/competitor_monitor/competitor_monitor.sqlite`、`data/processed/writer3/writer3.sqlite` | 搜索缓存/Writer3 索引（默认路径；生产实际路径待核） | 保留原有搜索缓存/Writer3 索引数据和业务稿件域 | 待核 |
| Telethon `.session` 及相关文件 | 第三方客户端认证会话 | 不是产品 SQL；由原服务的单一 owner 管理，不导入 SQL，不复制后双 IP 使用 | 迁移需停旧 owner 后按服务恢复/重新授权 |

路径表里的相对路径按应用数据根解释；以服务 unit 的工作目录、环境覆盖和代码实际配置为准，不能仅凭默认值假设生产位置。系统还可能有 PostgreSQL WAL、SQLite WAL/SHM、证据 gzip 文件和其他外部依赖私有状态。主库中的历史 Hyperliquid、business、MSX 表须与独立活动 SQLite 域分别迁移，不能按表名前缀合并。

## 模块与调用所有权

| 域/代码 | 当前代码状态 | 迁移责任与待办 |
| --- | --- | --- |
| `x_capture` | PG 原生仓储启用开发；`main` component migration；保留 SQLite factory | 核验全部账号字段/monitor_group、采集尝试抽样与 X 去重；待迁移清单覆盖目录和 attempt 导入 |
| `x_processing` | PG 原生仓储开发；JSONB、stage attempts、claim 与 `SKIP LOCKED`；schema 可独立初始化 | 继续核验所有 writer/publisher 字段与回写行为；争用恢复/失效 lease finalizer 需测 |
| `competitor_monitor` | PG 仓储启用开发；新增 URL-key/exclusion schema 与缺失 API | 覆盖时间校正、事件匹配/保护、event summary/外键触发器，做方法级 PG 集成测 |
| `external_media_alert`、`non_mainstream_media`、`jin10_monitor`、`auditor`、`fact_auditor`、`writer3`、`pipeline_supervisor`、`maintenance`、`whale_watch` | 多数旧 PG 类在 `repository.py` 里仍留存但 constructor 退役；有些只有 SQLite | 每域比对当前 SQLite 方法、schema 演进、row JSON/time、锁/租约；逐个启用原生仓储 |
| `common/source_exclusions.py`、`binance_square`、`publisher/push_client.py`、`business_news/storage.py`、`msx_notice_worker.py` | 分散仓储/调用点 | 统一走 storage backend 明确工厂；保留业务 API response shape 与幂等键 |
| `local_pipeline`、`hottopic`、`investment_leads` | 活动 SQLite 队列/业务；不属于主业务代理负责范围 | foundation/对应业务代理实现；跨域写需显式协议，不假设跨域事务 |
| `system_observability`、`small_evidence`、`pipeline_timing`、`ai_text_self_upgrade` | 独立读取/投影/证据库，可能直接连接 SQLite | 耗时看板已实现 PG 原生时间范围查询和聚合快照归档；仍需迁移历史快照。其他库逐表定义源/投影职责、watermark 和幂等重放；保留普通证据 1 天、故障与保护证据规则 |
| `meme_scanner`、`meme_quote_audit` 及其他小库 | 多个进程独立 SQLite 文件及读模型 | 清点 systemd owner、job retry、外部投递幂等和只读关联；逐域原生 PG |

入口初始检索：至少 30 个非测试 Python 文件直接调用 `sqlite3.connect`。所有 SQLite 连接 factory/paths、服务命令和 env 路由必须纳入最终 AST 审计；审计不以文件名猜连接归属。

## 可执行切分

1. **公共底座**：固定 psycopg 3/pool 版本、只读本机 `ODAILY_POSTGRES_DSN`、短连接池/角色名/超时、显式事务 helper、组件 migration ledger/checksum。验收：池饱和有限失败、每连接 search_path 隔离、异常回滚、schema migration 幂等和 checksum drift 抛错。
2. **主内容与发布**：按 `x_capture → x_processing → competitor/external/non-mainstream/jin10 → publisher/auditor/fact_auditor` 移植，兼容数据库可独立 init；每个包公开 API 对齐 SQLite，业务结果与 JSON/时间映射相同。
3. **队列、维护和依赖域**：local pipeline、supervisor、maintenance、writer3、whale/hyperliquid、business/gate/stock/macro/MSX/predict、editor local/auth、timing。每个任务有稳定幂等键、租约/attempt 和旧版本重放语义。
4. **HotTopic / investment**：保持账号资格、投资材料版本、来源保护与现有分析过程；由对应业务代理实现并单独签收。
5. **观测与证据**：系统信息投影以 source table/row ID + event ID 幂等恢复，水位和源事件不能先于目标提交推进；小证据 ref/hash/pin/retention 做全量核验，文件型大证据保留内容寻址与 gzip 校验。
6. **导入工具**：在停写一致快照上从生产 SQLite 直接流式批量导入，每批事务可重试；在外键依赖顺序内保留 ID；记录源快照指纹、迁移版本、每表 count/hash 和失败行定位；重跑必须校验或显式拒绝冲突，不能覆盖已导入数据。不得创建 SQLite 备份或根盘临时副本。
7. **演练与切换**：测试生产级数据量/索引和 2 倍当前峰值；验证断点重导、应用服务启动/停止和回切工具；正式窗口精确停止 OdAIly unit，逐项核验，不使用 `systemctl stop 'odaily*'`，因为 `OdailySeer` 是另一个项目。源库在完整目标校验前保留，校验后按完整物理数据库删除以释放 `/data` 空间。

## 迁移数据校验

- 冻结前只读清点：精确 unit、owner PID、每个 DB 路径及 WAL/SHM、SQLite `integrity_check`、表行数、主键范围/集合、最大时间、序列/自增值、队列 pending/running/failed/最老年龄、来源去重约束冲突、审核/版本/投递关联、证据 ref 和磁盘/freelist。
- 停写后直接读取每个 SQLite 原件及其 WAL/SHM 一致状态；不创建迁移备份，不复制完整文件，也不把数据暂存到根盘。记录 SQLite schema/user_version、源 commit、冻结时间和精确源路径，并对源库执行 `integrity_check`。
- 不按行删除 SQLite 源记录，因为普通 SQLite 文件不会因此缩小。只有完整物理数据库全部迁移完成、整库行数/主键/哈希及业务关联核验通过后，才可删除对应 SQLite/WAL/SHM 完整文件组；此前不得删除源文件。迁移按 `/data` 可用空间分块导入并逐步核实 PostgreSQL/WAL 余量，不扩容文件系统。
- 每表按确定列顺序、规范化 JSON、UTC 时间和 BLOB 字节计算可复现的主键分块 hash；原始文本、外部 ID、来源 URL、人工配置和审核结果不得重写。抽样原文 hash 与整表计数同时核验。
- PostgreSQL 导入后检查每表 row count、distinct PK、min/max ID、目标端同 source/schema 映射 FK orphan、unique violation、JSON 类型/必填字段、timestamp 时区、sequence `last_value`、业务 join 计数；任务状态和租约逐状态对账。Importer 不对大型 SQLite source 执行全库 `PRAGMA foreign_key_check`；其 FK audit 只覆盖同一 source mapping、同一目标 schema 内的已映射父子表关系，跨 source/schema 关系由业务 join 专项核对。
- 交易决策与名誉保护专项核对：source/source_item_id 去重账本、原稿/多版本/审核 ID、人工反馈/标注、事实与文本审核材料、Push API 外部 ID 和投递响应、失败重试结果、已 pin 证据；任一缺少均不通过。
- 所有源数据库与表均纳入迁移，包括历史、缓存、队列和投影；不得有意省略、清空或改为启动空库。其余任何行数或 hash 差异必须定位并修正，不能按比例容差。

## 低峰现场最短执行顺序

以下只在完成代码提交推送、官方服务器 fast-forward 同步、低峰冻结并确认一致 SQLite 原件及本机 PostgreSQL 可用后执行；它不是当前已执行记录。先加载 `/etc/odaily/postgresql.env`，确认 shell 内 `ODAILY_POSTGRES_DSN` 指向本机 `odaily`，并设置 `PYTHONPATH=backend`。命令在 `/opt/OdAIly` 运行，不创建源库副本或根盘临时文件。

```bash
cd /opt/OdAIly
set -a; . /etc/odaily/postgresql.env; set +a
export PYTHONPATH=backend
python -m packages.common.native_sqlite_import --initialize-schemas
```

随后对照下表路径清单逐一执行只读 preflight，通过后按外键顺序导入；单个物理 SQLite 文件只调用一次 importer，不能把同文件拆成多个 source key 分别导入。编排入口按固定 source 顺序逐库执行 preflight 和导入，任一源失败即停止；开始前必须人工停止所有 SQLite 写入者，并显式确认冻结。每库报告及总报告原子写入 `/data/OdAIly/migration-reports/`，目录不允许位于 `/data` 之外：

```bash
python scripts/run_native_sqlite_migration.py --confirm-frozen
```

现场路径不符合默认 manifest 时，用重复的 `--path SOURCE_KEY=/absolute/path` 覆盖对应项；可用重复 `--source SOURCE_KEY` 选择一段来源做续跑。失败时保留 SQLite 原件和 `/data/OdAIly/migration-reports/` 报告，先修复错误，再对失败源及后续来源重跑；导入器按 PostgreSQL checkpoint 续传，已完整完成的源仍须由报告和对账确认。runner 顺序先迁最大 `system_information`，紧接 `small_evidence`，尽早把同属观测物理库的数据导入 PG。每库完成报告的 `source_file_group_release.eligible_for_owner_review=true` 仅表示 importer 已完成该 source 所映射表的逐表 count/hash 验证和同 source/schema 内目标端 FK orphan 检查，供负责人启动释放审查；`automatic_deletion=false`，程序从不删除源。负责人还须核对冻结快照路径/大小与现场盘点一致、SQLite `integrity_check`、最终 checkpoint 与 WAL/SHM 状态、所有跨源关联（尤其 system-information evidence refs 均在 small_evidence 中，含 pin/保护引用）、目标唯一键/业务关联及删后 PG 可读，再由负责人按 SQLite、WAL、SHM 完整文件组手工释放。`system_information` 报告完成但 `small_evidence` 尚未完成时不得释放 evidence SQLite；两者完整验收后才可释放两组并核查实际磁盘回收量。导入器的 PG COPY、逐表核验及目标端 FK 检查使用 `ODAILY_POSTGRES_MIGRATION_STATEMENT_TIMEOUT_MS`、`...MIGRATION_LOCK_TIMEOUT_MS` 和 `...MIGRATION_IDLE_TRANSACTION_TIMEOUT_MS`，绕开普通业务 30 秒 statement timeout；单条语句超过配置的迁移超时仍会失败并可按 checkpoint 重试。SQLite 临时排序设置 `temp_store=FILE`，runner 将 `ODAILY_SQLITE_TMPDIR` 默认到 `/data/OdAIly/migration-reports/`；单源 CLI 遵循 `ODAILY_SQLITE_TMPDIR`、`SQLITE_TMPDIR`、`TMPDIR` 优先级，但目录必须显式落在 `/data` 且可写，否则拒绝启动；这样 SQLite 临时排序不会使用默认根盘 `/tmp`。不得把报告目录改到根盘。`--confirm-frozen` 只是人工确认，不会替代停止服务、检查残留写入者和验证 SQLite/WAL 一致性的现场步骤。

生产 21 源的 `source_key` 和路径以本次只读 manifest 为准。现 runner 是固定顺序的批次编排器，不读取历史报告自动跳过已完成源，不自动删除文件，也不在源完成时做生产业务 join 对账；失败后按已报告的 source 与下一 source 用重复 `--source` 显式选择续跑。分阶段释放至少需要三个现场批次：先 `system_information`，复核/释放其物理文件组；再 `small_evidence`，完成全部引用/内容关联校验后复核/释放其文件组；最后其余来源。每批次都需保留 `/data` 报告，释放后重查 `df`、PG relation/WAL 和 SQLite 整库状态。全部源完成后再复核跨域表行数、主键集合、哈希、外键/唯一键、序列、Prompt active version、队列各状态、source identity、证据 ref/pin 和 source outbox 冻结水位；只有所有检查通过后才进入服务 PG-only 启动步骤。

本地隔离 PostgreSQL 验证已覆盖非空 Prompt active version、队列 `running`/`pending`、JSON 数值、SQL NULL、时区转换、自增 sequence 与批次中断恢复。schema-only initializer 不生成 auth/newsflash 默认数据。仍未获得真实生产行数/hash 对账、约 30.53 GB 信息流/证据的空间与 WAL 峰值、source outbox 冻结水位完整演练、外部 evidence 文件落盘/校验以及 feedback/receipt 列映射验收；这些仍阻止低峰切换和源文件删除。

## 资源预算与生产边界

监督代理的生产只读核查记录：根盘余约 3.7 GiB、`/data` 余约 40 GiB、RAM 8 GiB，活动 SQLite 页面合计约 43 GB；观测库约 30.53 GB、主库约 11.8 GB，另有 Hyperliquid 3.89 GB 与 HotTopic 2.44 GB。按旧数据，先迁 system_information 才能释放约 30.53 GB，给后续导入创造空间；但其 PG relation、索引和 WAL 峰值未知，仅凭源文件大小不能证明首库能在 40 GB 余量内导入完成。首库若不能完整导入并完成校验，就没有可安全释放的空间，故必须先用同 schema 的受控非空小样测 PG 表/索引/WAL 放大率，再根据 SQLite 页数、free pages、各表行大小和索引计划制定分批流式容量区间；检查首库导入过程峰值与恢复余量。普通测试 schema 只能给样本放大率，不能证明全量尖峰；若估算加安全余量超过现场可用空间，停止切换并重新设计容量方案，不得边导入边删未验源行。所有临时数据、PG relation 和 WAL 留在 `/data`。每个物理文件组完整导入并验收后由负责人手工释放，再处理下一大域；程序不删 SQLite/WAL/SHM。SQLite `DELETE` 不会缩小普通文件。释放后立即核对 `df`、PostgreSQL `pg_database_size/pg_total_relation_size`、WAL 目录占用与 SQLite 组目录，记录实测回收量。

生产 PostgreSQL 基础设施状态（2026-11-18）：官方服务器 `odaily-official`、Linux 用户 `root`、仓库路径 `/opt/OdAIly` 上已安装 PostgreSQL 15.19，cluster 数据目录为 `/data/OdAIly/postgresql/15/main`，systemd drop-in 为 `/etc/systemd/system/postgresql.service.d/odaily-data.conf`，服务配置为 `/data/OdAIly/postgresql/15/main/odaily.conf`。服务监听 `127.0.0.1`；数据库 `odaily` 与受限 owner role `odaily` 已创建，PUBLIC database access 已撤销；登录与 checksums 已核验。专用环境文件位于 `/etc/odaily/postgresql.env`，权限为 `0600`，内含 DSN 与有界连接池配置，不复制到仓库或日志。尚无产品服务读取该文件，应用没有切换 backend，SQLite 未删除，数据尚未导入。此状态只表示基础设施准备完成，不表示产品迁移或切换完成。

正式低峰停机前由主负责人先在 `odaily-official`、Linux 用户 `root`、`/opt/OdAIly` 只读检查生产 unit、SQLite、日志、运行路径、checkpoint 与剩余空间。此前任何连接生产的命令都须在进度消息列明别名、用户、路径和目的。停止服务只列出本产品精确 unit；绝不碰 `OdailySeer` 服务。代码仍只在本地修改、提交和推送，服务器仅按既有规则 fast-forward 同步。

## 验收与回切

- 所有域 factory 在 `ODAILY_STORAGE_BACKEND=postgres` 时选择 PostgreSQL，在 `sqlite` 时仅用于迁移前/测试；缺配置、未迁域和 PG 错误应明确失败，不得吞错切 SQLite。
- 逐服务用 PG-only 启动，确认不存在产品 SQLite 写连接；只读访问对账无缺失。所有待处理队列可继续推进，重试不重复外部副作用。
- 观测源事件可重复投影且不漏；普通成功证据继续遵循一天留存，故障证据、正在审核稿件和明确保护证据不被清理。
- 在删除任何源文件组前保留可直接读取的 SQLite 原件；删除后若需回切，停止 PostgreSQL 写入并执行数据导出/恢复流程。
- 切换完成标准：逐表 hash 和业务专项全绿、峰值及恢复演练全绿、pool/lock/statement/WAL/磁盘监控可见、精确服务启动成功、无 SQLite 旁路；按用户已有的低峰迁移授权执行单写切换。
- 未通过时：停止所有新版本写服务，留存 PG 审计与错误；回切使用已验证的数据导出/恢复流程，恢复切换前代码与配置。不要假设存在 SQLite 外部备份、仅改 env 指向旧 Supabase，或运行双服务器同时消费队列/Telegram session。

本清单不表示上述实现、导入、生产只读核查或切换已经完成。服务器 SQLite 原件仅在全部数据成功导入目标并通过完整校验后才可删除；本次迁移不创建 SQLite 备份，回切按已验证的数据导出/恢复流程执行。

## 2026-11-18 本地实现状态补充

生产 schema manifest `/tmp/odaily-production-sqlite-schemas-20261118.json` 覆盖 21 个物理 SQLite 文件、266 张表；这是 schema 清单，不包含生产文件行数，也不代表本地能读取生产数据。原 JSONB archive importer 已移除，避免误作全量迁移入口。当前唯一导入入口为 `backend/packages/common/native_sqlite_import.py`，实现显式 source registry、主键 checkpoint、批次 COPY、目标基线检查、JSON/时间/日期/数组类型转换、行数与行哈希校验、序列推进及源外键检查；CLI 支持 `--preflight`、`--initialize-schemas`、批次行数与字节上限。schema-only initializer 使用仓储 DDL 初始化入口，并避免运行 auth 默认账号和 newsflash roster/state seed。信息流历史搜索索引属于可再生 postings，不纳入迁移；六小时信息流的业务信息、决策与巡检证据仍按各自映射保留。seen-items 源复合主键允许对应 PG 唯一约束；source identity 映射覆盖登记的 SQLite 文件名并批量写入。使用隔离 PostgreSQL 按生产 manifest 创建的 21 个 DDL fixture 已全部通过 preflight 和空源 native import；catalog 中 208 张 native 目标表均存在且有 PK/UNIQUE。该演练不含业务行，不能替代生产数据行数、主键集合及 hash 对账，因此仍不可用于生产切换或删除任何物理源文件。small evidence 转换校验 gzip 内容 hash、计算解压字节数并保留创建时间，相关系统信息路径改为 `postgres://small_evidence/<ref>`。当前剩余切换阻塞项包含 source-state outbox 冻结水位、外部 evidence 文件迁移、editor feedback/receipt 主域列映射，以及 Hyperliquid / newsflash 余项。

pipeline timing 当前新快照写入独立 `pipeline_timing` schema，且 PG fixture 验证主库 `main.pipeline_timing_snapshots` 历史行不被读取或覆盖。该独立 timing SQLite 文件的历史行仍待单独映射导入；主库同名旧表和独立 timing 文件不能共用主键或合并。

主库旧表 `hl_news_metadata` 保留在 `legacy_main`，不与活动 `hyperliquid_news` 域合并。主库旧表仍按 JSONB 导入；活动域将普通 metadata 存在 `hl_news_metadata` JSONB，序列化后超过 200 MiB 的 payload 存在 `hl_news_metadata_large` TEXT，应用读取透明地回退到大文本表。活动 Hyperliquid SQLite 源的 metadata 一律流入大文本表以保留原始 UTF-8 payload，避免 PostgreSQL JSONB 单值 256 MiB 限制；写入普通 metadata 时若键从大值变小值（或反向），事务内清除另一张表的旧副本。活动表迁移使用 SQLite `blobopen()` 分块读取、PostgreSQL 临时 staging 和独立 stream checkpoint，摘要为 `SHA256(key, MD5(payload UTF-8 bytes))`。已有通用 JSONB checkpoint 的前缀须先按旧 canonical digest 与 SQLite 源核验，再逐行核对 JSON 语义；事务内将 SQLite 原始 payload 文本搬到大文本表并删除 JSONB 副本，写入新协议 checkpoint 后继续扫描。此兼容桥保留既有业务数据，任何校验失败均回滚搬迁并停止导入。普通 JSONB 仍受 PostgreSQL 单值 256 MiB 限制，因此应用写入阈值留有余量；TEXT/staging 仍受 PostgreSQL 单字段大小和磁盘空间约束。该专属路径不扩大通用 64 MiB 行限制，也不改变其他表 canonical row hash。
