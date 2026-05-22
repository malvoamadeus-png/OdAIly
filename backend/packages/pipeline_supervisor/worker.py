from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.common.config import PipelineSupervisorSettings
from packages.x_processing.telegram import TelegramClient, TelegramResult

from .repository import PipelineSupervisorRepository


@dataclass(frozen=True, slots=True)
class PipelineAlert:
    alert_key: str
    message: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SupervisorRunResult:
    checked: int
    sent: int
    suppressed: int


class PipelineSupervisorWorker:
    def __init__(
        self,
        *,
        repository: PipelineSupervisorRepository,
        settings: PipelineSupervisorSettings,
        telegram_client: TelegramClient | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.telegram_client = telegram_client or TelegramClient(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            message_thread_id=settings.telegram_message_thread_id,
            timeout_seconds=settings.telegram_timeout_seconds,
            max_attempts=settings.retry.max_attempts,
            backoff_seconds=settings.retry.backoff_seconds,
        )

    def run_once(self) -> SupervisorRunResult:
        alerts = self.build_alerts()
        sent = 0
        suppressed = 0
        dedup_cutoff = utc_now() - timedelta(minutes=self.settings.alert_dedup_minutes)
        for alert in alerts:
            if not self.repository.claim_alert(
                alert_key=alert.alert_key,
                message=alert.message,
                dedup_cutoff=dedup_cutoff,
                metadata=alert.metadata,
            ):
                suppressed += 1
                continue
            result = self.telegram_client.send_message(alert.message)
            if result.ok or result.skipped:
                sent += 1
            else:
                print(f"[odaily] pipeline supervisor telegram failed alert={alert.alert_key}: {result.error}")
        return SupervisorRunResult(checked=len(alerts), sent=sent, suppressed=suppressed)

    def run_forever(self) -> None:
        print("[odaily] pipeline supervisor started")
        while True:
            result = self.run_once()
            if result.checked:
                print(
                    "[odaily] pipeline supervisor round "
                    f"alerts={result.checked} sent={result.sent} suppressed={result.suppressed}"
                )
            time.sleep(self.settings.interval_seconds)

    def build_alerts(self) -> list[PipelineAlert]:
        now = utc_now()
        heartbeat_cutoff = now - timedelta(minutes=self.settings.heartbeat_stale_minutes)
        task_cutoff = now - timedelta(minutes=self.settings.task_stuck_minutes)
        failed_since = now - timedelta(minutes=self.settings.failed_window_minutes)
        alerts: list[PipelineAlert] = []

        for row in self.repository.list_stale_heartbeats(cutoff=heartbeat_cutoff):
            component = str(row["component"])
            last_seen = format_dt(row.get("last_seen_at"))
            alerts.append(
                PipelineAlert(
                    alert_key=f"stale_heartbeat:{component}",
                    message=(
                        "OdAIly流水线报警\n"
                        "类型：worker 心跳超时\n"
                        f"对象：{component}\n"
                        f"详情：超过 {self.settings.heartbeat_stale_minutes} 分钟未更新，last_seen={last_seen}"
                    ),
                    metadata=dict(row),
                )
            )

        for row in self.repository.list_stale_success_heartbeats(cutoff=heartbeat_cutoff):
            component = str(row["component"])
            last_success = format_dt(row.get("last_success_at"))
            last_error = row.get("last_error") or "-"
            alerts.append(
                PipelineAlert(
                    alert_key=f"stale_success_heartbeat:{component}",
                    message=(
                        "OdAIly流水线报警\n"
                        "类型：worker 无近期成功心跳\n"
                        f"对象：{component}\n"
                        f"详情：最近 {self.settings.heartbeat_stale_minutes} 分钟没有成功心跳，"
                        f"last_success={last_success}，last_error={last_error}"
                    ),
                    metadata=dict(row),
                )
            )

        for row in self.repository.list_old_claimable_tasks(cutoff=task_cutoff):
            source = str(row["source"])
            status = str(row["status"])
            count = int(row["count"])
            oldest = format_dt(row.get("oldest_updated_at") or row.get("oldest_created_at"))
            alerts.append(
                PipelineAlert(
                    alert_key=f"old_claimable:{source}:{status}",
                    message=(
                        "OdAIly流水线报警\n"
                        "类型：任务积压\n"
                        f"对象：{source}/{status}\n"
                        f"详情：{count} 条任务等待超过 {self.settings.task_stuck_minutes} 分钟，最老更新时间={oldest}"
                    ),
                    metadata=dict(row),
                )
            )

        for row in self.repository.list_stuck_processing_tasks(cutoff=task_cutoff):
            source = str(row["source"])
            status = str(row["status"])
            count = int(row["count"])
            oldest = format_dt(row.get("oldest_updated_at"))
            locked_until = format_dt(row.get("oldest_locked_until"))
            alerts.append(
                PipelineAlert(
                    alert_key=f"stuck_processing:{source}:{status}",
                    message=(
                        "OdAIly流水线报警\n"
                        "类型：处理中任务卡住\n"
                        f"对象：{source}/{status}\n"
                        f"详情：{count} 条任务处理超时或锁过期，最老更新时间={oldest}，最早锁到期={locked_until}"
                    ),
                    metadata=dict(row),
                )
            )

        for row in self.repository.list_recent_failed_tasks(since=failed_since, threshold=self.settings.failed_threshold):
            source = str(row["source"])
            status = str(row["status"])
            count = int(row["count"])
            alerts.append(
                PipelineAlert(
                    alert_key=f"recent_failed:{source}:{status}",
                    message=(
                        "OdAIly流水线报警\n"
                        "类型：失败任务异常\n"
                        f"对象：{source}/{status}\n"
                        f"详情：最近 {self.settings.failed_window_minutes} 分钟失败 {count} 条"
                    ),
                    metadata=dict(row),
                )
            )

        if (
            self.repository.count_recent_x_success_attempts(since=heartbeat_cutoff) == 0
            and self.repository.count_recent_x_capture_success_heartbeats(since=heartbeat_cutoff) == 0
        ):
            alerts.append(
                PipelineAlert(
                    alert_key="x_capture:no_recent_success_attempt",
                    message=(
                        "OdAIly流水线报警\n"
                        "类型：X 抓取无成功记录\n"
                        "对象：x_capture_attempts\n"
                        f"详情：最近 {self.settings.heartbeat_stale_minutes} 分钟没有 success attempt"
                    ),
                    metadata={"since": heartbeat_cutoff.isoformat()},
                )
            )

        return alerts


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_dt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
