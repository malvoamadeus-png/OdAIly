# PostgreSQL 迁移收尾待执行计划

日期：2026-11-19。目标：完成 OdAIly 从 SQLite 到官方服务器本机 PostgreSQL 的迁移，保留全部业务数据，并在确认导入后清理旧 SQLite 文件及 WAL/SHM。本文供拆分到其他对话逐项执行；每项完成后记录证据和结果，避免重新跑已完成的几十 GB 导入。

## 约束

- 用户已授权低峰停机迁移；允许在目标 PostgreSQL 验收后删除对应旧 SQLite 文件组。
- 不主动丢弃任何业务数据；不创建备份、不扩容、不把临时文件放到根盘。迁移中 SQLite 原件暂时保留，只有对应数据确认已导入后才删。
- 目标为 `odaily-official` 上的本机 PostgreSQL，唯一新连接配置为 `ODAILY_POSTGRES_DSN`。旧 Supabase 不属于新运行路径。
- 生产代码只在本地修改、提交并推送；服务器只 `git fetch`、`git pull --ff-only` 和执行获准的迁移/服务操作。
- 只操作 OdAIly 精确服务。`OdailySeer` 是独立项目，其 SQLite 和服务不属于本计划，禁止停止或清理。
- 避免全库重复导入和逐条解压证据的长时间扫描。优先使用现有 checkpoint、源报告、计数/主键集合对账和小样本内容检查；仅对确有差异的数据做定点处理。

## 当前已知状态

- PostgreSQL 已部署在官方机；多个主要 OdAIly 服务以 PostgreSQL 为主写路径。2026-11-19 检查时，活动 OdAIly 进程没有打开 SQLite 文件句柄；HotTopic 修复部署后服务 `active`、`NRestarts=0`，且采集进程不再打开 `hottopic.sqlite`。这是运行时检查结果，不代表定时任务、全部历史数据或所有数据库均已验收。
- PostgreSQL 数据约 24.5 GB，`/data` 当时余约 15 GB；根盘余约 3.1 GB。空间紧张，任何导入前都要估计增量和 WAL 空间，不得用根盘临时目录。
- 生产迁移报告中有 17 个唯一 source key 的 complete 报告；`run.json` 重复包含两个来源。Hyperliquid、HotTopic 等报告显示完成。旧报告时间早于部分源文件最后更新时间，所以不能单凭 complete 报告删除源。
- `system_information` 各业务表 checkpoint 显示源行数与导入行数匹配，`small_evidence` checkpoint 为 449,813/449,813；但 `system_information.json` 在导入后注册 source identity 阶段失败。数据表已导入不等于身份索引/证据闭包验收完成。
- `business_news` 四张表 checkpoint 均完成（763、3、5、1 行）；旧 `business_news` 报告失败信息待更新/解释，不应因此重跑全库。
- main 全库缺少最终成功报告；现有 `reconcile.json` 的 complete 只表示其中 307 条 main/HotTopic outbox 增量处理成功，另有 17 个 pending tasks。它不是所有来源的最终冻结对账。
- 当前本地已有未提交改动：`backend/packages/common/native_sqlite_import.py`、`backend/packages/system_observability/projector.py`、`backend/test_native_sqlite_import.py`，另有未跟踪 `predicttest.txt`。后续执行者须先审阅并保留这些改动，不得覆盖或混入无关提交。

## 待执行事项

### 1. 完成来源身份修复并补齐索引

当前本地已有一项修复草稿：Hyperliquid `hl_news_dispatch` 的 SQLite rowid 会复用，观测投影需用稳定 `event_key`，并统一到 projector 使用的 `main` 身份空间。历史冲突对应两个不同事件及不同证据，不能合并或跳过。此修复尚未通过测试/提交/部署。

完成标准：有针对性测试证明两个不同事件即使旧 rowid 相同仍保留独立 identity；本地依赖齐全并通过该测试；代码提交推送并 fast-forward 部署；只重建 `system_information` source identity 索引，不重跑其 29 GB 表导入；报告冲突为零且两条事件及证据引用仍分别存在。

可复制任务：

> 只处理 OdAIly PostgreSQL 迁移的 system_information source identity 冲突。先检查当前本地未提交的 `native_sqlite_import.py`、`projector.py` 和测试改动，不覆盖其他人的工作。核对 `hl_news_dispatch` 两个冲突事件的稳定键和证据引用，完成正确的稳定 identity 修复与针对性测试；提交并推送后按项目规则 fast-forward 部署。生产只执行 identity 注册/重建，不重跑表导入，不合并或丢弃事件。记录冲突为零、两事件仍独立以及服务状态。不要清理 SQLite。

### 2. 补齐失败报告与 main 来源最终状态

业务表的 checkpoint 已说明 business_news 数据表全部导入；应查明旧失败发生在哪个后置步骤，并生成准确的成功/失败状态记录。main 需从 checkpoint 与现有增量 outbox 记录构造最终来源清单，不要猜测或重新导入所有表。

