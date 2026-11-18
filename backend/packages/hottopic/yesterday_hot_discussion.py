from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from packages.publisher import PushClient, PushResult
from packages.x_processing.formatter import format_brief
from packages.x_processing.models import DraftBrief


SHANGHAI = ZoneInfo("Asia/Shanghai")
EVENT_ACTION_PATTERN = re.compile(
    r"宣布|称|表示|设立|成立|投入|投资|收购|推出|发布|上线|开放|关闭|暂停|"
    r"起诉|辞职|逮捕|捐赠|赠送|授予|融资|获批|通过|拒绝|同意|签署|"
    r"达到|突破|暴涨|暴跌|上涨|下跌|涨超|跌超|拟采用|拟收购|计划|将向|"
    r"将于|首次|遭到|导致|引发|被迫|撤回|取消|裁员|解雇|离职|"
    r"announced|launches|launched|acquires|acquired|plans to|will give|"
    r"raises?\b|resigns?\b|sues?\b|approved\b|surges?\b|plunges?\b",
    re.IGNORECASE,
)
ANALYSIS_PATTERN = re.compile(
    r"行业趋势|市场趋势|行业分析|趋势分析|赛道分析|生态分析|"
    r"趋势|转向|押注|积分|博弈|对冲|集中持仓|观点交锋|"
    r"社区热议|用户热议|"
    r"industry trend|market trend|sector analysis|community debate|points incentives",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class YesterdayHotDiscussionResult:
    status: str
    local_date: str
    topic_id: str | None = None
    title: str | None = None
    error: str | None = None


class YesterdayHotDiscussion:
    """Daily deterministic selection and backstage delivery from HotTopic briefs."""

    def __init__(self, connection: sqlite3.Connection | None = None, *, push_client: PushClient | None = None, postgres_repository=None) -> None:
        self.connection = connection
        self.postgres_repository = postgres_repository
        self.enabled = os.getenv("HOTTOPIC_YESTERDAY_HOT_DISCUSSION_ENABLED", "true").strip().lower() not in {
            "0", "false", "no", "off"
        }
        self.push_client = push_client or PushClient(
            endpoint=os.getenv("ODAILY_PUSH_ENDPOINT") or "http://47.113.217.70:8501/push/data",
            timeout_seconds=30,
            max_attempts=1,
            backoff_seconds=0,
        )
        if self.connection is not None:
            self.connection.execute(
            """CREATE TABLE IF NOT EXISTS yesterday_hot_discussion_runs (
                local_date TEXT PRIMARY KEY,
                topic_id TEXT,
                status TEXT NOT NULL,
                title TEXT,
                error TEXT,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                excluded_analysis_count INTEGER NOT NULL DEFAULT 0,
                excluded_no_action_count INTEGER NOT NULL DEFAULT 0,
                attempted_at TEXT NOT NULL,
                completed_at TEXT
            )"""
            )
            columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(yesterday_hot_discussion_runs)")}
            with self.connection:
                for name in ("candidate_count", "excluded_analysis_count", "excluded_no_action_count"):
                    if name not in columns:
                        self.connection.execute(
                            f"ALTER TABLE yesterday_hot_discussion_runs ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                        )
        if self.connection is None and self.postgres_repository is None:
            raise ValueError("YesterdayHotDiscussion requires a connection or PostgreSQL repository")

    def run_if_due(self, now: datetime | None = None) -> YesterdayHotDiscussionResult | None:
        if not self.enabled:
            return None
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        local_now = current.astimezone(SHANGHAI)
        if (local_now.hour, local_now.minute) < (8, 30):
            return None

        day = local_now.date()
        day_key = day.isoformat()
        existing = (self.postgres_repository.yesterday_run(day_key) if self.postgres_repository is not None else
                    self.connection.execute("SELECT 1 FROM yesterday_hot_discussion_runs WHERE local_date=?", (day_key,)).fetchone())
        if existing:
            return None

        start = datetime.combine(day, time.min, tzinfo=SHANGHAI).astimezone(UTC).isoformat()
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=SHANGHAI).astimezone(UTC).isoformat()
        topic, metrics = self._select_topic(start, end)
        attempted_at = current.astimezone(UTC).isoformat()
        if topic is None:
            if self.postgres_repository is not None:
                claimed = self.postgres_repository.record_yesterday_no_candidate(
                    local_date=day_key, counts=metrics, at=datetime.fromisoformat(attempted_at),
                )
            else:
                with self.connection:
                    self.connection.execute(
                    "INSERT INTO yesterday_hot_discussion_runs(local_date,status,error,candidate_count,"
                    "excluded_analysis_count,excluded_no_action_count,attempted_at,completed_at) "
                    "VALUES(?, 'no_candidate', NULL, ?, ?, ?, ?, ?)",
                        (day_key, metrics["checked"], metrics["excluded_analysis"], metrics["excluded_no_action"], attempted_at, attempted_at),
                    )
                claimed = True
            if not claimed:
                return None
            return YesterdayHotDiscussionResult(status="no_candidate", local_date=day_key)

        title = f"昨夜精彩事件：{topic['title']}"
        # Claim the date before making the remote request so a worker restart
        # cannot create a duplicate backstage item after an uncertain response.
        if self.postgres_repository is not None:
            claimed = self.postgres_repository.claim_yesterday_run(
                local_date=day_key, topic_id=topic["topic_id"], title=title, counts=metrics,
                attempted_at=datetime.fromisoformat(attempted_at),
            )
        else:
            with self.connection:
                self.connection.execute(
                "INSERT INTO yesterday_hot_discussion_runs(local_date,topic_id,status,title,candidate_count,"
                "excluded_analysis_count,excluded_no_action_count,attempted_at) "
                "VALUES(?,?, 'processing', ?, ?, ?, ?, ?)",
                    (day_key, topic["topic_id"], title, metrics["checked"], metrics["excluded_analysis"], metrics["excluded_no_action"], attempted_at),
                )
            claimed = True
        if not claimed:
            return None

        try:
            final = format_brief(DraftBrief(title=title, content=topic["brief"]))
            result: PushResult = self.push_client.push(
                title=final.title,
                content=final.content,
                dry_run=False,
                is_publish=False,
                is_push=False,
                idempotency_key=f"yesterday-hot-discussion:{day_key}:{topic['topic_id']}",
                origin_key="scheduled_brief",
                outlet_category="ai_regular",
                priority_status="not_applicable",
            )
            status = "backstage_created" if result.ok else "push_failed"
            error = result.error
        except Exception as exc:
            status = "format_failed"
            error = f"{type(exc).__name__}: {exc}"[:1000]

        completed_at = datetime.now(UTC).isoformat()
        if self.postgres_repository is not None:
            self.postgres_repository.finish_yesterday_run(
                local_date=day_key, status=status, error=error, completed_at=datetime.fromisoformat(completed_at),
            )
        else:
            with self.connection:
                self.connection.execute(
                "UPDATE yesterday_hot_discussion_runs SET status=?,error=?,completed_at=? WHERE local_date=?",
                    (status, error, completed_at, day_key),
                )
        return YesterdayHotDiscussionResult(status, day_key, topic["topic_id"], title, error)

    def dashboard_status(self, now: datetime | None = None) -> dict[str, object]:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        local_now = current.astimezone(SHANGHAI)
        day_key = local_now.date().isoformat()
        row = (self.postgres_repository.yesterday_run(day_key) if self.postgres_repository is not None else
               self.connection.execute(
                   "SELECT local_date,topic_id,status,title,error,candidate_count,excluded_analysis_count,"
                   "excluded_no_action_count,attempted_at,completed_at FROM yesterday_hot_discussion_runs WHERE local_date=?",
                   (day_key,),
               ).fetchone())
        if row is None:
            if not self.enabled:
                return {
                    "localDate": day_key,
                    "status": "disabled",
                    "candidateCount": 0,
                    "excludedAnalysisCount": 0,
                    "excludedNoActionCount": 0,
                    "attemptedAt": None,
                    "completedAt": None,
                }
            status = "scheduled" if (local_now.hour, local_now.minute) < (8, 30) else "waiting"
            return {
                "localDate": day_key,
                "status": status,
                "candidateCount": 0,
                "excludedAnalysisCount": 0,
                "excludedNoActionCount": 0,
                "attemptedAt": None,
                "completedAt": None,
            }
        return {
            "localDate": row["local_date"],
            "topicId": row["topic_id"],
            "status": row["status"],
            "title": row["title"],
            "error": row["error"],
            "candidateCount": row["candidate_count"],
            "excludedAnalysisCount": row["excluded_analysis_count"],
            "excludedNoActionCount": row["excluded_no_action_count"],
            "attemptedAt": row["attempted_at"],
            "completedAt": row["completed_at"],
        }

    def _select_topic(self, start: str, end: str) -> tuple[dict[str, Any] | sqlite3.Row | None, dict[str, int]]:
        if self.postgres_repository is not None:
            start_dt = datetime.fromisoformat(start)
            end_dt = datetime.fromisoformat(end)
            topics = self.postgres_repository.yesterday_topic_candidates(start_dt, end_dt)
        else:
            topics = self.connection.execute(
            "SELECT t.topic_id,t.first_seen_at,t.hotness_score,b.title,b.brief "
            "FROM topics t JOIN brief_revisions b ON b.topic_id=t.topic_id "
            "AND b.revision=(SELECT MAX(x.revision) FROM brief_revisions x WHERE x.topic_id=t.topic_id) "
            "WHERE t.matching_status='active' AND t.visibility='visible' AND t.brief_status='ready' "
            "AND datetime(t.first_seen_at)>=datetime(?) AND datetime(t.first_seen_at)<datetime(?) "
            "ORDER BY t.hotness_score DESC,t.participant_count_24h DESC,t.first_seen_at,t.topic_id",
                (start, end),
            ).fetchall()
        metrics = {"checked": 0, "excluded_analysis": 0, "excluded_no_action": 0}
        for topic in topics:
            metrics["checked"] += 1
            title = str(topic["title"] or "").strip()
            brief = str(topic["brief"] or "").strip()
            combined = f"{title}\n{brief}"
            if not title or not brief:
                metrics["excluded_no_action"] += 1
                continue
            if ANALYSIS_PATTERN.search(combined):
                metrics["excluded_analysis"] += 1
                continue
            if not EVENT_ACTION_PATTERN.search(combined):
                metrics["excluded_no_action"] += 1
                continue
            return topic, metrics
        return None, metrics
