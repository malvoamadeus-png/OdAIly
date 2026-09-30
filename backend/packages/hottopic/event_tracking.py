"""Autonomous, official-account-only follow-up tracking for HotTopic events.

The module owns the complete event-tracking state machine.  Callers only feed
it newly affected HotTopic ids, provide an X timeline scanner, and render its
read-only projections.  In particular, neither account discovery nor any
failure path creates an editorial approval queue.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

import requests

from packages.common.storage import connect_sqlite

from .capture import AccountRow, ContentItem


EVENT_TRACKING_MAX_ACCOUNTS_PER_EVENT = 3
EVENT_TRACKING_MAX_ACTIVE_ACCOUNTS = 15
EVENT_TRACKING_POLL_INTERVAL = timedelta(minutes=1)
EVENT_TRACKING_INITIAL_WINDOW = timedelta(hours=72)
EVENT_TRACKING_SILENCE_WINDOW = timedelta(hours=24)
EVENT_TRACKING_PROGRESS_EXTENSION = timedelta(hours=48)
EVENT_TRACKING_HARD_CAP = timedelta(days=7)
EVENT_TRACKING_TOPIC_BATCH_SIZE = 12
EVENT_TRACKING_MAX_UPDATE_ATTEMPTS = 3
EVENT_TRACKING_OUTBOX_MAX_ATTEMPTS = 8
EVENT_TRACKING_SOURCE = "event_tracking"

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# These are sources of reporting or observation, never official event channels.
THIRD_PARTY_HANDLE_MARKERS = {
    "lookonchain",
    "odaily",
    "chaincatcher",
    "blockbeats",
    "panews",
    "wu_blockchain",
    "cointelegraph",
    "coindesk",
    "decryptmedia",
    "theblock",
    "arkham",
}
TRACKING_TYPES = {
    "security_asset_incident",
    "official_dispute_or_denial",
    "exceptional_project_decision",
}
UPDATE_CLASSIFICATIONS = {"material_progress", "relevant_no_progress", "irrelevant"}
EVENT_NEWS_TYPES = {"regular", "onchain", "funding"}
OFFICIAL_RELATIONS = {
    "official_institution",
    "official_project",
    "official_security",
    "official_regulator",
    "directly_involved_person",
}


TOPIC_JUDGMENT_PROMPT_V2 = """你是 OdAIly 的“新闻值班追踪判断器”。

OdAIly 用 AI 代替新闻值班人员：从已经形成的热点中，立即判断哪些事件值得继续盯住，等待下一条真正有新闻价值的官方信息。自动追踪是少数例外，不是每个热点的默认下一步。Crypto 读者最关心资产、交易、资产安全和交易环境，以及高传播的花边、娱乐、冲突、指控、名人或机构争议。

只能输出 `track` 或 `do_not_track`，没有人工确认状态。只有同时满足以下条件才可 `track`：
1. 对读者的资产安全、交易环境、持仓判断，或高传播花边/娱乐/趣味事件有明确价值；
2. 事件尚未结束，不是标准化或一次性动作；
3. 有合理理由认为事件主体的官方机构/项目/安全/监管账号，或直接涉事者本人可验证的公开账号，会在未来数小时到数天回应、解释、辟谣、调查、处置、赔偿、公布结果或升级处理；
4. 下一条官方信息可能明显改变读者对“发生了什么、谁负责、钱怎么办、争议是真是假”的理解。

优先考虑被盗、攻击、假链/假桥、漏洞、重大冻结、交易所/链/协议异常、资产损失、严重服务事故；高关注度账号被盗、合作或授权真假、责任归属、重大指控、项目方与用户冲突，以及等待当事人回应的花边或趣味事件；仅在影响项目存续、用户资产、交易入口或协议命运时才考虑罕见重大迁移、重大回购/销毁或重大项目决定。

默认 `do_not_track`：普通项目上线、开放、关闭、解锁、投票、回购、销毁、产品/模型/论文发布；上线前数周或数月预热；价格涨跌、目标价、仓位、爆仓、巨鲸买入、做市商吸筹、一次性转账；只有媒体、Lookonchain、Odaily、ChainCatcher、KOL、研究员或社区会继续更新的链上观察；无明确主体的谣言、隐私传闻和人身攻击；长期法案或路线图。

账号限制：后续只能追踪官方机构/项目/安全/监管账号或直接涉事者本人公开账号。媒体、链上追踪者、KOL、研究员、社区账号永远不是最终追踪对象。不要猜测 X handle，账号发现由下一任务的 GPT Web Search 完成。

整体只输出一个合法 JSON 数组，不要 Markdown 或解释。每个对象结构：
{
  "topic_id":"输入编号",
  "topic_title":"热点标题",
  "decision":"track|do_not_track",
  "reader_value":"asset_trading|gossip_entertainment|both|none",
  "tracking_type":"security_asset_incident|official_dispute_or_denial|exceptional_project_decision|none",
  "event_identity":"可稳定复用的简短事件身份，不要 X handle；do_not_track 时为空字符串",
  "confirmed_facts":["仅输入支持的事实"],
  "unconfirmed_claims":["仍未确认的说法"],
  "reason":"简洁理由",
  "official_response_hypothesis":{"entities":["可能回应的主体，不写 handle"],"why_likely":"回应动机","next_information":"最值得等待的信息"},
  "recommended_initial_window_hours":1,
  "stop_conditions":["事件解决","24 小时无实质官方进展"],
  "confidence":0.0
}
当 decision 为 do_not_track：tracking_type 必须为 none，official_response_hypothesis 必须为 null，recommended_initial_window_hours 必须为 0。证据不足时输出 do_not_track。

输入热点：
{{TOPICS}}
"""


OFFICIAL_DISCOVERY_PROMPT_V1 = """你是 OdAIly 的官方 X 账号核验器。必须使用刚刚实际执行的 Web Search 结果，寻找下列事件中未来几天最可能发布实质进展的账号。

只可返回以下关系：official_institution、official_project、official_security、official_regulator、directly_involved_person。最后一种只限事件直接涉事者本人。媒体、记者、链上追踪者、研究员、KOL、社区、投资人和转述账号一律排除，即使他们信息更快也不能返回。

每个候选必须有能证明“该 X handle 属于该主体”的官方关系说明，至少一个实际 Web Search 引用 URL，且 X profile URL 与 handle 一致。不要猜 handle；无法完成严格核验就返回空 accounts。最多返回 3 个，优先事件主体，其次官方安全/处置主体，最后才是直接涉事者本人。

整体只输出一个 JSON 对象，不要 Markdown：
{
  "accounts":[{
    "handle":"不带@的 X handle",
    "display_name":"显示名",
    "official_entity":"主体名称",
    "official_relation":"official_institution|official_project|official_security|official_regulator|directly_involved_person",
    "role":"为什么该账号会给出后续",
    "x_profile_url":"https://x.com/handle",
    "official_evidence":"简洁说明官方归属或本人涉事关系",
    "official_evidence_urls":["必须来自本次 Web Search 实际引用的 URL"],
    "person_involvement":"仅 directly_involved_person 时说明其与事件的直接关系，否则为空字符串"
  }],
  "reason":"为何这些是唯一应追踪的官方账号"
}

事件：
{{EVENT}}
"""


UPDATE_MATERIALITY_PROMPT_V1 = """你是 OdAIly 的官方事件进展判断器。比较一条新官方 X 帖子与既有事件时间线，只能输出 `material_progress`、`relevant_no_progress` 或 `irrelevant`。

material_progress 必须含新增、可报道且能明显改变事件理解的事实，例如官方确认/否认、影响范围、资金处置、调查发现、风险控制、服务恢复、冻结追回、赔偿、责任结论、可验证证据或争议升级。重复、安抚、营销、转发、没有新增事实的旧结论换说法是 relevant_no_progress；无关内容是 irrelevant。未确认说法必须明确标为未确认，不能写成事实。

整体只输出一个 JSON 对象，不要 Markdown：
{
  "classification":"material_progress|relevant_no_progress|irrelevant",
  "news_type":"regular|onchain|funding",
  "fact_summary":"新增事实的准确摘要；非实质时为空字符串",
  "difference_from_timeline":"与既有信息的关键差异",
  "confirmed_facts":["已确认事实"],
  "unconfirmed_claims":["未确认说法"],
  "confidence":0.0,
  "reason":"判断理由"
}

事件：
{{EVENT}}

既有实质进展：
{{TIMELINE}}

新官方帖子：
{{POST}}
"""


EVENT_TRACKING_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_tracking_prompt_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prompt_key TEXT NOT NULL,
  version_number INTEGER NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(prompt_key, version_number)
);
CREATE TABLE IF NOT EXISTS event_tracking_events (
  event_id TEXT PRIMARY KEY,
  identity_key TEXT NOT NULL UNIQUE,
  identity_tokens_json TEXT NOT NULL,
  title TEXT NOT NULL,
  tracking_type TEXT NOT NULL CHECK(tracking_type IN ('security_asset_incident','official_dispute_or_denial','exceptional_project_decision')),
  reader_value TEXT NOT NULL,
  confirmed_facts_json TEXT NOT NULL DEFAULT '[]',
  unconfirmed_claims_json TEXT NOT NULL DEFAULT '[]',
  rationale TEXT NOT NULL,
  official_response_hypothesis_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK(status IN ('discovering','active','ended','discovery_failed','capacity_exhausted')),
  current_cycle_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_hot_topic_at TEXT,
  last_material_progress_at TEXT,
  ended_at TEXT,
  end_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_tracking_events_status ON event_tracking_events(status, updated_at DESC);
CREATE TABLE IF NOT EXISTS event_tracking_dismissals (
  event_id TEXT PRIMARY KEY REFERENCES event_tracking_events(event_id),
  identity_key TEXT NOT NULL UNIQUE,
  dismissed_at TEXT NOT NULL,
  dismissed_by TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_event_tracking_dismissals_identity ON event_tracking_dismissals(identity_key);
CREATE TABLE IF NOT EXISTS event_tracking_cycles (
  cycle_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES event_tracking_events(event_id),
  status TEXT NOT NULL CHECK(status IN ('discovering','active','ended')),
  started_at TEXT NOT NULL,
  initial_expires_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  hard_expires_at TEXT NOT NULL,
  last_material_progress_at TEXT,
  ended_at TEXT,
  end_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_tracking_cycles_status ON event_tracking_cycles(status, expires_at, hard_expires_at);
CREATE TABLE IF NOT EXISTS event_tracking_topic_assessments (
  topic_id TEXT PRIMARY KEY,
  snapshot_hash TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('track','do_not_track','failed')),
  tracking_type TEXT,
  event_identity TEXT,
  result_json TEXT NOT NULL DEFAULT '{}',
  prompt_version_id INTEGER REFERENCES event_tracking_prompt_versions(id),
  actual_model TEXT,
  usage_json TEXT NOT NULL DEFAULT '{}',
  duration_ms REAL,
  raw_output TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  next_attempt_at TEXT,
  judged_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_tracking_assessments_due ON event_tracking_topic_assessments(next_attempt_at, decision);
CREATE TABLE IF NOT EXISTS event_tracking_topic_links (
  event_id TEXT NOT NULL REFERENCES event_tracking_events(event_id),
  topic_id TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  linked_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(event_id, topic_id)
);
CREATE INDEX IF NOT EXISTS idx_event_tracking_topic_links_topic ON event_tracking_topic_links(topic_id);
CREATE TABLE IF NOT EXISTS event_tracking_account_discoveries (
  discovery_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES event_tracking_events(event_id),
  cycle_id TEXT NOT NULL REFERENCES event_tracking_cycles(cycle_id),
  status TEXT NOT NULL CHECK(status IN ('succeeded','failed','no_official_account','capacity_exhausted')),
  attempts INTEGER NOT NULL,
  prompt_version_id INTEGER REFERENCES event_tracking_prompt_versions(id),
  actual_model TEXT,
  usage_json TEXT NOT NULL DEFAULT '{}',
  duration_ms REAL,
  search_tool_calls_json TEXT NOT NULL DEFAULT '[]',
  citations_json TEXT NOT NULL DEFAULT '[]',
  result_json TEXT NOT NULL DEFAULT '{}',
  raw_output TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  UNIQUE(cycle_id)
);
CREATE TABLE IF NOT EXISTS event_tracking_accounts (
  handle_lower TEXT PRIMARY KEY,
  screen_name TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  official_entity TEXT NOT NULL,
  official_relation TEXT NOT NULL CHECK(official_relation IN ('official_institution','official_project','official_security','official_regulator','directly_involved_person')),
  official_evidence_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK(status IN ('active','inactive')),
  next_due_at TEXT,
  last_polled_at TEXT,
  last_success_at TEXT,
  last_error TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_item_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_tracking_accounts_due ON event_tracking_accounts(status, next_due_at);
CREATE TABLE IF NOT EXISTS event_tracking_bindings (
  cycle_id TEXT NOT NULL REFERENCES event_tracking_cycles(cycle_id),
  event_id TEXT NOT NULL REFERENCES event_tracking_events(event_id),
  handle_lower TEXT NOT NULL REFERENCES event_tracking_accounts(handle_lower),
  role TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active','released')),
  discovered_at TEXT NOT NULL,
  released_at TEXT,
  last_contributed_at TEXT,
  PRIMARY KEY(cycle_id, handle_lower)
);
CREATE INDEX IF NOT EXISTS idx_event_tracking_bindings_active ON event_tracking_bindings(status, handle_lower);
CREATE TABLE IF NOT EXISTS event_tracking_inbox (
  tweet_id TEXT PRIMARY KEY,
  handle_lower TEXT NOT NULL REFERENCES event_tracking_accounts(handle_lower),
  payload_json TEXT NOT NULL,
  collected_at TEXT NOT NULL,
  processed_at TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_tracking_inbox_pending ON event_tracking_inbox(processed_at, collected_at);
CREATE TABLE IF NOT EXISTS event_tracking_updates (
  update_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES event_tracking_events(event_id),
  cycle_id TEXT NOT NULL REFERENCES event_tracking_cycles(cycle_id),
  tweet_id TEXT NOT NULL REFERENCES event_tracking_inbox(tweet_id),
  handle_lower TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','processing','succeeded','failed')),
  classification TEXT CHECK(classification IN ('material_progress','relevant_no_progress','irrelevant')),
  news_type TEXT,
  fact_summary TEXT,
  difference_text TEXT,
  confirmed_facts_json TEXT NOT NULL DEFAULT '[]',
  unconfirmed_claims_json TEXT NOT NULL DEFAULT '[]',
  confidence REAL,
  reason TEXT,
  prompt_version_id INTEGER REFERENCES event_tracking_prompt_versions(id),
  actual_model TEXT,
  usage_json TEXT NOT NULL DEFAULT '{}',
  duration_ms REAL,
  raw_output TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  classified_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(cycle_id, tweet_id)
);
CREATE INDEX IF NOT EXISTS idx_event_tracking_updates_due ON event_tracking_updates(status, next_attempt_at, created_at);
CREATE TABLE IF NOT EXISTS event_tracking_publication_outbox (
  outbox_id TEXT PRIMARY KEY,
  update_id TEXT NOT NULL UNIQUE REFERENCES event_tracking_updates(update_id),
  event_id TEXT NOT NULL REFERENCES event_tracking_events(event_id),
  cycle_id TEXT NOT NULL REFERENCES event_tracking_cycles(cycle_id),
  tweet_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','submitting','submitted','duplicate','published','failed')),
  primary_task_id INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  submitted_at TEXT,
  cancelled_at TEXT,
  cancelled_by TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_tracking_outbox_due ON event_tracking_publication_outbox(status, next_attempt_at, created_at);
CREATE TABLE IF NOT EXISTS event_tracking_audit (
  audit_id TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  event_id TEXT,
  cycle_id TEXT,
  detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_tracking_audit_event ON event_tracking_audit(event_id, at DESC);
"""