完成标准：每个唯一 source key 有明确结果；每表记录源行数、目标行数、checkpoint/摘要和报告时间；完成的来源不重复导入；未完成项有具体表名和原因；main、business_news 的增量事件/任务状态已核对。处理后不留“complete=true 但只覆盖局部增量”的歧义。

可复制任务：

> 只读核对生产 PostgreSQL native import checkpoints、migration-reports 和 main/business_news outbox 水位。business_news 已知四张表 checkpoint 均完成，不要全库重跑。为 main 与 business_news 生成准确的 source/table 完成清单，区分全量导入与 outbox 增量；定位现有失败报告对应阶段并修正报告或只补缺失步骤。对照各源当前行数/主键范围发现实际差异时才定点补齐。不要删除 SQLite，不做长时间全量 hash 重算。

### 3. 完成证据引用闭包的轻量核对

system_information 与 small_evidence 表数据已有完整 checkpoint，但跨源引用尚未验收。旧的逐条解压 SHA256 脚本运行近一小时且无进度，已被停止；不要照原样重跑。

完成标准：来源引用集合（items、events、decision samples、pins、review attempts）均能在 PostgreSQL 证据元数据中找到；小证据 ref 在 `small_evidence` 中有对应内容；外部 gzip 文件引用路径存在且抽样解压校验通过；保护/pin 记录没有悬空。输出缺失计数及少量样本即可，不扫描每条大文件内容。

可复制任务：

> 对生产 PostgreSQL 做 system_information 到 small_evidence 的证据引用闭包核对。利用 PG 内集合/anti-join 查询覆盖 items、events、decision_samples、pins、review attempts，不逐条解压所有证据。检查小证据引用的 ref、元数据与 payload 存在性；外部 gzip 路径做存在性核对并抽样校验 hash。报告缺失计数和少量定位样本；仅修复确认缺失的记录，不重复搬运 29 GB，不删除源文件。

### 4. 做一次低峰冻结与最终增量对账

逐服务确认写入者和数据库路径。选择低峰短暂停止精确 OdAIly writers，等待事务/队列提交，确认 SQLite WAL/SHM 状态后，读取最后增量并导入/对账 PostgreSQL。只停止列出的 OdAIly units，不使用通配符，不操作 OdailySeer。

完成标准：冻结时间、服务列表、源文件路径及 WAL/SHM 状态有记录；最后增量全部提交到 PG；任务队列 pending/running/failed 与外部投递结果对得上；恢复服务后业务写入只进入 PG。不得因观察到 SQLite 文件 mtime 较新就直接认定有新业务数据，先看实际表行数、最大主键/时间和 WAL checkpoint。

可复制任务：

> 在低峰对 OdAIly 做短时最终切换核对。先只读列出精确写服务、PID、源 SQLite/WAL/SHM 和 PG 状态；随后停这些 OdAIly writers，完成 SQLite checkpoint 状态确认及最后增量对账/补入，再启动服务并检查 PostgreSQL 写入与无 SQLite 文件句柄。不要停止 OdailySeer，不做备份/扩容，不按文件 mtime 猜数据差异，也不要重跑已完成来源。

### 5. 按完整物理库分组清理旧 SQLite

每个物理库仅在来源全量数据、最终增量、关联状态都验收后清理其 SQLite 主文件、WAL 和 SHM；清理后立即查看空间回收、PG 表可读、服务健康。优先释放体积大的已验收文件以缓解 `/data` 空间压力。不得只删除 WAL/SHM，也不得留下用户数据文件当作所谓备份。

完成标准：逐库清单显示“PG 已验收、文件组已删除、释放空间、服务状态”；未验收来源继续保留并明确原因。清理不触及 OdailySeer、session 文件、证据目录或非 SQLite 运行资产。

可复制任务：

> 按物理 SQLite 文件组逐个清理已完成 PostgreSQL 验收的 OdAIly 数据库。删除前确认该库最终增量、表对账和业务关联闭包都通过，且没有进程打开；随后删除该数据库的 SQLite 主文件及对应 WAL/SHM，并立即核对 `/data` 空间、PG 目标表可读和相关服务健康。先处理已验收的大文件释放空间。没有通过验收的库保留并列明阻塞；排除 OdailySeer 和非数据库文件。用户已明确不需要备份，但要求保留迁入的数据。

## 全部完成的定义

1. 每个生产 OdAIly 服务/定时任务均已确认使用 PostgreSQL；没有 SQLite 写入旁路。
2. 每个生产 SQLite 来源均有最终冻结后的导入/增量对账结果，pending/running 队列和发布账本保持一致。
3. 来源 identity 冲突清零；证据引用与 pin 闭包完成；没有未解释的数据差异。
4. 已验收的 OdAIly SQLite/WAL/SHM 均清理，`/data` 空间确实回收；OdailySeer 独立 SQLite 保持原样。
5. 服务恢复稳定、没有新增数据库错误；只做覆盖上述标准的针对性核验，不做重复的全量解压扫描。
