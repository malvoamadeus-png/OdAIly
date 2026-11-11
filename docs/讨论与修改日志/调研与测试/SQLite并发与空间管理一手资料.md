# SQLite 并发与空间管理：一手资料及项目对照

日期：2026-11-11。范围：SQLite 官方文档、Linux 手册和本地仓库代码；本记录未连接生产服务器，也未执行生产写操作。此处不对具体宕机原因作结论。

## 官方机制

1. [SQLite WAL](https://www.sqlite.org/wal.html) 允许读写并发，但同一数据库的 WAL 只有一个写入者。读事务的快照可能阻止 checkpoint 越过读者使用的位置；长读事务可能导致 checkpoint 无法继续，WAL 空间持续增长。默认自动 checkpoint 阈值为 1000 页，编译参数和连接设置可以改变它。**限制**：WAL 文件大不自动证明 checkpoint 受阻，也不证明这次事故的锁由长读事务引起。
2. [SQLite 事务](https://www.sqlite.org/lang_transaction.html) 中 `BEGIN IMMEDIATE` 立即尝试启动写事务；同一库只能有一个写事务。应测量从获得写事务到 commit/rollback 的时间，区分等待锁与持锁做事的时间。只延长等待时间不能提升单库的写并发量。[busy timeout](https://www.sqlite.org/c3ref/busy_timeout.html) 是连接级忙处理器，在累计睡眠达到设置值后返回 `SQLITE_BUSY`；同一连接只能设置一个忙处理器，后设置可覆盖先设置，且不能假定所有争锁都会等待整个时限。
3. [Checkpoint API](https://www.sqlite.org/c3ref/wal_checkpoint_v2.html) 中 PASSIVE、FULL、RESTART、TRUNCATE 的等待和截断行为不同；[PRAGMA wal_checkpoint](https://www.sqlite.org/pragma.html#pragma_wal_checkpoint) 返回忙状态及日志页/已 checkpoint 页数，不能仅凭 SQL 未抛异常认为 checkpoint 已完全完成。做 checkpoint 本身会读写数据库，线上诊断不要把它当成无副作用查询；实际模式和超时需依据业务测试。
4. [PRAGMA auto_vacuum 与 freelist_count](https://www.sqlite.org/pragma.html#pragma_auto_vacuum) 说明默认 `NONE` 下删除后的空闲页可被后续写入复用，但数据库文件不随之收缩；`freelist_count × page_size` 是整页可复用空间的粗略值，不包含页内空洞，也不是未来可安全回收的精确数量。`FULL` 回收 freelist 页但不重整页面，可能增加碎片；`incremental_vacuum` 仅适用于 incremental 模式。不要在现有数据库上假设设置 PRAGMA 即完成转换。
5. [VACUUM](https://www.sqlite.org/lang_vacuum.html) 会重建数据库，正常执行可能需要**额外多达原数据库两倍大小的空闲磁盘空间**，是写操作，可能受其他连接锁阻止；没有显式 `INTEGER PRIMARY KEY` 的表中 ROWID 可能改变。`VACUUM INTO` 是另一个输出文件并有自身约束，不能视为直接替换线上库的安全操作。空间、时间、备份和所有依赖都须事先演练；不能根据空闲页数直接在线执行。
6. [Linux inode(7)](https://man7.org/linux/man-pages/man7/inode.7.html) 指出 `st_blocks` 表示实际分配块，以 512 字节为单位，`st_size` 是文件逻辑字节数；对小文件或稀疏文件二者可能明显不同。[statvfs(3)](https://man7.org/linux/man-pages/man3/statvfs.3.html) 提供文件系统总 inode、空闲 inode 和可用空间。块使用、inode 使用、文件数均须关注；文件内容逻辑大小之和不是实际占盘。
7. [SQLite 在线备份 API](https://www.sqlite.org/backup.html) 适用于运行中制作一致性副本；备份应考虑正在运行服务的锁和 I/O 影响，并定期做恢复演练。不要把只复制主数据库文件当作 WAL 模式完整备份。

## 对照当前本地实现

- `backend/packages/common/storage.py` 的 `connect_sqlite(path, timeout_seconds=...)` 先传入连接 timeout，再无条件设置 `PRAGMA busy_timeout=30000`；后者覆盖连接上的忙处理器，因此传入的非 30 秒值不会达到调用者想要的效果。该 helper 使用 WAL 和 `synchronous=FULL`；这不代表所有业务都经同一个连接函数或都使用同一个 SQLite 文件。
- `backend/packages/auditor/sqlite_repository.py` 的 `claim_task` 原先在 `BEGIN IMMEDIATE` 后执行候选查询、排序、正文 hash 和逐条查找；候选多或 I/O 慢时，这些工作均占用写事务。本地现已将候选筛选移出写事务，并在短写事务内重新验证候选和租约；详见本次自查记录。是否足以解释任何实际事件仍需事务耗时和持锁现场证据。
- `backend/packages/system_observability/business_retention.py` 的 `trim_source_text` 在写事务中筛选及匹配审核，需考虑将有界候选读取与计算移出写事务，同时在事务内重新校验状态、更新时间、正文和活跃审核；不可牺牲审核保护来缩短事务。
- `backend/packages/system_observability/store.py::storage_health` 通过 glob 遍历证据文件、累加 `st_size`，尚不统计实际分配块或 inode；`prune_orphan_evidence` 先读取引用和遍历文件，再对候选限量，单次删除量有限不意味着总工作量有限。
- `backend/packages/x_processing/searcher.py` 的维护可选执行 `wal_checkpoint(TRUNCATE)` 和 `VACUUM`；`backend/packages/meme_scanner/retention.py` 的 `--vacuum` 说明要求先停止写入。须另核对实际调用与调度，不能把代码存在等同于生产正在运行。
- `backend/packages/common/sqlite_backup.py` 已采用 SQLite backup API 并对备份做完整性检查；仍需另外验证恢复流程和业务对账。

## 建议的验证和治理顺序

1. 不改变业务留存前，按数据库路径、服务、操作名采样事务持锁时间、获取锁等待、`SQLITE_BUSY` 次数、排队量及磁盘延迟；指标不记录正文或密钥。观察 checkpoint 时明确这是维护操作及其 I/O 成本。对跨进程、超时和并发领取用隔离 SQLite 回归；同时保留业务结果和原子性。
2. 在读路径与计算移出写事务后，写入事务内做必要的条件复核，缩小批次并对后台维护设置工作预算、退避和限速。不要直接增加 timeout、重试频率或把所有业务迁到单一进程而不验证吞吐、可用性和顺序语义。
3. 健康监控分别采集各持久卷剩余字节与 inode、数据库文件/WAL/空闲页、小文件实际分配块和历史趋势。文件全量扫描应有频率、批量和时间预算，避免监控本身在高负荷下加剧 I/O；引用状态与文件统计的原子性/恢复也需设计。
4. 任何保留政策应按业务分类、安全关联和进行中的审核保护单独评估；用户现已明确普通且已结束业务的运行证据保留一周，具体定义、实现与历史积压释放仍需核实。发布稿件、审核结论、来源身份、待处理和故障证据不应被简单按年龄删除。
5. 历史数据回收先做只读估算，评估数据库可复用空间与文件系统空间的区别、磁盘与 inode 压力、恢复可行性。重建数据库、删除文件、生产部署和服务重启均应遵循项目运维流程；不使用 `VACUUM` 当作即时止血措施。

网络读取官方资料时曾出现间歇连接超时；已成功访问 WAL、VACUUM、PRAGMA 和 busy timeout 页面。checkpoint 和 transaction 专页链接列出供复核，不把本记录当作对具体线上版本和编译配置的验证。
