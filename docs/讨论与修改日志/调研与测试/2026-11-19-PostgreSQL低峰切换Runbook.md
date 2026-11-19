# PostgreSQL 低峰切换 Runbook

状态：本地准备材料，尚未在生产执行。切换仍受真实数据全量导入/核验、外置证据迁移、source outbox 冻结水位和 2 倍峰值演练阻塞。用户已授权低峰停机切换，不创建 SQLite 备份；该授权不豁免逐库导入校验。

## 精确停止集合

生产只读核验确认以下 20 个 unit 属于切换窗口冻结集合。`odailyseer-*` 是独立项目，严禁停止或修改。LiteLLM 通常不读业务库，但按已确认的冻结集合停下再恢复；仅 19 个应用 unit 注入 PostgreSQL 环境，LiteLLM 不注入：

```text
odaily-auditor.service
odaily-business-telegram.service
odaily-competitor-monitor.service
odaily-editor-plugin-api.service
odaily-fact-auditor.service
odaily-gate-market-broadcast.service
odaily-hottopic.service
odaily-hyperliquid-account-profit.service
odaily-hyperliquid-naming.service
odaily-investment-leads.service
odaily-litellm.service
odaily-local-pipeline.service
odaily-macro-newsflash.service
odaily-meme-scanner.service
odaily-meme-tg-watcher.service
odaily-msx-notice.service
odaily-non-mainstream-media.service
odaily-stock-market-broadcast.service
odaily-worker.service
odaily-x-capture.service
```

停止/恢复只使用以上精确 unit 名，不使用 `odaily*`、`odaily-*.service` 或 systemd glob。X stage 和 external-alert stage instances 不在该已核验集合中；切换前另行确认它们确实未启用且没有独立 owner。运行时重新核实 unit PID、trigger 和 spool/session owner；若清单与生产实况不符，先更新清单并核验影响，不得猜测执行。

## 环境与数据目录

应用专用 DSN 已在 `/etc/odaily/postgresql.env`，权限应为 `0600`。确认准确服务实例后，在生产运行时为精确 unit 写 systemd drop-in；不修改 tracked unit，也不覆盖整个环境文件。每个 drop-in 只包含：

```ini
[Unit]
Requires=postgresql.service
After=postgresql.service

[Service]
EnvironmentFile=/etc/odaily/postgresql.env
Environment=ODAILY_STORAGE_BACKEND=postgres
```

上述 Unit 依赖只用于读取业务数据库的 19 个应用服务；不要写入 LiteLLM drop-in。`EnvironmentFile=/etc/odaily/postgresql.env` 在现有 `/opt/OdAIly/.env` 之后解析，使专用存储设置覆盖同名旧值。专用文件内只放专用 DSN 与有界 pool 设置，不放一般业务 secret；不要复制 DSN 到仓库、命令行参数或日志。用 `systemctl cat <exact-unit>` 与 `systemctl show <exact-unit> -p EnvironmentFiles` 检查生效来源，不显示 DSN 值。

在 `odaily-hottopic.service` 的同一个运行时 Service drop-in 增加 `Environment=ODAILY_COLLECT_SPOOL_DIR=/data/OdAIly/collector-spool` 与 `RequiresMountsFor=/data/OdAIly/collector-spool`。部署前验证该路径确在 `/data` 文件系统、有足够空间且服务用户可写。PostgreSQL cluster/WAL、collector spool、system-information evidence 与其他大文件运行资产都留在 `/data`。

## 停机、导入、启动