@dataclass(frozen=True, slots=True)
class EventModelResult:
    text: str
    model: str
    usage: dict[str, Any]
    duration_ms: float
    raw_payload: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    citations: list[str]


class EventTrackingAI(Protocol):
    def generate_json(self, *, model: str, prompt: str) -> EventModelResult: ...

    def web_search_json(self, *, model: str, prompt: str) -> EventModelResult: ...


class EventTrackingTaskDispatcher(Protocol):
    def ensure_task(self, *, event: dict[str, Any], cycle: dict[str, Any], update: dict[str, Any], post: dict[str, Any]) -> int: ...

    def submit(self, *, task_id: int, tweet_id: str) -> None: ...

    def task_status(self, task_id: int) -> str | None: ...

    def is_event_tracking_cancelled(self, task_id: int) -> bool: ...

    def task_detail(self, task_id: int) -> dict[str, Any] | None: ...

    def dismiss_event_tasks(self, *, event_id: str, dismissed_at: str, dismissed_by: str) -> dict[str, Any]: ...


class OpenAIEventTrackingAI:
    """Typed Responses adapter; Web Search success requires actual tool output."""

    def __init__(self, *, api_key: str, base_url: str, timeout_seconds: float = 90.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> "OpenAIEventTrackingAI | None":
        api_key = (
            os.getenv("HOTTOPIC_EVENT_OPENAI_API_KEY")
            or os.getenv("HOTTOPIC_OPENAI_API_KEY")
            or os.getenv("ODAILY_LLM_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        if not api_key:
            return None
        base_url = (
            os.getenv("HOTTOPIC_EVENT_OPENAI_BASE_URL")
            or os.getenv("HOTTOPIC_OPENAI_BASE_URL")
            or os.getenv("ODAILY_LLM_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        )
        timeout = _float_env("HOTTOPIC_EVENT_REQUEST_TIMEOUT_SECONDS", 90.0, minimum=1.0, maximum=180.0)
        return cls(api_key=api_key, base_url=base_url, timeout_seconds=timeout)

    def generate_json(self, *, model: str, prompt: str) -> EventModelResult:
        return self._request(model=model, prompt=prompt, web_search=False)

    def web_search_json(self, *, model: str, prompt: str) -> EventModelResult:
        result = self._request(model=model, prompt=prompt, web_search=True)
        if not result.tool_calls:
            raise ValueError("Responses result did not contain a web_search_call")
        if not result.citations:
            raise ValueError("Responses web search result did not contain citations")
        return result

    def _request(self, *, model: str, prompt: str, web_search: bool) -> EventModelResult:
        payload: dict[str, Any] = {"model": model, "input": prompt}
        if web_search:
            # Do not silently use a generic text response as a search result.
            payload["tools"] = [{"type": "web_search"}]
        started = time.perf_counter()
        response = requests.post(
            self._responses_url(),
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, dict):
            raise ValueError("Responses result was not an object")
        text = _extract_response_text(raw)
        tool_calls = [
            dict(item)
            for item in raw.get("output") or []
            if isinstance(item, dict) and str(item.get("type") or "") == "web_search_call"
        ]
        citations = _response_citations(raw)
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        return EventModelResult(
            text=text,
            model=str(raw.get("model") or model),
            usage=dict(usage),
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            raw_payload=raw,
            tool_calls=tool_calls,
            citations=citations,
        )

    def _responses_url(self) -> str:
        return self.base_url if self.base_url.endswith("/responses") else f"{self.base_url}/responses"


class SQLiteEventTrackingTaskDispatcher:
    """Narrow cross-database adapter for material official posts only."""

    def __init__(self, database_path: Path, *, pipeline_client: Any | None = None) -> None:
        self.database_path = database_path
        self.pipeline_client = pipeline_client

    def ensure_task(self, *, event: dict[str, Any], cycle: dict[str, Any], update: dict[str, Any], post: dict[str, Any]) -> int:
        # The main SQLite schema is initialized by its owning repository.  This
        # is intentionally a small adapter rather than a second task writer.
        from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository

        SQLiteXProcessingRepository(self.database_path)
        now = utc_iso()
        tweet_id = str(post["tweet_id"])
        metadata = {
            "source_kind": EVENT_TRACKING_SOURCE,
            "event_tracking": {
                "event_id": event["event_id"],
                "cycle_id": cycle["cycle_id"],
                "update_id": update["update_id"],
                "classification": update["classification"],
                "fact_summary": update.get("fact_summary") or "",
                "difference_from_timeline": update.get("difference_text") or "",
                "confirmed_facts": json_value(update.get("confirmed_facts_json"), []),
                "unconfirmed_claims": json_value(update.get("unconfirmed_claims_json"), []),
                "official_handle": post.get("account_screen_name") or post.get("author_screen_name") or update["handle_lower"],
                "auto_publish": True,
            },
        }
        raw_payload = {
            "event_tracking": metadata["event_tracking"],
            "official_post": post,
        }
        title = f"{event['title']}：官方进展"
        content = str(post.get("expanded_text") or post.get("text") or "").strip()
        source_url = str(post.get("url") or "").strip() or None
        published_at = str(post.get("created_at_iso") or post.get("created_at") or now)
        if not content:
            raise ValueError("event tracking post had no content")
        with connect_sqlite(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO tasks(
                  source,source_item_id,source_url,title,content,published_at,raw_payload,metadata,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?, 'judged',?,?)
                """,
                (
                    EVENT_TRACKING_SOURCE,
                    tweet_id,
                    source_url,
                    title,
                    content,
                    published_at,
                    json.dumps(raw_payload, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM tasks WHERE source=? AND source_item_id=?",
                (EVENT_TRACKING_SOURCE, tweet_id),
            ).fetchone()
            if row is None:  # pragma: no cover - guarded by the unique insert.
                conn.rollback()
                raise RuntimeError("event tracking task was not persisted")
            task_id = int(row["id"])
            conn.execute("INSERT OR IGNORE INTO x_task_pipeline(task_id) VALUES(?)", (task_id,))
            if cursor.rowcount:
                judge_output = {
                    "route": update["news_type"],
                    "discard_type": "none",
                    "rule_set": "event_tracking_material_progress",
                    "event_tracking": metadata["event_tracking"],
                }
                conn.execute(
                    """
                    UPDATE x_task_pipeline
                    SET news_type=?,judge_model=?,judge_output=?,judge_completed_at=?,last_error=NULL,updated_at=?
                    WHERE task_id=?
                    """,
                    (
                        update["news_type"],
                        "event_tracking",
                        json.dumps(judge_output, ensure_ascii=False, separators=(",", ":")),
                        now,
                        now,
                        task_id,
                    ),
                )
            conn.commit()
        return task_id

    def submit(self, *, task_id: int, tweet_id: str) -> None:
        pipeline_client = self.pipeline_client
        if pipeline_client is None:
            from packages.local_pipeline.client import LocalPipelineClient

            pipeline_client = LocalPipelineClient()
        pipeline_client.submit_job(
            job_type="write_flow",
            task_id=task_id,
            source=EVENT_TRACKING_SOURCE,
            source_item_id=tweet_id,
        )

    def task_status(self, task_id: int) -> str | None:
        try:
            with connect_sqlite(self.database_path) as conn:
                row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            return str(row["status"]) if row is not None else None
        except sqlite3.Error:
            return None

    def is_event_tracking_cancelled(self, task_id: int) -> bool:
        try:
            with connect_sqlite(self.database_path) as conn:
                row = conn.execute(
                    "SELECT source,status,metadata FROM tasks WHERE id=?",
                    (task_id,),
                ).fetchone()
            if row is None or str(row["source"]) != EVENT_TRACKING_SOURCE:
                return False
            metadata = json_value(row["metadata"], {})
            event_metadata = metadata.get("event_tracking") if isinstance(metadata, dict) else None
            return str(row["status"]) == "event_tracking_cancelled" or bool(
                isinstance(event_metadata, dict) and event_metadata.get("dismissed_at")
            )
        except sqlite3.Error:
            return False

    def task_detail(self, task_id: int) -> dict[str, Any] | None:
        try:
            with connect_sqlite(self.database_path) as conn:
                row = conn.execute(
                    """
                    SELECT t.id,t.source,t.source_item_id,t.source_url,t.status,t.metadata,
                           t.updated_at,p.draft_title,p.draft_content,p.final_title,p.final_content,
                           p.publisher_decision,p.publisher_reason_code,p.publisher_decided_at,
                           p.publish_completed_at,p.last_error
                    FROM tasks t
                    LEFT JOIN x_task_pipeline p ON p.task_id=t.id
                    WHERE t.id=?
                    """,
                    (task_id,),
                ).fetchone()
            if row is None:
                return None
            return {
                "taskId": int(row["id"]),
                "sourceItemId": str(row["source_item_id"]),
                "sourceUrl": row["source_url"],
                "status": str(row["status"]),
                "updatedAt": row["updated_at"],
                "draftTitle": row["draft_title"],
                "draftContent": row["draft_content"],
                "finalTitle": row["final_title"],
                "finalContent": row["final_content"],
                "publisherDecision": row["publisher_decision"],
                "publisherReasonCode": row["publisher_reason_code"],
                "publisherDecidedAt": row["publisher_decided_at"],
                "publishedAt": row["publish_completed_at"],
                "error": row["last_error"],
            }
        except sqlite3.Error:
            return None

    def dismiss_event_tasks(self, *, event_id: str, dismissed_at: str, dismissed_by: str) -> dict[str, Any]:
        cancelled: list[int] = []
        published: list[int] = []
        terminal: list[int] = []
        with connect_sqlite(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id,status,metadata FROM tasks WHERE source=? AND json_extract(metadata,'$.event_tracking.event_id')=?",
                (EVENT_TRACKING_SOURCE, event_id),
            ).fetchall()
            for row in rows:
                task_id = int(row["id"])
                metadata = json_value(row["metadata"], {})
                event_metadata = metadata.get("event_tracking")
                if not isinstance(event_metadata, dict):
                    event_metadata = {}
                    metadata["event_tracking"] = event_metadata
                event_metadata["dismissed_at"] = dismissed_at
                event_metadata["dismissed_by"] = dismissed_by
                status = str(row["status"])
                if status == "auto_published":
                    published.append(task_id)
                elif status in {"duplicate", "discarded", "expired", "legacy_skipped"}:
                    terminal.append(task_id)
                else:
                    cancelled.append(task_id)
                next_status = "event_tracking_cancelled" if task_id in cancelled else status
                conn.execute(
                    "UPDATE tasks SET metadata=?,status=?,locked_by=NULL,locked_until=NULL,updated_at=? WHERE id=?",
                    (compact_json(metadata), next_status, dismissed_at, task_id),
                )
            conn.commit()
        return {"cancelledTaskIds": cancelled, "publishedTaskIds": published, "terminalTaskIds": terminal}


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(UTC).isoformat()


def json_value(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name) or default)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() not in {"0", "false", "no", "off"}


def _parse_json_output(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


def _extract_response_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                parts.append(str(content["text"]))
    text = "\n".join(part.strip() for part in parts if part.strip()).strip()
    if not text:
        raise ValueError("Responses result did not contain output_text")
    return text


def _response_citations(payload: Any) -> list[str]:
    found: list[str] = []

    def visit(value: Any, *, citation_context: bool = False) -> None:
        if isinstance(value, dict):
            value_type = str(value.get("type") or "")
            is_citation = citation_context or value_type in {"url_citation", "web_search_result", "web_search_call"}
            for key, nested in value.items():
                if key in {"url", "source_url", "href"} and isinstance(nested, str) and URL_RE.match(nested):
                    # URLs produced in plain model text are not citations.
                    # They count only when attached to a web-search result,
                    # source list, or Responses URL-citation annotation.
                    if is_citation:
                        found.append(nested)
                else:
                    visit(nested, citation_context=is_citation or key in {"annotations", "sources"})
        elif isinstance(value, list):
            for nested in value:
                visit(nested, citation_context=citation_context)

    visit(payload)
    return list(dict.fromkeys(found))


def _datetime(value: str | None, *, fallback: datetime | None = None) -> datetime:
    if value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return fallback or utc_now()


def _is_obvious_third_party(handle: str) -> bool:
    normalized = handle.lower().strip().lstrip("@")
    return normalized in THIRD_PARTY_HANDLE_MARKERS or any(marker in normalized for marker in THIRD_PARTY_HANDLE_MARKERS)


class EventTracker:
    """Deep event-tracking module with a deliberately small caller surface.

    ``observe_topics`` consumes HotTopic changes, ``poll_due_accounts`` accepts
    a collector adapter, and the remaining methods advance durable work.  All
    model/network work happens outside SQLite transactions; callers never need
    to understand the individual tables or retry states.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        primary_database_path: Path,
        ai: EventTrackingAI | None = None,
        dispatcher: EventTrackingTaskDispatcher | None = None,
        now: Callable[[], datetime] = utc_now,
        enabled: bool | None = None,
        owns_connection: bool = False,
    ) -> None:
        self.db = connection
        self._owns_connection = owns_connection
        self.now = now
        self.enabled = _bool_env("HOTTOPIC_EVENT_TRACKING_ENABLED", True) if enabled is None else enabled
        self.ai = ai if ai is not None else OpenAIEventTrackingAI.from_environment()
        self.dispatcher = dispatcher or SQLiteEventTrackingTaskDispatcher(primary_database_path)
        self.topic_model = os.getenv("HOTTOPIC_EVENT_TOPIC_MODEL") or "gpt-5.6-luna"
        self.topic_fallback_model = os.getenv("HOTTOPIC_EVENT_TOPIC_FALLBACK_MODEL") or "gpt-5.6-terra"
        self.update_model = os.getenv("HOTTOPIC_EVENT_UPDATE_MODEL") or self.topic_model
        self.update_fallback_model = os.getenv("HOTTOPIC_EVENT_UPDATE_FALLBACK_MODEL") or self.topic_fallback_model
        self.discovery_model = os.getenv("HOTTOPIC_EVENT_WEB_SEARCH_MODEL") or self.topic_model
        self.discovery_retry_model = os.getenv("HOTTOPIC_EVENT_WEB_SEARCH_RETRY_MODEL") or self.topic_fallback_model
        self.db.executescript(EVENT_TRACKING_SCHEMA)
        self._ensure_event_tracking_schema()
        self._seed_prompt_versions()

    def close(self) -> None:
        if self._owns_connection:
            self.db.close()

    def _ensure_event_tracking_schema(self) -> None:
        columns = {str(row["name"]) for row in self.db.execute("PRAGMA table_info(event_tracking_publication_outbox)").fetchall()}
        with self.db:
            if "cancelled_at" not in columns:
                self.db.execute("ALTER TABLE event_tracking_publication_outbox ADD COLUMN cancelled_at TEXT")
            if "cancelled_by" not in columns:
                self.db.execute("ALTER TABLE event_tracking_publication_outbox ADD COLUMN cancelled_by TEXT")

    def _seed_prompt_versions(self) -> None:
        now = utc_iso(self.now())
        versions = (
            ("topic_judgment", 2, TOPIC_JUDGMENT_PROMPT_V2),
            ("official_discovery", 1, OFFICIAL_DISCOVERY_PROMPT_V1),
            ("update_materiality", 1, UPDATE_MATERIALITY_PROMPT_V1),
        )
        with self.db:
            for prompt_key, version_number, content in versions:
                self.db.execute(
                    "INSERT OR IGNORE INTO event_tracking_prompt_versions(prompt_key,version_number,content,created_at) VALUES(?,?,?,?)",
                    (prompt_key, version_number, content, now),
                )

    def _prompt(self, prompt_key: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM event_tracking_prompt_versions WHERE prompt_key=? ORDER BY version_number DESC LIMIT 1",
            (prompt_key,),
        ).fetchone()
        if row is None:  # pragma: no cover - protected by _seed_prompt_versions.
            raise RuntimeError(f"event tracking prompt is missing: {prompt_key}")
        return row

    def _audit(self, kind: str, detail: dict[str, Any], *, event_id: str | None = None, cycle_id: str | None = None) -> None:
        at = utc_iso(self.now())
        self.db.execute(
            "INSERT INTO event_tracking_audit(audit_id,at,kind,event_id,cycle_id,detail_json) VALUES(?,?,?,?,?,?)",
            (stable_id("event-audit", kind, event_id or "", cycle_id or "", at, compact_json(detail)), at, kind, event_id, cycle_id, compact_json(detail)),
        )

    def _topic_snapshots(self, topic_ids: Iterable[str] | None) -> list[dict[str, Any]]:
        requested = sorted({str(item) for item in topic_ids or [] if str(item)})
        params: list[Any] = []
        where = "t.matching_status='active' AND t.visibility='visible'"
        if requested:
            where += f" AND t.topic_id IN ({','.join('?' for _ in requested)})"
            params.extend(requested)
        # First rollout and later model failures intentionally inspect active
        # topics as well; Python compares the durable snapshot hash below.
        rows = self.db.execute(
            f"""
            SELECT t.*,b.title AS brief_title,b.brief,b.generated_at
            FROM topics t
            LEFT JOIN brief_revisions b ON b.topic_id=t.topic_id
              AND b.revision=(SELECT MAX(x.revision) FROM brief_revisions x WHERE x.topic_id=t.topic_id)
            WHERE {where}
            ORDER BY t.hotness_score DESC,t.last_evidence_at DESC
            LIMIT 100
            """,
            params,
        ).fetchall()
        snapshots: list[dict[str, Any]] = []
        for row in rows:
            topic_id = str(row["topic_id"])
            evidence_rows = self.db.execute(
                """
                SELECT c.claim_text,c.action_or_issue,c.stance,c.evidence_span,c.created_at,
                       ci.activity_account,ci.expanded_text,ci.source_url
                FROM topic_evidence te
                JOIN claims c ON c.claim_id=te.claim_id
                JOIN content_items ci ON ci.content_item_id=c.content_item_id
                WHERE te.topic_id=?
                ORDER BY c.created_at DESC
                LIMIT 10
                """,
                (topic_id,),
            ).fetchall()
            participants = self.db.execute(
                "SELECT activity_account FROM topic_participations WHERE topic_id=? ORDER BY last_participation_at DESC LIMIT 20",
                (topic_id,),
            ).fetchall()
            snapshot = {
                "topic_id": topic_id,
                "title": str(row["brief_title"] or row["working_title"]),
                "brief": str(row["brief"] or ""),
                "canonical_subject": str(row["canonical_subject"]),
                "core_entities": json_value(row["core_entities_json"], []),
                "event_or_issue": str(row["event_or_issue"]),
                "started_at": str(row["started_at"]),
                "last_evidence_at": row["last_evidence_at"],
                "hotness": float(row["hotness_score"] or 0),
                "participants": [str(item["activity_account"]) for item in participants],
                "evidence": [
                    {
                        "claim": str(item["claim_text"]),
                        "action_or_issue": str(item["action_or_issue"]),
                        "stance": str(item["stance"]),
                        "evidence": str(item["evidence_span"]),
                        "account": str(item["activity_account"]),
                        "text": str(item["expanded_text"]),
                        "source_url": str(item["source_url"]),
                        "created_at": str(item["created_at"]),
                    }
                    for item in evidence_rows
                ],
            }
            snapshot["snapshot_hash"] = hashlib.sha256(compact_json(snapshot).encode("utf-8")).hexdigest()
            snapshots.append(snapshot)
        return snapshots

    def observe_topics(self, topic_ids: Iterable[str] | None = None) -> dict[str, int]:
        """Run task 1 for changed or as-yet-unassessed visible HotTopics."""
        if not self.enabled:
            return {"assessed": 0, "tracked": 0, "failed": 0}
        snapshots = self._topic_snapshots(topic_ids)
        pending: list[dict[str, Any]] = []
        current = utc_iso(self.now())
        for snapshot in snapshots:
            assessment = self.db.execute(
                "SELECT snapshot_hash,decision,next_attempt_at,actual_model FROM event_tracking_topic_assessments WHERE topic_id=?",
                (snapshot["topic_id"],),
            ).fetchone()
            if assessment is not None and assessment["actual_model"] == "operator_override":
                continue
            if assessment is not None and assessment["snapshot_hash"] == snapshot["snapshot_hash"]:
                if assessment["decision"] in {"track", "do_not_track"}:
                    continue
                if assessment["next_attempt_at"] and str(assessment["next_attempt_at"]) > current:
                    continue
            pending.append(snapshot)
        pending = pending[:EVENT_TRACKING_TOPIC_BATCH_SIZE]
        if not pending:
            return {"assessed": 0, "tracked": 0, "failed": 0}

        prompt_version = self._prompt("topic_judgment")
        prompt = str(prompt_version["content"]).replace("{{TOPICS}}", compact_json(pending))
        try:
            result = self._call_text(prompt=prompt, models=(self.topic_model, self.topic_fallback_model))
            parsed = _parse_json_output(result.text)
            if not isinstance(parsed, list):
                raise ValueError("topic judgment output must be a JSON array")
            decisions = self._parse_topic_decisions(parsed, pending)
        except Exception as exc:
            self._record_topic_assessment_failure(pending, prompt_version, exc)
            return {"assessed": 0, "tracked": 0, "failed": len(pending)}

        tracked = 0
        with self.db:
            for snapshot in pending:
                decision = decisions.get(snapshot["topic_id"])
                if decision is None:
                    decision = self._do_not_track_decision(snapshot, "模型未返回该热点的有效判断")
                self._store_topic_assessment(snapshot, decision, prompt_version, result)
                if decision["decision"] == "track":
                    self._link_tracked_topic(snapshot, decision)
                    tracked += 1
            self._audit("topic_judgment_completed", {"topic_count": len(pending), "tracked": tracked})
        return {"assessed": len(pending), "tracked": tracked, "failed": 0}

    def start_tracking_topic(
        self,
        topic_id: str,
        *,
        actor: str,
        reason: str,
        tracking_type: str,
        reader_value: str,
        confirmed_facts: list[str],
        unconfirmed_claims: list[str],
        official_response_hypothesis: dict[str, Any],
    ) -> str:
        """Start a single operator-selected HotTopic through normal discovery and lifecycle."""
        if not self.enabled:
            raise RuntimeError("event tracking is disabled")
        if not actor.strip() or not reason.strip():
            raise ValueError("operator and reason are required")
        if tracking_type not in TRACKING_TYPES or reader_value not in {"asset_trading", "gossip_entertainment", "both"}:
            raise ValueError("invalid tracking type or reader value")
        if not isinstance(official_response_hypothesis, dict) or not _string_list(official_response_hypothesis.get("entities")):
            raise ValueError("official response hypothesis needs named entities")
        snapshots = self._topic_snapshots([topic_id])
        if len(snapshots) != 1 or snapshots[0]["topic_id"] != topic_id:
            raise ValueError("visible active HotTopic not found")
        snapshot = snapshots[0]
        existing = self._find_event(snapshot)
        if existing is not None and self._is_event_dismissed(str(existing["event_id"])):
            raise ValueError("tracking event was dismissed")
        if existing is None and self._is_identity_dismissed(stable_id("event-topic", topic_id)):
            raise ValueError("tracking topic was dismissed")
        assessment = self.db.execute(
            "SELECT actual_model FROM event_tracking_topic_assessments WHERE topic_id=?", (topic_id,)
        ).fetchone()
        if assessment is not None and assessment["actual_model"] == "operator_override" and existing is not None:
            return str(existing["event_id"])
        decision = {
            "topic_id": topic_id,
            "decision": "track",
            "tracking_type": tracking_type,
            "reader_value": reader_value,
            "event_identity": topic_id,
            "confirmed_facts": _string_list(confirmed_facts),
            "unconfirmed_claims": _string_list(unconfirmed_claims),
            "reason": reason.strip(),
            "official_response_hypothesis": official_response_hypothesis,
            "recommended_initial_window_hours": 72,
            "stop_conditions": ["事件解决", "24 小时无实质官方进展"],
            "confidence": 1.0,
            "operator": actor.strip(),
        }
        model_result = EventModelResult(compact_json(decision), "operator_override", {}, 0.0, {}, [], [])
        with self.db:
            self._store_topic_assessment(snapshot, decision, self._prompt("topic_judgment"), model_result)
            self._link_tracked_topic(snapshot, decision)
            linked = self._find_event(snapshot)
            if linked is None:
                raise RuntimeError("operator-selected topic was not linked to an event")
            event_id = str(linked["event_id"])
            self._audit(
                "operator_topic_tracking_started",
                {"topic_id": topic_id, "actor": actor.strip(), "reason": reason.strip()},
                event_id=event_id,
            )
        return event_id

    def _parse_topic_decisions(self, parsed: list[Any], snapshots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        allowed = {item["topic_id"]: item for item in snapshots}
        result: dict[str, dict[str, Any]] = {}
        for raw in parsed:
            if not isinstance(raw, dict):
                continue
            topic_id = str(raw.get("topic_id") or "")
            if topic_id not in allowed:
                continue
            decision = str(raw.get("decision") or "").strip()
            if decision not in {"track", "do_not_track"}:
                continue
            tracking_type = str(raw.get("tracking_type") or "none").strip()
            if decision == "track":
                hypothesis = raw.get("official_response_hypothesis")
                if tracking_type not in TRACKING_TYPES or not isinstance(hypothesis, dict) or not hypothesis.get("entities"):
                    result[topic_id] = self._do_not_track_decision(allowed[topic_id], "追踪条件或官方回应预期不足")
                    continue
                window = raw.get("recommended_initial_window_hours")
                try:
                    window_int = int(window)
                except (TypeError, ValueError):
                    window_int = 0
                if not 1 <= window_int <= 72:
                    result[topic_id] = self._do_not_track_decision(allowed[topic_id], "初始观察窗口无效")
                    continue
            else:
                tracking_type = "none"
                raw["official_response_hypothesis"] = None
                raw["recommended_initial_window_hours"] = 0
            result[topic_id] = {
                "topic_id": topic_id,
                "decision": decision,
                "tracking_type": tracking_type,
                "reader_value": str(raw.get("reader_value") or "none"),
                "event_identity": str(raw.get("event_identity") or "").strip(),
                "confirmed_facts": _string_list(raw.get("confirmed_facts")),
                "unconfirmed_claims": _string_list(raw.get("unconfirmed_claims")),
                "reason": str(raw.get("reason") or "").strip() or "模型未提供理由",
                "official_response_hypothesis": raw.get("official_response_hypothesis"),
                "recommended_initial_window_hours": int(raw.get("recommended_initial_window_hours") or 0),
                "stop_conditions": _string_list(raw.get("stop_conditions")),
                "confidence": _confidence(raw.get("confidence")),
            }
        return result

    @staticmethod
    def _do_not_track_decision(snapshot: dict[str, Any], reason: str) -> dict[str, Any]:
        return {
            "topic_id": snapshot["topic_id"],
            "decision": "do_not_track",
            "tracking_type": "none",
            "reader_value": "none",
            "event_identity": "",
            "confirmed_facts": [],
            "unconfirmed_claims": [],
            "reason": reason,
            "official_response_hypothesis": None,
            "recommended_initial_window_hours": 0,
            "stop_conditions": [],
            "confidence": 0.0,
        }

    def _call_text(self, *, prompt: str, models: tuple[str, str]) -> EventModelResult:
        if self.ai is None:
            raise RuntimeError("event tracking GPT client is not configured")
        errors: list[str] = []
        for model in dict.fromkeys(models):
            try:
                return self.ai.generate_json(model=model, prompt=prompt)
            except Exception as exc:
                errors.append(f"{model}: {type(exc).__name__}: {exc}")
        raise RuntimeError("; ".join(errors))

    def _record_topic_assessment_failure(self, snapshots: list[dict[str, Any]], prompt: sqlite3.Row, error: Exception) -> None:
        now = self.now()
        retry_at = utc_iso(now + timedelta(minutes=15))
        message = f"{type(error).__name__}: {error}"[:2000]
        with self.db:
            for snapshot in snapshots:
                self.db.execute(
                    """
                    INSERT INTO event_tracking_topic_assessments(
                      topic_id,snapshot_hash,snapshot_json,decision,prompt_version_id,attempts,last_error,next_attempt_at,created_at,updated_at
                    ) VALUES(?,?,?,'failed',?,1,?,?,?,?)
                    ON CONFLICT(topic_id) DO UPDATE SET
                      snapshot_hash=excluded.snapshot_hash,snapshot_json=excluded.snapshot_json,decision='failed',
                      prompt_version_id=excluded.prompt_version_id,attempts=event_tracking_topic_assessments.attempts+1,
                      last_error=excluded.last_error,next_attempt_at=excluded.next_attempt_at,updated_at=excluded.updated_at
                    """,
                    (snapshot["topic_id"], snapshot["snapshot_hash"], compact_json(snapshot), int(prompt["id"]), message, retry_at, utc_iso(now), utc_iso(now)),
                )
            self._audit("topic_judgment_failed", {"topic_ids": [item["topic_id"] for item in snapshots], "error": message})

    def _store_topic_assessment(self, snapshot: dict[str, Any], decision: dict[str, Any], prompt: sqlite3.Row, result: EventModelResult) -> None:
        now = utc_iso(self.now())
        payload = dict(decision)
        self.db.execute(
            """
            INSERT INTO event_tracking_topic_assessments(
              topic_id,snapshot_hash,snapshot_json,decision,tracking_type,event_identity,result_json,prompt_version_id,
              actual_model,usage_json,duration_ms,raw_output,attempts,last_error,next_attempt_at,judged_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,NULL,NULL,?,?,?)
            ON CONFLICT(topic_id) DO UPDATE SET
              snapshot_hash=excluded.snapshot_hash,snapshot_json=excluded.snapshot_json,decision=excluded.decision,
              tracking_type=excluded.tracking_type,event_identity=excluded.event_identity,result_json=excluded.result_json,
              prompt_version_id=excluded.prompt_version_id,actual_model=excluded.actual_model,usage_json=excluded.usage_json,
              duration_ms=excluded.duration_ms,raw_output=excluded.raw_output,attempts=event_tracking_topic_assessments.attempts+1,
              last_error=NULL,next_attempt_at=NULL,judged_at=excluded.judged_at,updated_at=excluded.updated_at
            """,
            (
                snapshot["topic_id"], snapshot["snapshot_hash"], compact_json(snapshot), decision["decision"],
                None if decision["tracking_type"] == "none" else decision["tracking_type"], decision["event_identity"],
                compact_json(payload), int(prompt["id"]), result.model, compact_json(result.usage), result.duration_ms,
                result.text[:30000], now, now, now,
            ),
        )

    def _link_tracked_topic(self, snapshot: dict[str, Any], decision: dict[str, Any]) -> None:
        event = self._find_event(snapshot)
        now = self.now()
        if event is not None and self._is_event_dismissed(str(event["event_id"])):
            self._audit(
                "tracked_topic_suppressed",
                {"topic_id": snapshot["topic_id"], "identity_key": event["identity_key"]},
                event_id=str(event["event_id"]),
            )
            return
        if event is None:
            identity_key = stable_id("event-topic", snapshot["topic_id"])
            if self._is_identity_dismissed(identity_key):
                self._audit("tracked_topic_suppressed", {"topic_id": snapshot["topic_id"], "identity_key": identity_key})
                return
            event_id = stable_id("tracking-event", identity_key)
            now_text = utc_iso(now)
            self.db.execute(
                """
                INSERT INTO event_tracking_events(
                  event_id,identity_key,identity_tokens_json,title,tracking_type,reader_value,confirmed_facts_json,
                  unconfirmed_claims_json,rationale,official_response_hypothesis_json,status,current_cycle_id,
                  created_at,updated_at,last_hot_topic_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?, 'discovering',NULL,?,?,?)
                """,
                (
                    event_id, identity_key, compact_json([snapshot["topic_id"]]), snapshot["title"], decision["tracking_type"], decision["reader_value"],
                    compact_json(decision["confirmed_facts"]), compact_json(decision["unconfirmed_claims"]), decision["reason"],
                    compact_json(decision["official_response_hypothesis"] or {}), now_text, now_text, now_text,
                ),
            )
            event = self.db.execute("SELECT * FROM event_tracking_events WHERE event_id=?", (event_id,)).fetchone()
            self._open_cycle(dict(event), now)
            event = self.db.execute("SELECT * FROM event_tracking_events WHERE event_id=?", (event_id,)).fetchone()
            self._audit("event_created", {"topic_id": snapshot["topic_id"], "tracking_type": decision["tracking_type"]}, event_id=event_id)
        else:
            event_id = str(event["event_id"])
            self.db.execute(
                "UPDATE event_tracking_events SET updated_at=?,last_hot_topic_at=?,title=COALESCE(NULLIF(?,''),title) WHERE event_id=?",
                (utc_iso(now), utc_iso(now), snapshot["title"], event_id),
            )
            if str(event["status"]) in {"ended", "discovery_failed", "capacity_exhausted"}:
                self._open_cycle(dict(event), now)
                self._audit("event_cycle_reopened", {"topic_id": snapshot["topic_id"]}, event_id=event_id)
        event_id = str(event["event_id"])
        now_text = utc_iso(now)
        self.db.execute(
            """
            INSERT INTO event_tracking_topic_links(event_id,topic_id,snapshot_hash,snapshot_json,linked_at,updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(event_id,topic_id) DO UPDATE SET snapshot_hash=excluded.snapshot_hash,snapshot_json=excluded.snapshot_json,updated_at=excluded.updated_at
            """,
            (event_id, snapshot["topic_id"], snapshot["snapshot_hash"], compact_json(snapshot), now_text, now_text),
        )

    def _is_event_dismissed(self, event_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM event_tracking_dismissals WHERE event_id=? LIMIT 1", (event_id,)
        ).fetchone() is not None

    def _is_identity_dismissed(self, identity_key: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM event_tracking_dismissals WHERE identity_key=? LIMIT 1", (identity_key,)
        ).fetchone() is not None

    def _find_event(self, snapshot: dict[str, Any]) -> sqlite3.Row | None:
        return self.db.execute(
            """
            SELECT e.* FROM event_tracking_topic_links l
            JOIN event_tracking_events e ON e.event_id=l.event_id
            WHERE l.topic_id=? ORDER BY e.updated_at DESC LIMIT 1
            """,
            (snapshot["topic_id"],),
        ).fetchone()

    def reconcile_topic_merges(self) -> dict[str, int]:
        """Make one tracking cycle owner follow the surviving HotTopic."""
        merges = self.db.execute(
            "SELECT source_topic_id,target_topic_id FROM topic_merges ORDER BY created_at,merge_id"
        ).fetchall()
        moved = retired = 0
        for merge in merges:
            source_id, target_id = merge["source_topic_id"], merge["target_topic_id"]
            source = self.db.execute(
                "SELECT e.* FROM event_tracking_topic_links l JOIN event_tracking_events e ON e.event_id=l.event_id "
                "WHERE l.topic_id=? ORDER BY e.updated_at DESC LIMIT 1", (source_id,),
            ).fetchone()
            if source is None:
                continue
            linked_topics = {
                row["topic_id"] for row in self.db.execute(
                    "SELECT topic_id FROM event_tracking_topic_links WHERE event_id=?", (source["event_id"],)
                )
            }
            if linked_topics - {source_id, target_id}:
                continue
            target = self.db.execute(
                "SELECT e.* FROM event_tracking_topic_links l JOIN event_tracking_events e ON e.event_id=l.event_id "
                "WHERE l.topic_id=? ORDER BY e.updated_at DESC LIMIT 1", (target_id,),
            ).fetchone()
            if target is not None and target["event_id"] != source["event_id"]:
                target_links = {
                    row["topic_id"] for row in self.db.execute(
                        "SELECT topic_id FROM event_tracking_topic_links WHERE event_id=?", (target["event_id"],)
                    )
                }
                if target_links - {source_id, target_id}:
                    continue
            if target is not None and target["event_id"] == source["event_id"]:
                continue
            winner = source
            if target is not None and target["event_id"] != source["event_id"]:
                def rank(event: sqlite3.Row) -> tuple[int, int, str]:
                    published = self.db.execute(
                        "SELECT COUNT(*) FROM event_tracking_publication_outbox WHERE event_id=? AND status IN ('submitted','published')",
                        (event["event_id"],),
                    ).fetchone()[0]
                    return (int(published > 0), int(event["status"] == "active"), str(event["created_at"]))

                winner, loser = (source, target) if rank(source) > rank(target) else (target, source)
                self._retire_merged_event(str(loser["event_id"]), str(winner["event_id"]))
                retired += 1
            now_text = utc_iso(self.now())
            with self.db:
                if target is None or target["event_id"] != winner["event_id"]:
                    prior = self.db.execute(
                        "SELECT snapshot_hash,snapshot_json FROM event_tracking_topic_links WHERE event_id=? AND topic_id=?",
                        (winner["event_id"], source_id),
                    ).fetchone()
                    if prior:
                        self.db.execute(
                            "INSERT OR IGNORE INTO event_tracking_topic_links(event_id,topic_id,snapshot_hash,snapshot_json,linked_at,updated_at) "
                            "VALUES(?,?,?,?,?,?)",
                            (winner["event_id"], target_id, prior["snapshot_hash"], prior["snapshot_json"], now_text, now_text),
                        )
                        moved += 1
                self.db.execute(
                    "DELETE FROM event_tracking_topic_links WHERE topic_id=? AND event_id<>?",
                    (target_id, winner["event_id"]),
                )
                self.db.execute(
                    "DELETE FROM event_tracking_topic_links WHERE topic_id=?",
                    (source_id,),
                )
                self._audit("topic_merge_tracking_reconciled", {"source_topic_id": source_id, "target_topic_id": target_id}, event_id=winner["event_id"])
        return {"moved": moved, "retired": retired}

    def _retire_merged_event(self, event_id: str, winner_event_id: str) -> None:
        now_text = utc_iso(self.now())
        cancel_tasks = getattr(self.dispatcher, "dismiss_event_tasks", None)
        if callable(cancel_tasks):
            cancel_tasks(event_id=event_id, dismissed_at=now_text, dismissed_by="topic_merge")
        with self.db:
            for cycle in self.db.execute(
                "SELECT * FROM event_tracking_cycles WHERE event_id=? AND status IN ('discovering','active')", (event_id,),
            ).fetchall():
                self._end_cycle(dict(cycle), "topic_merged", now_text)
            self.db.execute(
                "UPDATE event_tracking_events SET status='ended',ended_at=?,end_reason='topic_merged',updated_at=? WHERE event_id=?",
                (now_text, now_text, event_id),
            )
            self.db.execute(
                "UPDATE event_tracking_publication_outbox SET cancelled_at=?,cancelled_by='topic_merge',next_attempt_at=NULL,updated_at=? "
                "WHERE event_id=? AND cancelled_at IS NULL AND status NOT IN ('published','duplicate')",
                (now_text, now_text, event_id),
            )
            self.db.execute(
                "UPDATE event_tracking_accounts SET status='inactive',next_due_at=NULL,updated_at=? "
                "WHERE status='active' AND NOT EXISTS (SELECT 1 FROM event_tracking_bindings b "
                "WHERE b.handle_lower=event_tracking_accounts.handle_lower AND b.status='active')",
                (now_text,),
            )
            self._audit("event_retired_after_topic_merge", {"winner_event_id": winner_event_id}, event_id=event_id)

    def _open_cycle(self, event: dict[str, Any], now: datetime) -> str:
        cycle_id = stable_id("tracking-cycle", event["event_id"], utc_iso(now))
        initial = now + EVENT_TRACKING_INITIAL_WINDOW
        hard_cap = now + EVENT_TRACKING_HARD_CAP
        now_text = utc_iso(now)
        self.db.execute(
            """
            INSERT INTO event_tracking_cycles(
              cycle_id,event_id,status,started_at,initial_expires_at,expires_at,hard_expires_at,created_at,updated_at
            ) VALUES(?,?, 'discovering',?,?,?,?,?,?)
            """,
            (cycle_id, event["event_id"], now_text, utc_iso(initial), utc_iso(initial), utc_iso(hard_cap), now_text, now_text),
        )
        self.db.execute(
            "UPDATE event_tracking_events SET status='discovering',current_cycle_id=?,ended_at=NULL,end_reason=NULL,updated_at=? WHERE event_id=?",
            (cycle_id, now_text, event["event_id"]),
        )
        return cycle_id

    def discover_official_accounts(self, *, event_id: str | None = None) -> dict[str, int]:
        """Run task 0 for newly opened cycles; a failed search ends autonomously."""
        if not self.enabled:
            return {"discovered": 0, "failed": 0, "bound": 0}
        rows = self.db.execute(
            """
            SELECT c.*,e.title,e.tracking_type,e.reader_value,e.confirmed_facts_json,e.unconfirmed_claims_json,
                   e.rationale,e.official_response_hypothesis_json
            FROM event_tracking_cycles c
            JOIN event_tracking_events e ON e.event_id=c.event_id
            LEFT JOIN event_tracking_account_discoveries d ON d.cycle_id=c.cycle_id
            WHERE c.status='discovering' AND d.discovery_id IS NULL AND (? IS NULL OR c.event_id=?)
            ORDER BY c.created_at
            LIMIT 12
            """,
            (event_id, event_id),
        ).fetchall()
        discovered = failed = bound = 0
        for row in rows:
            outcome = self._discover_cycle(dict(row))
            discovered += 1
            failed += int(outcome["status"] != "succeeded")
            bound += int(outcome["bound"])
        return {"discovered": discovered, "failed": failed, "bound": bound}

    def _discover_cycle(self, row: dict[str, Any]) -> dict[str, Any]:
        prompt_version = self._prompt("official_discovery")
        links = self.db.execute(
            "SELECT snapshot_json FROM event_tracking_topic_links WHERE event_id=? ORDER BY updated_at DESC LIMIT 8",
            (row["event_id"],),
        ).fetchall()
        event_payload = {
            "event_id": row["event_id"],
            "title": row["title"],
            "tracking_type": row["tracking_type"],
            "reader_value": row["reader_value"],
            "confirmed_facts": json_value(row["confirmed_facts_json"], []),
            "unconfirmed_claims": json_value(row["unconfirmed_claims_json"], []),
            "rationale": row["rationale"],
            "official_response_hypothesis": json_value(row["official_response_hypothesis_json"], {}),
            "hot_topic_evidence": [json_value(item["snapshot_json"], {}) for item in links],
        }
        prompt = str(prompt_version["content"]).replace("{{EVENT}}", compact_json(event_payload))
        result: EventModelResult | None = None
        error: Exception | None = None
        attempts = 0
        # This is intentionally exactly one initial Web Search plus one bounded
        # retry.  A text-only answer is not a successful discovery.
        if self.ai is None:
            error = RuntimeError("event tracking GPT Web Search client is not configured")
        else:
            for model in dict.fromkeys((self.discovery_model, self.discovery_retry_model)):
                attempts += 1
                try:
                    result = self.ai.web_search_json(model=model, prompt=prompt)
                    if not result.tool_calls or not result.citations:
                        raise ValueError("Web Search did not return both tool calls and citations")
                    break
                except Exception as exc:
                    error = exc
                    result = None
        now = self.now()
        if result is None:
            message = f"{type(error).__name__}: {error}" if error else "unknown Web Search failure"
            with self.db:
                self._finish_discovery(
                    row,
                    prompt_version,
                    status="failed",
                    result=None,
                    payload={},
                    error=message,
                    bound=0,
                    now=now,
                    attempts=attempts,
                )
            return {"status": "failed", "bound": 0}
        try:
            parsed = _parse_json_output(result.text)
            if not isinstance(parsed, dict):
                raise ValueError("official discovery output must be a JSON object")
            accounts = self._verified_official_accounts(parsed.get("accounts"), citations=result.citations)
        except Exception as exc:
            with self.db:
                self._finish_discovery(
                    row,
                    prompt_version,
                    status="failed",
                    result=result,
                    payload={},
                    error=f"{type(exc).__name__}: {exc}",
                    bound=0,
                    now=now,
                    attempts=attempts,
                )
            return {"status": "failed", "bound": 0}

        if not accounts:
            with self.db:
                self._finish_discovery(
                    row,
                    prompt_version,
                    status="no_official_account",
                    result=result,
                    payload={"reason": str(parsed.get("reason") or ""), "accounts": []},
                    error=None,
                    bound=0,
                    now=now,
                    attempts=attempts,
                )
            return {"status": "no_official_account", "bound": 0}

        selected, capacity_exhausted = self._select_account_capacity(accounts)
        if not selected:
            with self.db:
                self._finish_discovery(
                    row,
                    prompt_version,
                    status="capacity_exhausted" if capacity_exhausted else "no_official_account",
                    result=result,
                    payload={"reason": str(parsed.get("reason") or ""), "accounts": accounts},
                    error=None,
                    bound=0,
                    now=now,
                    attempts=attempts,
                )
            return {"status": "capacity_exhausted" if capacity_exhausted else "no_official_account", "bound": 0}

        with self.db:
            now_text = utc_iso(now)
            for account in selected:
                self.db.execute(
                    """
                    INSERT INTO event_tracking_accounts(
                      handle_lower,screen_name,display_name,official_entity,official_relation,official_evidence_json,status,next_due_at,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?, 'active',?,?,?)
                    ON CONFLICT(handle_lower) DO UPDATE SET
                      screen_name=excluded.screen_name,display_name=CASE WHEN excluded.display_name='' THEN event_tracking_accounts.display_name ELSE excluded.display_name END,
                      official_entity=excluded.official_entity,official_relation=excluded.official_relation,
                      official_evidence_json=excluded.official_evidence_json,status='active',next_due_at=COALESCE(event_tracking_accounts.next_due_at,excluded.next_due_at),updated_at=excluded.updated_at
                    """,
                    (
                        account["handle"].lower(), account["handle"], account["display_name"], account["official_entity"],
                        account["official_relation"], compact_json(account), now_text, now_text, now_text,
                    ),
                )
                self.db.execute(
                    """
                    INSERT INTO event_tracking_bindings(cycle_id,event_id,handle_lower,role,status,discovered_at)
                    VALUES(?,?,?,?, 'active',?)
                    ON CONFLICT(cycle_id,handle_lower) DO UPDATE SET role=excluded.role,status='active',released_at=NULL
                    """,
                    (row["cycle_id"], row["event_id"], account["handle"].lower(), account["role"], now_text),
                )
            self.db.execute(
                "UPDATE event_tracking_cycles SET status='active',updated_at=? WHERE cycle_id=?",
                (now_text, row["cycle_id"]),
            )
            self.db.execute(
                "UPDATE event_tracking_events SET status='active',updated_at=? WHERE event_id=?",
                (now_text, row["event_id"]),
            )
            self._finish_discovery(
                row,
                prompt_version,
                status="succeeded",
                result=result,
                payload={"reason": str(parsed.get("reason") or ""), "accounts": selected},
                error=None,
                bound=len(selected),
                now=now,
                within_transaction=True,
                attempts=attempts,
            )
        return {"status": "succeeded", "bound": len(selected)}

    def _verified_official_accounts(self, value: Any, *, citations: list[str]) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        cited = {_canonical_url(item) for item in citations if URL_RE.match(item)}
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in value:
            if not isinstance(raw, dict):
                continue
            handle = str(raw.get("handle") or "").strip().lstrip("@")
            if not HANDLE_RE.fullmatch(handle) or _is_obvious_third_party(handle):
                continue
            lower = handle.lower()
            if lower in seen:
                continue
            relation = str(raw.get("official_relation") or "").strip()
            if relation not in OFFICIAL_RELATIONS:
                continue
            entity = str(raw.get("official_entity") or "").strip()
            evidence = str(raw.get("official_evidence") or "").strip()
            role = str(raw.get("role") or "").strip()
            profile_url = str(raw.get("x_profile_url") or "").strip()
            evidence_urls = [
                _canonical_url(item)
                for item in _string_list(raw.get("official_evidence_urls"))
                if URL_RE.match(item)
            ]
            if not entity or not evidence or not role or not _is_matching_x_profile(profile_url, handle):
                continue
            if not evidence_urls or not any(item in cited for item in evidence_urls):
                continue
            person_involvement = str(raw.get("person_involvement") or "").strip()
            if relation == "directly_involved_person" and not person_involvement:
                continue
            result.append(
                {
                    "handle": handle,
                    "display_name": str(raw.get("display_name") or "").strip(),
                    "official_entity": entity,
                    "official_relation": relation,
                    "role": role,
                    "x_profile_url": profile_url,
                    "official_evidence": evidence,
                    "official_evidence_urls": evidence_urls,
                    "person_involvement": person_involvement,
                }
            )
            seen.add(lower)
            if len(result) >= EVENT_TRACKING_MAX_ACCOUNTS_PER_EVENT:
                break
        return result

    def _select_account_capacity(self, accounts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        active_rows = self.db.execute(
            "SELECT DISTINCT handle_lower FROM event_tracking_bindings WHERE status='active'"
        ).fetchall()
        active = {str(row["handle_lower"]) for row in active_rows}
        selected: list[dict[str, Any]] = []
        exhausted = False
        for account in accounts[:EVENT_TRACKING_MAX_ACCOUNTS_PER_EVENT]:
            handle = account["handle"].lower()
            if handle not in active and len(active) >= EVENT_TRACKING_MAX_ACTIVE_ACCOUNTS:
                exhausted = True
                continue
            selected.append(account)
            active.add(handle)
        return selected, exhausted

    def _finish_discovery(
        self,
        row: dict[str, Any],
        prompt: sqlite3.Row,
        *,
        status: str,
        result: EventModelResult | None,
        payload: dict[str, Any],
        error: str | None,
        bound: int,
        now: datetime,
        within_transaction: bool = False,
        attempts: int = 1,
    ) -> None:
        del within_transaction  # The caller decides whether the surrounding connection is already transactional.
        now_text = utc_iso(now)
        result = result or EventModelResult("", "", {}, 0.0, {}, [], [])
        self.db.execute(
            """
            INSERT INTO event_tracking_account_discoveries(
              discovery_id,event_id,cycle_id,status,attempts,prompt_version_id,actual_model,usage_json,duration_ms,
              search_tool_calls_json,citations_json,result_json,raw_output,error,created_at,completed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cycle_id) DO NOTHING
            """,
            (
                stable_id("discovery", row["cycle_id"]), row["event_id"], row["cycle_id"], status, max(1, attempts),
                int(prompt["id"]), result.model or None, compact_json(result.usage), result.duration_ms,
                compact_json(result.tool_calls), compact_json(result.citations), compact_json(payload), result.text[:30000] or None,
                (error or "")[:2000] or None, now_text, now_text,
            ),
        )
        if status == "succeeded":
            self._audit("official_accounts_discovered", {"bound": bound}, event_id=row["event_id"], cycle_id=row["cycle_id"])
            return
        end_reason = "capacity_exhausted" if status == "capacity_exhausted" else "discovery_failed"
        event_status = "capacity_exhausted" if status == "capacity_exhausted" else "discovery_failed"
        self.db.execute(
            "UPDATE event_tracking_cycles SET status='ended',ended_at=?,end_reason=?,updated_at=? WHERE cycle_id=?",
            (now_text, end_reason, now_text, row["cycle_id"]),
        )
        self.db.execute(
            "UPDATE event_tracking_events SET status=?,ended_at=?,end_reason=?,updated_at=? WHERE event_id=?",
            (event_status, now_text, end_reason, now_text, row["event_id"]),
        )
        self._audit(
            "official_discovery_failed",
            {"status": status, "error": error, "bound": bound},
            event_id=row["event_id"],
            cycle_id=row["cycle_id"],
        )

    def due_accounts(self, *, limit: int = EVENT_TRACKING_MAX_ACTIVE_ACCOUNTS) -> list[tuple[AccountRow, datetime]]:
        now_text = utc_iso(self.now())
        rows = self.db.execute(
            """
            SELECT a.*,MIN(c.started_at) AS cutoff_at
            FROM event_tracking_accounts a
            JOIN event_tracking_bindings b ON b.handle_lower=a.handle_lower AND b.status='active'
            JOIN event_tracking_cycles c ON c.cycle_id=b.cycle_id AND c.status='active'
            WHERE a.status='active' AND (a.next_due_at IS NULL OR a.next_due_at<=?)
            GROUP BY a.handle_lower
            ORDER BY COALESCE(a.next_due_at,''),a.handle_lower
            LIMIT ?
            """,
            (now_text, max(1, min(limit, EVENT_TRACKING_MAX_ACTIVE_ACCOUNTS))),
        ).fetchall()
        return [
            (
                AccountRow(
                    screen_name=str(row["screen_name"]),
                    name=str(row["display_name"]),
                    description="",
                    followers_count=0,
                    statuses_count=0,
                    protected=False,
                    url=f"https://x.com/{row['screen_name']}",
                ),
                _datetime(str(row["cutoff_at"]), fallback=self.now()),
            )
            for row in rows
        ]

    def poll_due_accounts(
        self,
        scanner: Callable[[AccountRow, datetime], tuple[list[ContentItem], Any, dict[str, Any] | None]],
        *,
        workers: int = EVENT_TRACKING_MAX_ACTIVE_ACCOUNTS,
    ) -> dict[str, int]:
        """Poll official accounts before the caller polls baseline HotTopic accounts."""
        if not self.enabled:
            return {"polled": 0, "accepted": 0, "errors": 0}
        due = self.due_accounts()
        if not due:
            return {"polled": 0, "accepted": 0, "errors": 0}
        accepted = errors = 0
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(due))), thread_name_prefix="event-tracking") as executor:
            futures = {executor.submit(scanner, account, cutoff): (account, cutoff) for account, cutoff in due}
            for future in as_completed(futures):
                account, _cutoff = futures[future]
                try:
                    items, _latest, error = future.result()
                except Exception as exc:
                    items, error = [], {"kind": "exception", "error": str(exc)}
                accepted += self._store_event_poll(account, items, error)
                errors += int(error is not None)
        return {"polled": len(due), "accepted": accepted, "errors": errors}

    def _store_event_poll(self, account: AccountRow, items: list[ContentItem], error: dict[str, Any] | None) -> int:
        lower = account.screen_name.lower()
        now = self.now()
        now_text = utc_iso(now)
        # An event account can only contribute its own public posts.  This is
        # stricter than the general HotTopic collector because a quoted third
        # party must never acquire official-source status by proximity.
        own_items = [item for item in items if item.author_screen_name.lower() == lower]
        with self.db:
            current = self.db.execute(
                "SELECT consecutive_failures,last_success_at FROM event_tracking_accounts WHERE handle_lower=?",
                (lower,),
            ).fetchone()
            if current is None:
                return 0
            accepted = 0
            for item in own_items:
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO event_tracking_inbox(tweet_id,handle_lower,payload_json,collected_at) VALUES(?,?,?,?)",
                    (item.tweet_id, lower, compact_json(item.__dict__), now_text),
                )
                accepted += int(cursor.rowcount or 0)
            failures = 0 if error is None else int(current["consecutive_failures"] or 0) + 1
            delay = EVENT_TRACKING_POLL_INTERVAL if error is None else min(
                timedelta(hours=6), EVENT_TRACKING_POLL_INTERVAL * (2 ** min(failures, 6))
            )
            self.db.execute(
                """
                UPDATE event_tracking_accounts
                SET next_due_at=?,last_polled_at=?,last_success_at=?,last_error=?,consecutive_failures=?,last_item_count=?,updated_at=?
                WHERE handle_lower=?
                """,
                (
                    utc_iso(now + delay), now_text, now_text if error is None else current["last_success_at"],
                    compact_json(error) if error else None, failures, len(own_items), now_text, lower,
                ),
            )
            if error:
                self._audit("official_account_poll_failed", {"handle": account.screen_name, "error": error})
        return accepted

    def classify_official_updates(self, *, limit: int = 30) -> dict[str, int]:
        """Classify each official post against every active event binding."""
        if not self.enabled:
            return {"classified": 0, "material": 0, "failed": 0}
        now_text = utc_iso(self.now())
        inbox_rows = self.db.execute(
            """
            SELECT * FROM event_tracking_inbox
            WHERE processed_at IS NULL
            ORDER BY collected_at,tweet_id
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()
        classified = material = failed = 0
        for inbox in inbox_rows:
            post = json_value(inbox["payload_json"], {})
            bindings = self.db.execute(
                """
                SELECT b.event_id,b.cycle_id,e.title,e.tracking_type,e.reader_value,e.confirmed_facts_json,
                       e.unconfirmed_claims_json,e.rationale,e.official_response_hypothesis_json,c.started_at,c.hard_expires_at
                FROM event_tracking_bindings b
                JOIN event_tracking_cycles c ON c.cycle_id=b.cycle_id AND c.status='active'
                JOIN event_tracking_events e ON e.event_id=b.event_id AND e.status='active'
                WHERE b.status='active' AND b.handle_lower=?
                """,
                (inbox["handle_lower"],),
            ).fetchall()
            if not bindings:
                with self.db:
                    self.db.execute("UPDATE event_tracking_inbox SET processed_at=? WHERE tweet_id=?", (now_text, inbox["tweet_id"]))
                continue
            all_terminal = True
            for binding in bindings:
                outcome = self._classify_one_update(dict(binding), dict(inbox), post)
                classified += int(outcome["classified"])
                material += int(outcome["material"])
                failed += int(outcome["failed"])
                all_terminal = all_terminal and bool(outcome["terminal"])
            if all_terminal:
                with self.db:
                    self.db.execute("UPDATE event_tracking_inbox SET processed_at=?,error=NULL WHERE tweet_id=?", (utc_iso(self.now()), inbox["tweet_id"]))
        return {"classified": classified, "material": material, "failed": failed}

    def _classify_one_update(self, binding: dict[str, Any], inbox: dict[str, Any], post: dict[str, Any]) -> dict[str, bool]:
        if self._is_event_dismissed(str(binding["event_id"])):
            return {"classified": False, "material": False, "failed": False, "terminal": True}
        existing = self.db.execute(
            "SELECT * FROM event_tracking_updates WHERE cycle_id=? AND tweet_id=?",
            (binding["cycle_id"], inbox["tweet_id"]),
        ).fetchone()
        now = self.now()
        now_text = utc_iso(now)
        if existing is not None:
            status = str(existing["status"])
            if status == "succeeded":
                return {"classified": False, "material": str(existing["classification"]) == "material_progress", "failed": False, "terminal": True}
            if int(existing["attempts"] or 0) >= EVENT_TRACKING_MAX_UPDATE_ATTEMPTS:
                return {"classified": False, "material": False, "failed": True, "terminal": True}
            if existing["next_attempt_at"] and str(existing["next_attempt_at"]) > now_text:
                return {"classified": False, "material": False, "failed": False, "terminal": False}
            update_id = str(existing["update_id"])
            with self.db:
                self.db.execute(
                    "UPDATE event_tracking_updates SET status='processing',attempts=attempts+1,updated_at=? WHERE update_id=?",
                    (now_text, update_id),
                )
        else:
            update_id = stable_id("event-update", binding["cycle_id"], inbox["tweet_id"])
            with self.db:
                self.db.execute(
                    """
                    INSERT INTO event_tracking_updates(
                      update_id,event_id,cycle_id,tweet_id,handle_lower,status,attempts,created_at,updated_at
                    ) VALUES(?,?,?,?,?,'processing',1,?,?)
                    """,
                    (update_id, binding["event_id"], binding["cycle_id"], inbox["tweet_id"], inbox["handle_lower"], now_text, now_text),
                )

        prompt_version = self._prompt("update_materiality")
        timeline_rows = self.db.execute(
            """
            SELECT u.tweet_id,u.handle_lower,u.fact_summary,u.difference_text,u.confirmed_facts_json,u.unconfirmed_claims_json,u.classified_at,
                   i.payload_json
            FROM event_tracking_updates u
            LEFT JOIN event_tracking_inbox i ON i.tweet_id=u.tweet_id
            WHERE u.event_id=? AND u.classification='material_progress' AND u.status='succeeded'
            ORDER BY u.classified_at DESC LIMIT 8
            """,
            (binding["event_id"],),
        ).fetchall()
        timeline = [
            {
                "tweet_id": item["tweet_id"],
                "handle": item["handle_lower"],
                "fact_summary": item["fact_summary"],
                "difference": item["difference_text"],
                "confirmed_facts": json_value(item["confirmed_facts_json"], []),
                "unconfirmed_claims": json_value(item["unconfirmed_claims_json"], []),
                "classified_at": item["classified_at"],
                "post": json_value(item["payload_json"], {}),
            }
            for item in timeline_rows
        ]
        event_payload = {
            "event_id": binding["event_id"],
            "title": binding["title"],
            "tracking_type": binding["tracking_type"],
            "reader_value": binding["reader_value"],
            "confirmed_facts": json_value(binding["confirmed_facts_json"], []),
            "unconfirmed_claims": json_value(binding["unconfirmed_claims_json"], []),
            "rationale": binding["rationale"],
            "official_response_hypothesis": json_value(binding["official_response_hypothesis_json"], {}),
        }
        prompt = str(prompt_version["content"])
        prompt = prompt.replace("{{EVENT}}", compact_json(event_payload))
        prompt = prompt.replace("{{TIMELINE}}", compact_json(timeline))
        prompt = prompt.replace("{{POST}}", compact_json(post))
        try:
            result = self._call_text(prompt=prompt, models=(self.update_model, self.update_fallback_model))
            parsed = _parse_json_output(result.text)
            decision = self._parse_update_decision(parsed)
        except Exception as exc:
            attempts_row = self.db.execute("SELECT attempts FROM event_tracking_updates WHERE update_id=?", (update_id,)).fetchone()
            attempts = int(attempts_row["attempts"] or 0) if attempts_row else EVENT_TRACKING_MAX_UPDATE_ATTEMPTS
            terminal = attempts >= EVENT_TRACKING_MAX_UPDATE_ATTEMPTS
            with self.db:
                self.db.execute(
                    """
                    UPDATE event_tracking_updates
                    SET status='failed',last_error=?,next_attempt_at=?,updated_at=? WHERE update_id=?
                    """,
                    (
                        f"{type(exc).__name__}: {exc}"[:2000],
                        None if terminal else utc_iso(now + timedelta(minutes=10 * attempts)), utc_iso(now), update_id,
                    ),
                )
                self._audit("official_update_judgment_failed", {"tweet_id": inbox["tweet_id"], "error": f"{type(exc).__name__}: {exc}"}, event_id=binding["event_id"], cycle_id=binding["cycle_id"])
            return {"classified": False, "material": False, "failed": True, "terminal": terminal}

        with self.db:
            self.db.execute(
                """
                UPDATE event_tracking_updates
                SET status='succeeded',classification=?,news_type=?,fact_summary=?,difference_text=?,confirmed_facts_json=?,
                    unconfirmed_claims_json=?,confidence=?,reason=?,prompt_version_id=?,actual_model=?,usage_json=?,duration_ms=?,
                    raw_output=?,last_error=NULL,next_attempt_at=NULL,classified_at=?,updated_at=?
                WHERE update_id=?
                """,
                (
                    decision["classification"], decision["news_type"], decision["fact_summary"], decision["difference_from_timeline"],
                    compact_json(decision["confirmed_facts"]), compact_json(decision["unconfirmed_claims"]), decision["confidence"], decision["reason"],
                    int(prompt_version["id"]), result.model, compact_json(result.usage), result.duration_ms, result.text[:30000], now_text, now_text, update_id,
                ),
            )
            if decision["classification"] == "material_progress":
                if self._is_event_dismissed(str(binding["event_id"])):
                    return {"classified": True, "material": False, "failed": False, "terminal": True}
                material_at = _datetime(str(post.get("created_at_iso") or post.get("created_at") or now_text), fallback=now)
                cycle = self.db.execute("SELECT * FROM event_tracking_cycles WHERE cycle_id=?", (binding["cycle_id"],)).fetchone()
                if cycle is not None:
                    prior_expiry = _datetime(str(cycle["expires_at"]), fallback=now)
                    hard_cap = _datetime(str(cycle["hard_expires_at"]), fallback=now)
                    expires_at = min(hard_cap, max(prior_expiry, material_at + EVENT_TRACKING_PROGRESS_EXTENSION))
                    self.db.execute(
                        "UPDATE event_tracking_cycles SET last_material_progress_at=?,expires_at=?,updated_at=? WHERE cycle_id=?",
                        (utc_iso(material_at), utc_iso(expires_at), now_text, binding["cycle_id"]),
                    )
                    self.db.execute(
                        "UPDATE event_tracking_events SET last_material_progress_at=?,updated_at=? WHERE event_id=?",
                        (utc_iso(material_at), now_text, binding["event_id"]),
                    )
                self.db.execute(
                    """
                    INSERT OR IGNORE INTO event_tracking_publication_outbox(
                      outbox_id,update_id,event_id,cycle_id,tweet_id,status,attempts,next_attempt_at,created_at,updated_at
                    ) VALUES(?,?,?,?,?,'pending',0,?,?,?)
                    """,
                    (stable_id("event-outbox", update_id), update_id, binding["event_id"], binding["cycle_id"], inbox["tweet_id"], now_text, now_text, now_text),
                )
                self.db.execute(
                    "UPDATE event_tracking_bindings SET last_contributed_at=? WHERE cycle_id=? AND handle_lower=?",
                    (now_text, binding["cycle_id"], inbox["handle_lower"]),
                )
                self._audit("material_official_progress", {"tweet_id": inbox["tweet_id"], "fact_summary": decision["fact_summary"]}, event_id=binding["event_id"], cycle_id=binding["cycle_id"])
        return {"classified": True, "material": decision["classification"] == "material_progress", "failed": False, "terminal": True}

    def _parse_update_decision(self, parsed: Any) -> dict[str, Any]:
        if not isinstance(parsed, dict):
            raise ValueError("update judgment output must be a JSON object")
        classification = str(parsed.get("classification") or "").strip()
        if classification not in UPDATE_CLASSIFICATIONS:
            raise ValueError("invalid official update classification")
        news_type = str(parsed.get("news_type") or "regular").strip()
        if news_type not in EVENT_NEWS_TYPES:
            raise ValueError("invalid event tracking news_type")
        fact_summary = str(parsed.get("fact_summary") or "").strip()
        difference = str(parsed.get("difference_from_timeline") or "").strip()
        reason = str(parsed.get("reason") or "").strip()
        if classification == "material_progress" and (not fact_summary or not difference or not reason):
            raise ValueError("material progress requires summary, difference, and reason")
        return {
            "classification": classification,
            "news_type": news_type,
            "fact_summary": fact_summary,
            "difference_from_timeline": difference,
            "confirmed_facts": _string_list(parsed.get("confirmed_facts")),
            "unconfirmed_claims": _string_list(parsed.get("unconfirmed_claims")),
            "confidence": _confidence(parsed.get("confidence")),
            "reason": reason or "模型未提供理由",
        }

    def dispatch_publications(self, *, limit: int = 20) -> dict[str, int]:
        """Persist a main-pipeline task first, then enqueue it with tweet-level idempotency."""
        if not self.enabled:
            return {"submitted": 0, "failed": 0, "duplicate": 0, "published": 0}
        self.refresh_publication_statuses()
        now = self.now()
        rows = self.db.execute(
            """
            SELECT o.*,u.*,e.title AS event_title,e.tracking_type,c.started_at AS cycle_started_at,i.payload_json
            FROM event_tracking_publication_outbox o
            JOIN event_tracking_updates u ON u.update_id=o.update_id
            JOIN event_tracking_events e ON e.event_id=o.event_id
            JOIN event_tracking_cycles c ON c.cycle_id=o.cycle_id
            JOIN event_tracking_inbox i ON i.tweet_id=o.tweet_id
            WHERE o.cancelled_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM event_tracking_dismissals d WHERE d.event_id=o.event_id)
              AND o.status IN ('pending','failed') AND (o.next_attempt_at IS NULL OR o.next_attempt_at<=?)
              AND o.attempts<?
            ORDER BY o.created_at LIMIT ?
            """,
            (utc_iso(now), EVENT_TRACKING_OUTBOX_MAX_ATTEMPTS, max(1, limit)),
        ).fetchall()
        submitted = failed = 0
        for row in rows:
            outcome = self._dispatch_one(dict(row))
            submitted += int(outcome == "submitted")
            failed += int(outcome == "failed")
        refreshed = self.refresh_publication_statuses()
        return {"submitted": submitted, "failed": failed, **refreshed}

    def _dispatch_one(self, row: dict[str, Any]) -> str:
        now = self.now()
        now_text = utc_iso(now)
        if self._is_event_dismissed(str(row["event_id"])):
            with self.db:
                self.db.execute(
                    "UPDATE event_tracking_publication_outbox SET cancelled_at=?,cancelled_by='system',next_attempt_at=NULL,updated_at=? WHERE outbox_id=? AND cancelled_at IS NULL",
                    (now_text, now_text, row["outbox_id"]),
                )
            return "cancelled"
        with self.db:
            self.db.execute(
                "UPDATE event_tracking_publication_outbox SET status='submitting',attempts=attempts+1,updated_at=? WHERE outbox_id=?",
                (now_text, row["outbox_id"]),
            )
        event = {"event_id": row["event_id"], "title": row["event_title"], "tracking_type": row["tracking_type"]}
        cycle = {"cycle_id": row["cycle_id"], "started_at": row["cycle_started_at"]}
        update = {
            "update_id": row["update_id"], "classification": row["classification"], "news_type": row["news_type"],
            "fact_summary": row["fact_summary"], "difference_text": row["difference_text"],
            "confirmed_facts_json": row["confirmed_facts_json"], "unconfirmed_claims_json": row["unconfirmed_claims_json"],
            "handle_lower": row["handle_lower"],
        }
        post = json_value(row["payload_json"], {})
        try:
            task_id = int(row["primary_task_id"] or self.dispatcher.ensure_task(event=event, cycle=cycle, update=update, post=post))
            if self._is_event_dismissed(str(row["event_id"])):
                dismiss_tasks = getattr(self.dispatcher, "dismiss_event_tasks", None)
                if callable(dismiss_tasks):
                    dismiss_tasks(event_id=str(row["event_id"]), dismissed_at=now_text, dismissed_by="system")
                with self.db:
                    self.db.execute(
                        "UPDATE event_tracking_publication_outbox SET cancelled_at=?,cancelled_by='system',next_attempt_at=NULL,updated_at=? WHERE outbox_id=? AND cancelled_at IS NULL",
                        (now_text, now_text, row["outbox_id"]),
                    )
                return "cancelled"
            self.dispatcher.submit(task_id=task_id, tweet_id=str(row["tweet_id"]))
        except Exception as exc:
            attempts = int(row["attempts"] or 0) + 1
            with self.db:
                self.db.execute(
                    """
                    UPDATE event_tracking_publication_outbox
                    SET status='failed',primary_task_id=COALESCE(primary_task_id,?),last_error=?,next_attempt_at=?,updated_at=?
                    WHERE outbox_id=?
                    """,
                    (row.get("primary_task_id"), f"{type(exc).__name__}: {exc}"[:2000], utc_iso(now + timedelta(minutes=min(60, 2 ** attempts))), utc_iso(now), row["outbox_id"]),
                )
                self._audit("event_publication_dispatch_failed", {"tweet_id": row["tweet_id"], "error": f"{type(exc).__name__}: {exc}"}, event_id=row["event_id"], cycle_id=row["cycle_id"])
            return "failed"
        with self.db:
            self.db.execute(
                """
                UPDATE event_tracking_publication_outbox
                SET status='submitted',primary_task_id=?,submitted_at=?,last_error=NULL,next_attempt_at=NULL,updated_at=?
                WHERE outbox_id=?
                """,
                (task_id, now_text, now_text, row["outbox_id"]),
            )
            self._audit("event_publication_submitted", {"tweet_id": row["tweet_id"], "task_id": task_id}, event_id=row["event_id"], cycle_id=row["cycle_id"])
        return "submitted"

    def refresh_publication_statuses(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT outbox_id,primary_task_id,status,event_id,cycle_id FROM event_tracking_publication_outbox WHERE primary_task_id IS NOT NULL AND cancelled_at IS NULL"
        ).fetchall()
        duplicate = published = 0
        with self.db:
            for row in rows:
                status = self.dispatcher.task_status(int(row["primary_task_id"]))
                if status == "duplicate" and row["status"] != "duplicate":
                    self.db.execute(
                        "UPDATE event_tracking_publication_outbox SET status='duplicate',updated_at=? WHERE outbox_id=?",
                        (utc_iso(self.now()), row["outbox_id"]),
                    )
                    self._audit("event_publication_duplicate", {"task_id": row["primary_task_id"]}, event_id=row["event_id"], cycle_id=row["cycle_id"])
                    duplicate += 1
                elif status == "auto_published" and row["status"] != "published":
                    self.db.execute(
                        "UPDATE event_tracking_publication_outbox SET status='published',updated_at=? WHERE outbox_id=?",
                        (utc_iso(self.now()), row["outbox_id"]),
                    )
                    published += 1
        return {"duplicate": duplicate, "published": published}

    def maintain(self) -> dict[str, int]:
        """End silent/expired cycles and release their temporary account bindings."""
        now = self.now()
        now_text = utc_iso(now)
        rows = self.db.execute(
            "SELECT * FROM event_tracking_cycles WHERE status IN ('discovering','active') ORDER BY started_at"
        ).fetchall()
        ended = 0
        with self.db:
            for row in rows:
                cycle = dict(row)
                started = _datetime(str(cycle["started_at"]), fallback=now)
                last_material = _datetime(cycle.get("last_material_progress_at"), fallback=started)
                hard_cap = _datetime(str(cycle["hard_expires_at"]), fallback=now)
                expires = _datetime(str(cycle["expires_at"]), fallback=now)
                reason: str | None = None
                if now >= hard_cap:
                    reason = "hard_cap_7d"
                elif now - last_material >= EVENT_TRACKING_SILENCE_WINDOW:
                    reason = "silence_24h"
                elif now >= expires:
                    reason = "initial_window_expired"
                if reason is not None:
                    self._end_cycle(cycle, reason, now_text)
                    ended += 1
            self.db.execute(
                """
                UPDATE event_tracking_accounts
                SET status='inactive',next_due_at=NULL,updated_at=?
                WHERE status='active' AND NOT EXISTS(
                  SELECT 1 FROM event_tracking_bindings b WHERE b.handle_lower=event_tracking_accounts.handle_lower AND b.status='active'
                )
                """,
                (now_text,),
            )
        return {"ended": ended}

    def _end_cycle(self, cycle: dict[str, Any], reason: str, now_text: str) -> None:
        event_id = str(cycle["event_id"])
        cycle_id = str(cycle["cycle_id"])
        self.db.execute(
            "UPDATE event_tracking_cycles SET status='ended',ended_at=?,end_reason=?,updated_at=? WHERE cycle_id=?",
            (now_text, reason, now_text, cycle_id),
        )
        self.db.execute(
            "UPDATE event_tracking_bindings SET status='released',released_at=? WHERE cycle_id=? AND status='active'",
            (now_text, cycle_id),
        )
        other_active = self.db.execute(
            "SELECT 1 FROM event_tracking_cycles WHERE event_id=? AND status IN ('discovering','active') LIMIT 1",
            (event_id,),
        ).fetchone()
        if other_active is None:
            self.db.execute(
                "UPDATE event_tracking_events SET status='ended',ended_at=?,end_reason=?,updated_at=? WHERE event_id=?",
                (now_text, reason, now_text, event_id),
            )
        self._audit("event_cycle_ended", {"reason": reason}, event_id=event_id, cycle_id=cycle_id)

    def advance(
        self,
        *,
        topic_ids: Iterable[str] | None = None,
        scanner: Callable[[AccountRow, datetime], tuple[list[ContentItem], Any, dict[str, Any] | None]] | None = None,
        poll_accounts: bool = False,
    ) -> dict[str, Any]:
        """Convenience orchestration for the HotTopic worker and tests."""
        topic = self.observe_topics(topic_ids)
        discovery = self.discover_official_accounts()
        polled = self.poll_due_accounts(scanner) if scanner is not None and poll_accounts else {"polled": 0, "accepted": 0, "errors": 0}
        updates = self.classify_official_updates()
        outbox = self.dispatch_publications()
        lifecycle = self.maintain()
        return {"topics": topic, "discovery": discovery, "poll": polled, "updates": updates, "outbox": outbox, "lifecycle": lifecycle}

    def dismiss_event(self, event_id: str, *, actor: str = "") -> dict[str, Any]:
        """Stop one event without deleting its evidence or published output."""
        normalized_id = str(event_id or "").strip()
        if not normalized_id:
            raise ValueError("event_id 不能为空")
        dismissed_at = utc_iso(self.now())
        with self.db:
            event = self.db.execute("SELECT * FROM event_tracking_events WHERE event_id=?", (normalized_id,)).fetchone()
            if event is None:
                raise ValueError("自动快讯事件不存在")
            previous = self.db.execute(
                "SELECT dismissed_at FROM event_tracking_dismissals WHERE event_id=?", (normalized_id,)
            ).fetchone()
            if previous is not None:
                return {
                    "eventId": normalized_id,
                    "dismissed": True,
                    "alreadyDismissed": True,
                    "dismissedAt": previous["dismissed_at"],
                    "cancelledOutbox": 0,
                    "cancelledTasks": 0,
                }
        dismiss_tasks = getattr(self.dispatcher, "dismiss_event_tasks", None)
        task_result = (
            dismiss_tasks(event_id=normalized_id, dismissed_at=dismissed_at, dismissed_by=actor)
            if callable(dismiss_tasks)
            else {"cancelledTaskIds": [], "publishedTaskIds": [], "terminalTaskIds": []}
        )
        published_task_ids = {int(item) for item in task_result.get("publishedTaskIds", [])}
        terminal_task_ids = {int(item) for item in task_result.get("terminalTaskIds", [])}
        with self.db:
            self.db.execute(
                "INSERT INTO event_tracking_dismissals(event_id,identity_key,dismissed_at,dismissed_by,detail_json) VALUES(?,?,?,?,?)",
                (
                    normalized_id,
                    event["identity_key"],
                    dismissed_at,
                    actor,
                    compact_json({"actor": actor, "task_result": task_result}),
                ),
            )
            cycles = self.db.execute(
                "SELECT * FROM event_tracking_cycles WHERE event_id=? AND status IN ('discovering','active')",
                (normalized_id,),
            ).fetchall()
            for cycle in cycles:
                self._end_cycle(dict(cycle), "manual_dismissed", dismissed_at)
            self.db.execute(
                "UPDATE event_tracking_events SET status='ended',ended_at=?,end_reason='manual_dismissed',updated_at=? WHERE event_id=?",
                (dismissed_at, dismissed_at, normalized_id),
            )
            outbox_rows = self.db.execute(
                "SELECT outbox_id,status,primary_task_id,cancelled_at FROM event_tracking_publication_outbox WHERE event_id=?",
                (normalized_id,),
            ).fetchall()
            cancelled_outbox = 0
            for row in outbox_rows:
                if row["cancelled_at"] is not None:
                    continue
                task_id = int(row["primary_task_id"] or 0)
                should_cancel = str(row["status"]) in {"pending", "submitting", "failed"}
                if str(row["status"]) == "submitted" and task_id not in published_task_ids and task_id not in terminal_task_ids:
                    should_cancel = True
                if should_cancel:
                    self.db.execute(
                        "UPDATE event_tracking_publication_outbox SET cancelled_at=?,cancelled_by=?,last_error=NULL,next_attempt_at=NULL,updated_at=? WHERE outbox_id=?",
                        (dismissed_at, actor, dismissed_at, row["outbox_id"]),
                    )
                    cancelled_outbox += 1
            self.db.execute(
                """
                UPDATE event_tracking_accounts
                SET status='inactive',next_due_at=NULL,updated_at=?
                WHERE status='active' AND NOT EXISTS(
                  SELECT 1 FROM event_tracking_bindings b
                  WHERE b.handle_lower=event_tracking_accounts.handle_lower AND b.status='active'
                )
                """,
                (dismissed_at,),
            )
            self._audit(
                "event_manually_dismissed",
                {
                    "actor": actor,
                    "cancelled_outbox": cancelled_outbox,
                    "cancelled_tasks": task_result.get("cancelledTaskIds", []),
                    "published_tasks": sorted(published_task_ids),
                },
                event_id=normalized_id,
            )
        return {
            "eventId": normalized_id,
            "dismissed": True,
            "alreadyDismissed": False,
            "dismissedAt": dismissed_at,
            "cancelledOutbox": cancelled_outbox,
            "cancelledTasks": len(task_result.get("cancelledTaskIds", [])),
        }

    def dashboard(self, *, limit: int = 100) -> dict[str, Any]:
        now = self.now()
        rows = self.db.execute(
            """
            SELECT e.*,c.status AS cycle_status,c.started_at AS cycle_started_at,c.expires_at,c.hard_expires_at,
                   c.last_material_progress_at AS cycle_last_material_progress_at,c.ended_at AS cycle_ended_at,c.end_reason AS cycle_end_reason,
                   (SELECT COUNT(*) FROM event_tracking_bindings b WHERE b.cycle_id=c.cycle_id AND b.status='active') AS active_account_count,
                   (SELECT COUNT(*) FROM event_tracking_updates u WHERE u.cycle_id=c.cycle_id AND u.classification='material_progress' AND u.status='succeeded') AS material_progress_count
            FROM event_tracking_events e
            LEFT JOIN event_tracking_cycles c ON c.cycle_id=e.current_cycle_id
            WHERE NOT EXISTS (SELECT 1 FROM event_tracking_dismissals d WHERE d.event_id=e.event_id)
            ORDER BY CASE e.status WHEN 'active' THEN 0 WHEN 'discovering' THEN 1 ELSE 2 END,e.updated_at DESC
            LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
        events = [self._event_card(dict(row), now) for row in rows]
        counts = self.db.execute(
            """
            SELECT e.status,COUNT(*) AS count
            FROM event_tracking_events e
            WHERE NOT EXISTS (SELECT 1 FROM event_tracking_dismissals d WHERE d.event_id=e.event_id)
            GROUP BY e.status
            """
        ).fetchall()
        active_accounts = self.db.execute(
            "SELECT COUNT(DISTINCT handle_lower) AS count FROM event_tracking_bindings WHERE status='active'"
        ).fetchone()
        prompts = self.prompt_versions(include_content=False)
        return {
            "generatedAt": utc_iso(now),
            "enabled": self.enabled,
            "summary": {
                "eventsByStatus": {str(row["status"]): int(row["count"]) for row in counts},
                "activeAccounts": int(active_accounts["count"] if active_accounts else 0),
                "maxActiveAccounts": EVENT_TRACKING_MAX_ACTIVE_ACCOUNTS,
                "pendingUpdates": int(self.db.execute(
                    """
                    SELECT COUNT(*) FROM event_tracking_updates u
                    WHERE u.status IN ('pending','processing','failed')
                      AND NOT EXISTS (SELECT 1 FROM event_tracking_dismissals d WHERE d.event_id=u.event_id)
                    """
                ).fetchone()[0]),
                "pendingOutbox": int(self.db.execute(
                    """
                    SELECT COUNT(*) FROM event_tracking_publication_outbox o
                    WHERE o.cancelled_at IS NULL AND o.status IN ('pending','submitting','failed')
                      AND NOT EXISTS (SELECT 1 FROM event_tracking_dismissals d WHERE d.event_id=o.event_id)
                    """
                ).fetchone()[0]),
            },
            "prompts": prompts,
            "events": events,
        }

    def _event_card(self, row: dict[str, Any], now: datetime) -> dict[str, Any]:
        remaining_seconds: int | None = None
        if row.get("cycle_status") in {"active", "discovering"} and row.get("expires_at") and row.get("hard_expires_at"):
            deadline = min(_datetime(str(row["expires_at"]), fallback=now), _datetime(str(row["hard_expires_at"]), fallback=now))
            remaining_seconds = max(0, int((deadline - now).total_seconds()))
        return {
            "id": row["event_id"],
            "title": row["title"],
            "trackingType": row["tracking_type"],
            "readerValue": row["reader_value"],
            "status": row["status"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "lastHotTopicAt": row["last_hot_topic_at"],
            "lastMaterialProgressAt": row["last_material_progress_at"],
            "endReason": row["end_reason"],
            "cycle": None if not row.get("current_cycle_id") else {
                "id": row["current_cycle_id"],
                "status": row["cycle_status"],
                "startedAt": row["cycle_started_at"],
                "expiresAt": row["expires_at"],
                "hardExpiresAt": row["hard_expires_at"],
                "lastMaterialProgressAt": row["cycle_last_material_progress_at"],
                "endedAt": row["cycle_ended_at"],
                "endReason": row["cycle_end_reason"],
                "remainingSeconds": remaining_seconds,
                "activeAccountCount": int(row.get("active_account_count") or 0),
                "materialProgressCount": int(row.get("material_progress_count") or 0),
            },
        }

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        event = self.db.execute("SELECT * FROM event_tracking_events WHERE event_id=?", (event_id,)).fetchone()
        if event is None or self._is_event_dismissed(event_id):
            return None
        event_value = dict(event)
        cycles = [dict(row) for row in self.db.execute("SELECT * FROM event_tracking_cycles WHERE event_id=? ORDER BY started_at DESC", (event_id,)).fetchall()]
        topic_links = [
            {
                "topicId": row["topic_id"],
                "snapshot": json_value(row["snapshot_json"], {}),
                "linkedAt": row["linked_at"],
                "updatedAt": row["updated_at"],
            }
            for row in self.db.execute("SELECT * FROM event_tracking_topic_links WHERE event_id=? ORDER BY updated_at DESC", (event_id,)).fetchall()
        ]
        accounts = [
            {
                "handle": row["screen_name"],
                "displayName": row["display_name"],
                "officialEntity": row["official_entity"],
                "officialRelation": row["official_relation"],
                "officialEvidence": json_value(row["official_evidence_json"], {}),
                "status": row["status"],
                "nextDueAt": row["next_due_at"],
                "lastPolledAt": row["last_polled_at"],
                "lastSuccessAt": row["last_success_at"],
                "lastError": json_value(row["last_error"], row["last_error"]),
                "bindings": [
                    {
                        "cycleId": binding["cycle_id"], "status": binding["status"], "role": binding["role"],
                        "discoveredAt": binding["discovered_at"], "releasedAt": binding["released_at"], "lastContributedAt": binding["last_contributed_at"],
                    }
                    for binding in self.db.execute("SELECT * FROM event_tracking_bindings WHERE event_id=? AND handle_lower=? ORDER BY discovered_at DESC", (event_id, row["handle_lower"])).fetchall()
                ],
            }
            for row in self.db.execute(
                """
                SELECT DISTINCT a.* FROM event_tracking_accounts a
                JOIN event_tracking_bindings b ON b.handle_lower=a.handle_lower
                WHERE b.event_id=? ORDER BY a.screen_name
                """,
                (event_id,),
            ).fetchall()
        ]
        updates = [
            {
                "id": row["update_id"], "cycleId": row["cycle_id"], "tweetId": row["tweet_id"], "handle": row["handle_lower"],
                "status": row["status"], "classification": row["classification"], "newsType": row["news_type"],
                "factSummary": row["fact_summary"], "difference": row["difference_text"],
                "confirmedFacts": json_value(row["confirmed_facts_json"], []), "unconfirmedClaims": json_value(row["unconfirmed_claims_json"], []),
                "confidence": row["confidence"], "reason": row["reason"], "actualModel": row["actual_model"], "usage": json_value(row["usage_json"], {}),
                "durationMs": row["duration_ms"], "error": row["last_error"], "classifiedAt": row["classified_at"],
                "post": json_value(row["payload_json"], {}),
            }
            for row in self.db.execute(
                """
                SELECT u.*,i.payload_json FROM event_tracking_updates u
                LEFT JOIN event_tracking_inbox i ON i.tweet_id=u.tweet_id
                WHERE u.event_id=? ORDER BY u.created_at DESC
                """,
                (event_id,),
            ).fetchall()
        ]
        outbox = []
        task_detail = getattr(self.dispatcher, "task_detail", None)
        for row in self.db.execute("SELECT * FROM event_tracking_publication_outbox WHERE event_id=? ORDER BY created_at DESC", (event_id,)).fetchall():
            task = task_detail(int(row["primary_task_id"])) if callable(task_detail) and row["primary_task_id"] else None
            post = self.db.execute(
                "SELECT payload_json FROM event_tracking_inbox WHERE tweet_id=?",
                (row["tweet_id"],),
            ).fetchone()
            post_value = json_value(post["payload_json"], {}) if post is not None else {}
            if not isinstance(post_value, dict):
                post_value = {}
            status = "cancelled" if row["cancelled_at"] else str(row["status"])
            task_status = str((task or {}).get("status") or "")
            if row["cancelled_at"]:
                status = "cancelled"
            elif task_status == "auto_published":
                status = "published"
            elif task_status == "duplicate":
                status = "duplicate"
            elif task_status == "event_tracking_cancelled":
                status = "cancelled"
            elif task_status.endswith("_failed"):
                status = "failed"
            generated_title = (task or {}).get("finalTitle") or (task or {}).get("draftTitle")
            generated_content = (task or {}).get("finalContent") or (task or {}).get("draftContent")
            outbox.append(
                {
                    "id": row["outbox_id"], "updateId": row["update_id"], "tweetId": row["tweet_id"], "status": status,
                    "taskId": row["primary_task_id"], "attempts": row["attempts"],
                    "error": row["last_error"] or (task or {}).get("error"), "submittedAt": row["submitted_at"],
                    "taskStatus": task_status or None, "sourceUrl": (task or {}).get("sourceUrl") or post_value.get("url"),
                    "title": generated_title, "content": generated_content,
                    "contentStage": "final" if (task or {}).get("finalContent") else "draft" if (task or {}).get("draftContent") else None,
                    "publisherDecision": (task or {}).get("publisherDecision"),
                    "publisherReasonCode": (task or {}).get("publisherReasonCode"),
                    "publishedAt": (task or {}).get("publishedAt"), "updatedAt": (task or {}).get("updatedAt"),
                }
            )
        discoveries = [
            {
                "id": row["discovery_id"], "cycleId": row["cycle_id"], "status": row["status"], "attempts": row["attempts"],
                "actualModel": row["actual_model"], "usage": json_value(row["usage_json"], {}), "durationMs": row["duration_ms"],
                "toolCalls": json_value(row["search_tool_calls_json"], []), "citations": json_value(row["citations_json"], []),
                "result": json_value(row["result_json"], {}), "error": row["error"], "completedAt": row["completed_at"],
            }
            for row in self.db.execute("SELECT * FROM event_tracking_account_discoveries WHERE event_id=? ORDER BY completed_at DESC", (event_id,)).fetchall()
        ]
        return {
            "event": {
                "id": event_value["event_id"], "title": event_value["title"], "identityKey": event_value["identity_key"],
                "trackingType": event_value["tracking_type"], "readerValue": event_value["reader_value"], "status": event_value["status"],
                "confirmedFacts": json_value(event_value["confirmed_facts_json"], []), "unconfirmedClaims": json_value(event_value["unconfirmed_claims_json"], []),
                "rationale": event_value["rationale"], "officialResponseHypothesis": json_value(event_value["official_response_hypothesis_json"], {}),
                "createdAt": event_value["created_at"], "updatedAt": event_value["updated_at"], "endReason": event_value["end_reason"],
            },
            "cycles": [self._cycle_detail(row) for row in cycles],
            "topics": topic_links,
            "accounts": accounts,
            "updates": updates,
            "outbox": outbox,
            "discoveries": discoveries,
            "promptVersions": self.prompt_versions(include_content=False),
        }

    @staticmethod
    def _cycle_detail(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["cycle_id"], "status": row["status"], "startedAt": row["started_at"], "initialExpiresAt": row["initial_expires_at"],
            "expiresAt": row["expires_at"], "hardExpiresAt": row["hard_expires_at"], "lastMaterialProgressAt": row["last_material_progress_at"],
            "endedAt": row["ended_at"], "endReason": row["end_reason"],
        }

    def prompt_versions(self, *, include_content: bool = True) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT p.*,COUNT(a.topic_id) AS topic_call_count
            FROM event_tracking_prompt_versions p
            LEFT JOIN event_tracking_topic_assessments a ON a.prompt_version_id=p.id
            GROUP BY p.id
            UNION ALL
            SELECT p.*,COUNT(d.discovery_id) AS topic_call_count
            FROM event_tracking_prompt_versions p
            LEFT JOIN event_tracking_account_discoveries d ON d.prompt_version_id=p.id
            WHERE p.prompt_key='official_discovery'
            GROUP BY p.id
            UNION ALL
            SELECT p.*,COUNT(u.update_id) AS topic_call_count
            FROM event_tracking_prompt_versions p
            LEFT JOIN event_tracking_updates u ON u.prompt_version_id=p.id
            WHERE p.prompt_key='update_materiality'
            GROUP BY p.id
            """
        ).fetchall()
        # The UNION keeps the dashboard portable; fold duplicate rows caused by
        # the topic-assessment branch into one immutable prompt projection.
        projections: dict[int, dict[str, Any]] = {}
        for row in rows:
            target = projections.setdefault(
                int(row["id"]),
                {
                    "id": int(row["id"]), "key": row["prompt_key"], "version": int(row["version_number"]),
                    "createdAt": row["created_at"], "callCount": 0,
                },
            )
            target["callCount"] += int(row["topic_call_count"] or 0)
            if include_content:
                target["content"] = row["content"]
        return sorted(projections.values(), key=lambda item: (item["key"], item["version"]))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _canonical_url(value: str) -> str:
    return value.strip().rstrip("/").lower()


def _is_matching_x_profile(value: str, handle: str) -> bool:
    canonical = _canonical_url(value)
    allowed_prefixes = ("https://x.com/", "https://twitter.com/", "https://www.twitter.com/")
    return canonical.startswith(allowed_prefixes) and canonical.rsplit("/", 1)[-1].lower() == handle.lower()