1. 低峰窗口只读记录精确 unit、owner PID、SQLite 源路径/WAL/SHM、`integrity_check`、各表 row count/hash、队列状态、证据 ref/pin、水位及 `/data` 余量。确认无未审计写者后，停止上述精确 units 和会写这些源的 timer/手工进程；等待所有进程退出并确认 SQLite checkpoint 一致。不创建副本。
2. 启动 PostgreSQL，确认 `systemctl is-active postgresql.service`，再从 `/etc/odaily/postgresql.env` 加载 DSN，核验目标是 `127.0.0.1` 上的数据库 `odaily`。为 19 个数据库应用服务写入并检查精确 runtime drop-in；HotTopic drop-in 同时设置 spool 与 `/data` mount 依赖。不写 LiteLLM；不要启动应用服务。
3. SQLite writer 全部停止后，先检查目标 PG 中 manifest 所有目标表均为空且没有既有 `native_sqlite_import_checkpoints` 业务记录；初始化 schema 后先运行下文的 frozen source outbox drain，将冻结源的最终 outbox 投影写入现有 SQLite system-information 库。核对每库 outbox target/watermark 后，再用 `PYTHONPATH=backend` 执行 native import，使刚 drain 的信息流与水位一并导入 PG。以 21-source manifest 为权威；runner 中 `DEFAULT_PATHS` 只是候选值，必须逐项与冻结前从精确 service Environment/EnvironmentFile、CLI、仓储 path resolver 得到的实际路径核对。将最终核定的绝对路径显式保存到本次报告/操作记录，并把不一致项通过 `--path KEY=/absolute/path` 传给 runner；确认 21 个解析路径均存在、realpath 互不重复，且两个 `ai_text_self_upgrade` 文件按活动/观测 owner 分别映射。runner 会在 schema 初始化之前拒绝缺失文件和重复 realpath。相同 source key/path 才可重跑，让 checkpoint/hash 检测幂等恢复；遇到 schema 指纹或目标基线冲突即停，不 truncate、不覆盖、不改 key 规避冲突。若先导入 system_information 后才 drain 到 SQLite，必须停止并重新制定恢复方案，不能直接重导。
4. 每库完成后立即逐表比对源/目标 row count、distinct PK、主键分块 hash、外键 orphan、唯一约束、JSON/时间、sequence 和业务专项关联。small evidence、文件证据、feedback/receipt 列映射及 source outbox 水位必须完成独立全量核验。Importer 单源成功只证明 registry 映射表及源内 FK，不证明跨源引用闭包；来源/业务 join、system-information evidence_ref 到 small_evidence 与外部 gzip 文件、pin/保护引用、投递/审核关联均须单独查询比对。任何一项失败，保留全部 SQLite/WAL/SHM 原件并继续停机排障。
5. 只在所有数据库及证据都核验通过后启动新写服务。PostgreSQL 由 unit ordering 保证先行。按序逐个启动并验收：local-pipeline、hottopic、核心 publisher `odaily-worker`、业务 API、业务 collectors，再启动 Gate/Hyperliquid/宏观/Meme/股票等其余冻结 unit；LiteLLM 最后恢复。使用清单中的精确 unit。每个步骤观察健康、心跳和业务读写后再继续，失败即停止后续启动。
6. PG-only 状态下检查每服务 `active`、启动日志、worker heartbeat、连接池/锁/statement/WAL/磁盘、队列推进、source outbox 水位和代表业务读写。确认无 SQLite 写旁路、无重复外部投递；保持低峰观察窗口。

源文件删除必须逐个 physical database 完成独立核验，并复核证据/审核/pin/outbox 引用闭包。某库通过后只处理该库完整 SQLite 文件组；其他未通过库继续保留。不要按行删除或以运行正常替代 hash 验收。

### PostgreSQL evidence 引用闭包

Native importer 的 FK audit 只覆盖单个 source mapping 内、source schema 中显式声明的 SQLite foreign keys；它不覆盖不同 source key 之间的关系、无 SQLite FK 的逻辑引用、外置 gzip 文件字节，也不证明引用行齐全。`system_information` 完成导入后、`small_evidence` 完成导入后各执行一次只读 evidence 检查；最终只在第二次检查全绿后评估释放两份 SQLite 元数据/内容源。脚本从 PG 扫描 `system_information_items/events/decision_samples/evidence_pins` 和 `system_review_analysis_attempts.raw_response_ref` 的所有唯一 ref，逐一要求 PG evidence metadata 存在；`postgres://` 或历史 `/_sqlite/` path 必须在 `small_evidence` 有对应 gzip bytes 且解压长度/SHA-256 与 ref 一致；其他 path 必须能从原路径读取外部 gzip 文件且长度/hash 匹配。原外部 gzip 文件留在 `/data` 原位，不随 SQLite 文件组删除。命令退出码非零、`complete=false`、失败明细非空或 missing relation 均不得释放源文件组：

```bash
cd /opt/OdAIly
set -a; . /etc/odaily/postgresql.env; set +a
export PYTHONPATH=backend
python scripts/verify_postgres_evidence_closure.py \
  --sqlite-source /data/OdAIly/system-observability/system_information.sqlite
```

带 `--sqlite-source` 时，该命令同时逐项流式比较冻结 system-information SQLite 与 PG 五类表中非空 evidence ref 的完整去重集合，任一方向多/缺都会非零退出。脚本用服务器端 cursor 分批扫描、不把全量 refs 放进 Python 内存，并使用迁移 statement/lock timeout。该 ref-set 对比需要全部五张源表都存在；历史 schema 缺表时失败关闭，须先核清是否确为旧版本缺省表，不可直接跳过。Editor plugin 的 `feedbacks` 与 `receipts` 通过 `(feed_item_id, feed_kind)` 指向多个不同 feed 类型，是有意的多态引用，目标项也可能已按 feed 留存策略过期；没有一个统一业务表可作为正确性 FK。此处以 importer 对 `main.editor_plugin_feedbacks`、`main.editor_plugin_receipts` 的全表 count/hash/PK 校验保留历史交互，启动前对比各 `feed_kind` 计数与主键、检查空身份；不把已过期 feed 当作迁移丢行。它不证明这些历史 feed 目标仍可渲染。Push API 投递记录仍需按各业务表的外部 ID/response/status 做专项源/PG 对账；这些关联当前仍是删除相应源文件组和 PG-only 启服前的阻塞。

### 冻结 source outbox

在所有会写入所列 SQLite 的 services、timers 和手工进程都停止并确认 owner 退出后、且在 system_information 原生导入之前执行 drain。drain 目标是现有 SQLite system-information 目录，不是 PostgreSQL；其投影行与最终 watermark 必须由后续 system_information native import 一并导入。source 清单必须来自本次生产只读核验：只列真实配置存在且由 projector 映射的物理 SQLite 库；一个物理文件只列一次。drain 命令中的 key 是 projector `database_key`（例如独立 HotTopic 文件 key 为 `hot`），不是 native importer key `hottopic`。配置了但文件不存在、映射为空、审计 schema/trigger 版本不匹配或 outbox 含未知表都会失败关闭。不要为可选源猜路径或传入不存在的路径。

若只需生产只读 readiness 检查，不要调用 drain 脚本：它会初始化/写入 system-information。以下命令直接以 `mode=ro` 读取八个来源和现有 system-information watermark，不创建目录、不初始化数据库、不推进水位；退出码 0 表示当前冻结区间通过，2 表示有来源未就绪：

```bash
cd /opt/OdAIly
PYTHONPATH=backend python - <<'PY'
import json, sqlite3
from pathlib import Path
from types import SimpleNamespace
from packages.system_observability.projector import table_specs_for_business_path
from packages.system_observability.native_import_cutover import inspect_frozen_sources

class ReadOnlyState:
    def __init__(self, path): self.path = Path(path)
    def get_state(self, key, default=''):
        uri = self.path.resolve().as_uri() + '?mode=ro'
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute('SELECT state_value FROM system_information_state WHERE state_key=?', (key,)).fetchone()
        return str(row[0]) if row else default

sources = {
    'main': Path('/opt/OdAIly/data/database/odaily.sqlite'),
    'hyperliquid': Path('/data/OdAIly/hyperliquid/hyperliquid.sqlite'),
    'hot': Path('/opt/OdAIly/data/runtime/hottopic.sqlite'),
    'meme': Path('/opt/OdAIly/data/processed/meme_scanner.sqlite3'),
    'gate': Path('/opt/OdAIly/data/runtime/gate_market.sqlite'),
    'stock': Path('/opt/OdAIly/data/runtime/stock_market.sqlite'),
    'queue': Path('/opt/OdAIly/data/runtime/local_pipeline.sqlite'),
    'business': Path('/opt/OdAIly/data/runtime/business_news.sqlite'),
}
projector = SimpleNamespace(
    table_specs=table_specs_for_business_path(separate=True),
    store=ReadOnlyState('/data/OdAIly/system-observability/system_information.sqlite'),
)
report = inspect_frozen_sources(sources, projector=projector)
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report['ready'] else 2)
PY
```

该检查只验证各 source audit schema、trigger version、映射表名及当前 watermark 后待处理区间内是否含未知表；它不冻结写者、不代表最终 drain target，也不能替代停写后的 drain。路径必须先按本次生产核验结果修正。若命令返回失败，保持切换阻塞并依据逐库 `reason` 排查。

从仓库根目录使用 `PYTHONPATH=backend`，逐个传入完整 source 清单与现有 system-observability 目标目录：

```bash
PYTHONPATH=backend python scripts/drain_frozen_source_outboxes.py \
  --source-writes-stopped \
  --observability-dir /data/OdAIly/system-observability \
  --source 'main=/opt/OdAIly/data/database/odaily.sqlite' \
  --source 'hyperliquid=/data/OdAIly/hyperliquid/hyperliquid.sqlite' \
  --source 'hot=/opt/OdAIly/data/runtime/hottopic.sqlite' \
  --source 'meme=/opt/OdAIly/data/processed/meme_scanner.sqlite3' \
  --source 'gate=/opt/OdAIly/data/runtime/gate_market.sqlite' \
  --source 'stock=/opt/OdAIly/data/runtime/stock_market.sqlite' \
  --source 'queue=/opt/OdAIly/data/runtime/local_pipeline.sqlite' \
  --source 'business=/opt/OdAIly/data/runtime/business_news.sqlite'
```

代码中当前 projector database_key 为 `main`、`hyperliquid`、`gate`、`stock`、`hot`、`meme`、`queue`、`business`。独立商务路径已在模块文档和既有生产切换记录中确认为 `/opt/OdAIly/data/runtime/business_news.sqlite`。runtime 仅把 `business_message_inbox` 从 `main` 改映射为 `business`；其他 `main` 表仍属于 `main`，不得把整个主库 key 改成 `business`。独立库中的其他商务表不在当前 projector 审计 allowlist 内，因此 `business` 来源的 outbox `table_name` 应只包含 `business_message_inbox`。不要传 importer key `hottopic` 或 `local_pipeline`，它们不是 projector key。命令中的其余路径仍是候选值，不是本轮生产核实结果；执行前逐项按精确 service Environment/EnvironmentFile、CLI、仓储 path resolver 核实源真实存在、路径一致，一个物理文件只列一次。`inspect_frozen_sources` 要求每个来源都已有 outbox/meta 表、当前 trigger version、outbox 表名均映射；尤其如果这些历史库从未由当前 projector 安装审计 trigger，命令会对 missing schema/version fail closed。不得通过删除未就绪源来绕过；先确认该物理库是否确实有需纳入的 audited tables，若有则必须有相同冻结版本 trigger/outbox 水位，否则保持切换阻塞。

命令要求显式 `--source-writes-stopped`，只读取来源 outbox 并将投影事务/水位写入目标观测库；它不安装/重建触发器、不跑全量快照、不复制或备份源文件、不运行 retention/cleanup，也不启动服务。输出中的每个 `target` 是停止写入后捕获的 outbox 最大序号；只有进程退出码为 0、`complete=true` 且每个来源的最终 watermark 达到对应 target，才算 drain 完成。失败或非零退出时保持停机并排查，不得继续切换。

### local-pipeline epoch 接管

Native import 保留 `local_pipeline_jobs.storage_epoch` 原值。导入到 PostgreSQL 后，SQLite 的 `sqlite-*` 队列行不会被 PostgreSQL 默认 epoch worker 领取。所有 SQLite writer 已冻结、全量导入及主键/状态/租约核验通过后，必须在 PG-only 单写窗口执行一次显式 epoch adoption；禁止把 adoption 放入应用/schema 初始化流程。

adoption 前先选定非空的切换 epoch（例如 `postgres-2026-11-cutover1`），校验符合 `ODAILY_STORAGE_EPOCH` 字符限制，并将它同时写入 `/etc/odaily/postgresql.env` 的 `ODAILY_STORAGE_EPOCH`，确保目标服务与执行 adoption 的命令读取同一值。源 epoch 必须显式从冻结 SQLite 行统计得出并以 `sqlite-` 开头；当前生产观测值为 `sqlite-20260802`，不可假定为 `sqlite-primary`。目标值必须与源 epoch 不同，且本次写入 PG 的应用服务都使用同一目标 epoch；adoption 返回的 `destination_epoch` 必须与该值一致，`adopted` 与冻结 SQLite 对应源 epoch 行数相同。重复执行预期 `adopted=0`。此时仍保持消费者停止，完成计数核验后再启动 local-pipeline。

```python
from packages.local_pipeline.postgres_queue import PostgresLocalPipelineQueue

queue = PostgresLocalPipelineQueue()
report = queue.adopt_frozen_sqlite_epoch(source_epoch="sqlite-20260802", source_writes_stopped=True)
print(report)
```

方法只接管显式指定的 `sqlite-*` 源 epoch 到当前 `ODAILY_STORAGE_EPOCH`，以事务更新 epoch，不改变 job status、attempt、payload、时间戳、lease owner/token 或 next-attempt。返回按源状态计数及接管行数；首次执行后核对计数，重复执行只应返回 `adopted=0`。其他 epoch（包括 `postgres-legacy`）保持原样。`running` job 的 lease 不会被立即清除或重放；沿用 worker 启动时的 30 分钟 stale lease recovery，旧 lease token 仍由 completion/failure fencing 拒绝。只有在核实原 owner 已退出且 recovery 判 stale 后，任务才重新进入 retry 流程，以降低重复外部副作用风险。队列状态计数不一致时保持服务停止并调查，不手工覆盖状态。

重复执行原则：导入只允许使用相同 source key、路径和 checkpoint 重入；导入前后不自动启动 services。service start/restart 必须在确认前一实例已停止、旧 owner 已退出后逐个执行；不要为“重试”同时启动第二个 consumer。切换后复核 scheduler/系统重启是否会拉起本地 enable 状态以外的 unit，必要时仅对已核验 unit 执行 enable 状态检查。

### 冻结混合写入增量对账

若某个已完成的混合写入窗口需要把少量 SQLite 源行补到 PostgreSQL，使用 `scripts/reconcile_frozen_increment.py`。工具仅处理显式 outbox 序号区间和四张业务表：`main.newsflash_items`、`main.tasks`、`main.x_capture_attempts`、`hottopic.x_agent_analysis_jobs`，以及这些行直接依赖的 `x_task_pipeline`、`x_processing_attempts`、`x_processing_attempt_events`、`search_event_candidates`、`search_event_sources` 和 HotTopic `hottopic_inbox`。它读取 SQLite 当前值，以 id 和业务唯一键检查 PostgreSQL；任一冲突会阻止整个事务，已有 PostgreSQL 行不更新，源侧已删除行只报告。执行前必须以冻结源上的 dry-run 计划逐行核对依赖和终态。默认 apply 遇到 pending task 会拒绝；只有明确接受这些行进入 PostgreSQL 后可能被 worker 领取时，才可增加 `--allow-pending-tasks`。该工具不会调用 worker、发布接口或外部服务。

先在本地冻结源文件写入并确定准确路径及已核准的上界。默认命令只做只读 dry-run；确认报告无冲突且缺行集合正确后，才可显式 apply。`x_capture_attempts` 若引用的账号缺失会由 PG 外键拒绝并回滚；需先单独核实账号缺口，不在此工具中自动补父行。

```bash
PYTHONPATH=backend python scripts/reconcile_frozen_increment.py \
  --source 'main=/path/to/odaily.sqlite' \
  --source 'hottopic=/path/to/hottopic.sqlite' \
  --seq 'main=1965044:MAIN_FROZEN_MAX' \
  --seq 'hottopic=135565:HOTTOPIC_FROZEN_MAX'
```

`MAIN_FROZEN_MAX` 与 `HOTTOPIC_FROZEN_MAX` 必须替换为冻结后分别读取到的真实 outbox 最大序号；不可直接假设两个来源已停写或把旧值作为上界。apply 命令增加 `--apply --source-writes-stopped`；只有经业务确认 pending task 可恢复领取时才额外增加 `--allow-pending-tasks`。必须先审查完整 JSON 计划，确认所有 identity 匹配、关联父行和 sequence 处理无冲突。源 SQLite 与 PG 均保留，apply 后复核相关表、attempt/event 链和序列值；不要启动 worker 作为验证手段。

## 回切

任何数据核验未过、PG-only 服务无法恢复、队列重复副作用或 SQLite 旁路出现时，停止所有已启动的新写服务，记录错误和 PG 审计。只有源库尚完整保留时，才按已演练的导出/恢复流程回切；不得假设有备份，不把 DSN 改回 Supabase，也不让 SQLite 与 PostgreSQL 同时写入/消费。
