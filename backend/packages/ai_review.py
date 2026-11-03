from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
DAILY_TOKEN_LIMIT = 50_000_000
REVIEW_LOOKBACK_DAYS = 3
MAX_ANALYSIS_BATCH = 50
OUTPUT_TOKEN_RESERVE = 1_200
MAX_ANALYSIS_ATTEMPTS = 3
EVENT_SCAN_VERSION = 4
EVENT_ANALYSIS_VERSION = 4
RECOMMENDATIONS = {"priority", "consider", "drop"}
RECOMMENDATION_LABELS = {"priority": "优先考虑", "consider": "可考虑", "drop": "不建议跟进"}
STATUS_VALUES = {"pending", "accepted", "rejected", "deferred"}
URL_RE = re.compile(r"https?://[^\s<>\]\[(){}\"']+", re.IGNORECASE)
X_URL_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:/|\b)", re.IGNORECASE)
TRACKED_SOURCE_DOMAINS = {
    "odaily.news", "theblockbeats.info", "blockbeats.info", "panewslab.com", "jinse.cn",
    "coindesk.com", "cointelegraph.com", "decrypt.co", "theblock.co",
}
PUBLISHER_DOMAINS = {
    "odaily": ("odaily.news",),
    "blockbeats": ("theblockbeats.info", "blockbeats.info"),
    "panews": ("panewslab.com",),
    "jinse": ("jinse.cn",),
    "coindesk": ("coindesk.com",),
    "cointelegraph": ("cointelegraph.com",),
    "theblock": ("theblock.co",),
    "decrypt": ("decrypt.co",),
}
PUBLISHER_LABELS = {
    "blockbeats": "BlockBeats", "panews": "PANews", "jinse": "金色财经",
    "coindesk": "CoinDesk", "cointelegraph": "Cointelegraph", "theblock": "The Block", "decrypt": "Decrypt",
}
ORIGINAL_LINK_KEYS = {
    "link", "href", "sourceurl", "originurl", "originalurl", "referencelink", "referenceurl",
    "sourcelink", "originallink", "externallink", "externalurl", "jumpurl", "detailurl", "canonicalurl",
}
BODY_LINK_KEYS = {"content", "body", "description", "desc", "summary", "text", "excerpt"}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ai_review_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_key TEXT NOT NULL UNIQUE,
  item_type TEXT NOT NULL DEFAULT 'event' CHECK(item_type='event'),
  category TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected','deferred')),
  analysis_status TEXT NOT NULL DEFAULT 'queued' CHECK(analysis_status IN ('queued','completed','failed')),
  title TEXT NOT NULL,
  recommendation TEXT NOT NULL DEFAULT 'consider',
  reason TEXT NOT NULL DEFAULT '',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  clues_json TEXT NOT NULL DEFAULT '{}',
  analysis_model TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  analysis_attempts INTEGER NOT NULL DEFAULT 0,
  analysis_error TEXT,
  human_decision TEXT,
  feedback_note TEXT,
  decision_by TEXT,
  decided_at TEXT,
  source_updated_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_review_queue ON ai_review_items(status,item_type,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_review_analysis ON ai_review_items(analysis_status,analysis_attempts,created_at);
CREATE TABLE IF NOT EXISTS ai_review_daily_usage (
  usage_date TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'event' CHECK(kind='event'),
  used_tokens INTEGER NOT NULL DEFAULT 0,
  reserved_tokens INTEGER NOT NULL DEFAULT 0,
  calls INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(usage_date,kind)
);
CREATE TABLE IF NOT EXISTS ai_review_run_state (
  state_key TEXT PRIMARY KEY,
  state_value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value)) if value not in (None, "") else default
    except (TypeError, json.JSONDecodeError):
        return default


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _local_date(now: datetime) -> str:
    return now.astimezone(BEIJING_TZ).date().isoformat()


def estimate_tokens(text: str) -> int:
    try:
        import tiktoken

        try:
            encoding = tiktoken.encoding_for_model("gpt-6-luna")
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")
        return max(1, len(encoding.encode(text)))
    except Exception:
        # Character count is a conservative fallback for mixed Chinese/English prompts.
        return max(1, len(text))


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


class AIReviewRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA_SQL)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _item(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["evidence"] = _decode(item.pop("evidence_json"), [])
        item["clues"] = _decode(item.pop("clues_json"), {})
        item.pop("account_lower", None)
        item.pop("adopted_targets_json", None)
        for old, new in (
            ("item_key", "itemKey"), ("item_type", "type"), ("analysis_status", "analysisStatus"),
            ("analysis_model", "analysisModel"), ("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
            ("analysis_attempts", "analysisAttempts"), ("analysis_error", "analysisError"),
            ("human_decision", "humanDecision"), ("feedback_note", "feedbackNote"), ("decision_by", "decisionBy"),
            ("decided_at", "decidedAt"), ("source_updated_at", "sourceUpdatedAt"),
            ("created_at", "createdAt"), ("updated_at", "updatedAt"),
        ):
            item[new] = item.pop(old)
        item["recommendationLabel"] = RECOMMENDATION_LABELS.get(item["recommendation"], "可考虑")
        return item

    def insert_candidate(
        self, *, key: str, category: str, title: str, evidence: list[dict[str, Any]],
        clues: dict[str, Any] | None = None, source_updated_at: str | None = None,
        refresh_existing: bool = False,
    ) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO ai_review_items(item_key,item_type,category,title,evidence_json,clues_json,"
                "source_updated_at,created_at,updated_at) VALUES(?,'event',?,?,?,?,?,?,?)",
                (key, category, title[:300], _json(evidence), _json(clues or {}), source_updated_at, now, now),
            )
            if cursor.rowcount == 0 and refresh_existing:
                self.db.execute(
                    "UPDATE ai_review_items SET title=?,evidence_json=?,clues_json=?,source_updated_at=?,updated_at=? "
                    "WHERE item_key=? AND item_type='event'",
                    (title[:300], _json(evidence), _json(clues or {}), source_updated_at, now, key),
                )
        return cursor.rowcount > 0

    def pending_analysis(self, limit: int = MAX_ANALYSIS_BATCH) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM ai_review_items WHERE item_type='event' AND analysis_status IN ('queued','failed') "
            "AND analysis_attempts<? ORDER BY datetime(COALESCE(source_updated_at,created_at)) DESC,id DESC LIMIT ?",
            (MAX_ANALYSIS_ATTEMPTS, limit),
        ).fetchall()
        return [self._item(row) for row in rows]

    def delete_stale_event_candidates(self, since: datetime, keep_keys: set[str]) -> int:
        rows = self.db.execute(
            "SELECT item_key,category,status,source_updated_at,created_at FROM ai_review_items WHERE item_type='event'"
        ).fetchall()
        allowed_categories = {"competitor_first_ai", "multiple_competitors_no_odaily"}
        stale_keys = []
        for row in rows:
            key = str(row["item_key"])
            updated = _parse_datetime(row["source_updated_at"] or row["created_at"])
            legacy_key = not key.startswith("event-source:")
            stale_category = str(row["category"]) not in allowed_categories
            stale_recent = bool(updated and updated >= since and key not in keep_keys)
            expired_pending = bool(updated and updated < since and row["status"] == "pending")
            if legacy_key or stale_category or stale_recent or expired_pending:
                stale_keys.append(key)
        if not stale_keys:
            return 0
        with self.db:
            self.db.executemany("DELETE FROM ai_review_items WHERE item_key=?", ((key,) for key in stale_keys))
        return len(stale_keys)

    def complete_analysis(
        self, item_id: int, *, recommendation: str, reason: str, model: str,
        input_tokens: int, output_tokens: int,
    ) -> None:
        with self.db:
            self.db.execute(
                "UPDATE ai_review_items SET analysis_status='completed',recommendation=?,reason=?,analysis_model=?,"
                "input_tokens=?,output_tokens=?,analysis_error=NULL,analysis_attempts=analysis_attempts+1,updated_at=? WHERE id=?",
                (recommendation, reason[:1500], model, input_tokens, output_tokens, datetime.now(UTC).isoformat(), item_id),
            )

    def fail_analysis(self, item_id: int, error: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE ai_review_items SET analysis_status='failed',analysis_error=?,analysis_attempts=analysis_attempts+1,updated_at=? WHERE id=?",
                (error[:1000], datetime.now(UTC).isoformat(), item_id),
            )

    def update_usage(self, usage_date: str, *, reserve: int = 0, settle: tuple[int, int] | None = None) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO ai_review_daily_usage(usage_date,kind,updated_at) VALUES(?,'event',?)",
                (usage_date, now),
            )
            row = self.db.execute(
                "SELECT used_tokens,reserved_tokens FROM ai_review_daily_usage WHERE usage_date=? AND kind='event'",
                (usage_date,),
            ).fetchone()
            if reserve:
                if int(row["used_tokens"]) + int(row["reserved_tokens"]) + reserve > DAILY_TOKEN_LIMIT:
                    return False
                self.db.execute(
                    "UPDATE ai_review_daily_usage SET reserved_tokens=reserved_tokens+?,updated_at=? WHERE usage_date=? AND kind='event'",
                    (reserve, now, usage_date),
                )
            elif settle is not None:
                reserved, actual = settle
                self.db.execute(
                    "UPDATE ai_review_daily_usage SET reserved_tokens=max(0,reserved_tokens-?),used_tokens=used_tokens+?,"
                    "calls=calls+1,updated_at=? WHERE usage_date=? AND kind='event'",
                    (reserved, max(0, actual), now, usage_date),
                )
        return True

    def release_usage(self, usage_date: str, reserved: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE ai_review_daily_usage SET reserved_tokens=max(0,reserved_tokens-?),updated_at=? WHERE usage_date=? AND kind='event'",
                (reserved, datetime.now(UTC).isoformat(), usage_date),
            )

    def usage(self, usage_date: str) -> dict[str, dict[str, int]]:
        rows = self.db.execute(
            "SELECT kind,used_tokens,reserved_tokens,calls FROM ai_review_daily_usage WHERE usage_date=? AND kind='event'",
            (usage_date,),
        ).fetchall()
        result = {"event": {"usedTokens": 0, "reservedTokens": 0, "calls": 0}}
        for row in rows:
            result[row["kind"]] = {
                "usedTokens": int(row["used_tokens"]), "reservedTokens": int(row["reserved_tokens"]),
                "calls": int(row["calls"]),
            }
        return result

    def get_state(self, key: str) -> str | None:
        row = self.db.execute("SELECT state_value FROM ai_review_run_state WHERE state_key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO ai_review_run_state(state_key,state_value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at=excluded.updated_at",
                (key, value, datetime.now(UTC).isoformat()),
            )

    def list_items(
        self, *, status: str = "all", category: str = "all", offset: int = 0, limit: int = 30,
    ) -> dict[str, Any]:
        clauses, params = ["item_type='event'", "analysis_status='completed'", "recommendation='priority'"], []
        if status in STATUS_VALUES:
            clauses.append("status=?")
            params.append(status)
        if category not in {"", "all"}:
            clauses.append("category=?")
            params.append(category)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = int(self.db.execute(f"SELECT count(*) FROM ai_review_items{where}", params).fetchone()[0])
        safe_limit, safe_offset = min(100, max(1, int(limit))), max(0, int(offset))
        rows = self.db.execute(
            f"SELECT * FROM ai_review_items{where} ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END,"
            "datetime(COALESCE(source_updated_at,created_at)) DESC,id DESC LIMIT ? OFFSET ?",
            [*params, safe_limit, safe_offset],
        ).fetchall()
        return {
            "items": [self._item(row) for row in rows], "total": total, "offset": safe_offset, "limit": safe_limit,
            "usage": self.usage(_local_date(datetime.now(UTC))), "weekly": self.weekly_summary(),
        }

    def weekly_summary(self) -> dict[str, int | float]:
        cutoff = (datetime.now(UTC) - timedelta(days=REVIEW_LOOKBACK_DAYS)).isoformat()
        row = self.db.execute(
            "SELECT COUNT(*) AS candidates,"
            "SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END) AS accepted,"
            "SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected,"
            "SUM(CASE WHEN status='deferred' THEN 1 ELSE 0 END) AS deferred,"
            "COALESCE(SUM(input_tokens),0) AS input_tokens,COALESCE(SUM(output_tokens),0) AS output_tokens "
            "FROM ai_review_items WHERE item_type='event' AND datetime(created_at)>=datetime(?) AND analysis_status='completed' "
            "AND recommendation='priority'",
            (cutoff,),
        ).fetchone()
        candidates = int(row["candidates"] or 0)
        accepted, rejected = int(row["accepted"] or 0), int(row["rejected"] or 0)
        return {
            "candidates": candidates, "accepted": accepted, "rejected": rejected,
            "deferred": int(row["deferred"] or 0),
            "acceptanceRate": round(accepted / candidates, 4) if candidates else 0.0,
            "rejectionRate": round(rejected / candidates, 4) if candidates else 0.0,
            "inputTokens": int(row["input_tokens"] or 0), "outputTokens": int(row["output_tokens"] or 0),
        }

    def get_item(self, item_key: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM ai_review_items WHERE item_key=? AND item_type='event'", (item_key,),
        ).fetchone()
        return self._item(row) if row else None

    def decide(self, item_key: str, *, decision: str, actor: str, note: str = "") -> dict[str, Any]:
        if decision not in {"accepted", "rejected", "deferred"}:
            raise ValueError("不支持的人工决定")
        now = datetime.now(UTC).isoformat()
        with self.db:
            row = self.db.execute(
                "SELECT * FROM ai_review_items WHERE item_key=? AND item_type='event'", (item_key,),
            ).fetchone()
            if row is None:
                raise ValueError("审阅项目不存在")
            if row["status"] == "accepted" and decision == "accepted":
                return self._item(row)
            self.db.execute(
                "UPDATE ai_review_items SET status=?,human_decision=?,feedback_note=?,decision_by=?,decided_at=?,updated_at=? WHERE item_key=?",
                (decision, decision, note[:500], actor[:200], now, now, item_key),
            )
        item = self.get_item(item_key)
        assert item is not None
        return item

    def has_pending_analysis(self) -> bool:
        return self.db.execute(
            "SELECT 1 FROM ai_review_items WHERE item_type='event' AND analysis_status IN ('queued','failed') "
            "AND analysis_attempts<? LIMIT 1", (MAX_ANALYSIS_ATTEMPTS,),
        ).fetchone() is not None

    def requeue_event_analyses(self) -> int:
        with self.db:
            cursor = self.db.execute(
                "UPDATE ai_review_items SET analysis_status='queued',recommendation='consider',reason='',"
                "analysis_model=NULL,input_tokens=0,output_tokens=0,analysis_attempts=0,analysis_error=NULL,updated_at=? "
                "WHERE item_type='event' AND analysis_status='completed'",
                (datetime.now(UTC).isoformat(),),
            )
        return cursor.rowcount

    def history_examples(self, category: str, limit: int = 2) -> list[dict[str, str]]:
        rows = self.db.execute(
            "SELECT category,human_decision,feedback_note FROM ai_review_items WHERE item_type='event' "
            "AND human_decision IS NOT NULL AND category=? ORDER BY decided_at DESC LIMIT ?", (category, limit),
        ).fetchall()
        return [
            {"category": str(row["category"]), "decision": str(row["human_decision"]), "note": str(row["feedback_note"] or "")[:120]}
            for row in rows
        ]


class AIReviewAnalyzer:
    def __init__(self, *, post_json: Callable[[str, str, bytes, float], dict[str, Any]] | None = None) -> None:
        self.base_url = (
            os.getenv("X_AGENT_OPENAI_BASE_URL") or os.getenv("ODAILY_LLM_BASE_URL")
            or os.getenv("OPENAI_BASE_URL") or ""
        ).rstrip("/")
        explicit_key = os.getenv("X_AGENT_OPENAI_API_KEY") or ""
        local_route = self.base_url.startswith(("http://127.0.0.1:", "http://localhost:", "https://127.0.0.1:", "https://localhost:"))
        self.api_key = explicit_key or (
            (os.getenv("LITELLM_MASTER_KEY") or os.getenv("ODAILY_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "")
            if local_route else (os.getenv("ODAILY_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "")
        )
        self.model = "gpt-6-luna"
        self.timeout = 60.0
        self.post_json = post_json or self._post_json

    @staticmethod
    def _post_json(url: str, key: str, body: bytes, timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(
            url, data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("AI 审阅模型返回了无效响应")
        return payload

    def analyze(self, item: dict[str, Any], examples: list[dict[str, str]]) -> dict[str, Any]:
        if not self.base_url or not self.api_key:
            raise RuntimeError("AI 审阅需要配置 X Agent 兼容模型路由和 API key")
        system = (
            "你是 Crypto 垂直媒体的信源编辑，只根据给定竞品报道和它们明确链接的原始信源判断哪些值得持续监控，不搜索外部资料。每个候选只对应一个原始账号或域名，竞品媒体只是发现入口，"
            "不是推荐追踪对象；证据里的 originSources 是从竞品原文链接、结构化原文字段或正文 URL 提取的候选。"
            "优先推荐持续发布 Crypto 一手数据、研究、公告或对读者交易决策有价值内容的原始账号/网站。若链接只指向一次性文章、一次性项目页面、临时活动或普通转载，"
            "而没有证据表明背后的账号/网站会持续产出有价值信息，应不建议跟进。仅有竞品自己的文章链接不构成原始信源。"
            "同一事件的多个竞品可能链接到同一个源；按原始账号/域名判断，避免把 PANews、金色财经、BlockBeats 等竞品误当追踪对象。"
            "只按提供的原文和链接判断，不能把被提及对象或作者姓名猜成信源；错误归因可能损害个人或机构名誉。"
            "reason 简短说明这个原始账号/域名是否有持续追踪价值；证据列表提供对应原始信源链接和发现它的竞品。"
            "只返回 JSON：{\"recommendation\":\"priority|consider|drop\",\"reason\":\"简短中文理由\"}。"
        )
        evidence = [row for row in item["evidence"] if row.get("originSources")][:8]
        prompt = _json({
            "item": {"title": item["title"], "category": item["category"], "evidence": evidence},
            "similar_human_decisions": examples,
        })
        body = json.dumps(
            {"model": self.model, "temperature": 0.1, "max_tokens": OUTPUT_TOKEN_RESERVE, "reasoning_effort": "none",
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]},
            ensure_ascii=False,
        ).encode()
        response = self.post_json(f"{self.base_url}/chat/completions", self.api_key, body, self.timeout)
        content = str((((response.get("choices") or [{}])[0]).get("message") or {}).get("content") or "")
        match = re.search(r"\{[\s\S]*\}", content)
        if not match:
            raise ValueError("AI 审阅模型未返回 JSON")
        parsed = json.loads(match.group(0))
        recommendation = str(parsed.get("recommendation") or "consider")
        if recommendation not in RECOMMENDATIONS:
            recommendation = "consider"
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        input_tokens = _usage_int(usage, "input_tokens", "prompt_tokens") or estimate_tokens(system + prompt)
        output_tokens = _usage_int(usage, "output_tokens", "completion_tokens") or estimate_tokens(content)
        return {
            "recommendation": recommendation, "reason": str(parsed.get("reason") or "")[:1500],
            "model": str(response.get("model") or self.model), "input_tokens": input_tokens, "output_tokens": output_tokens,
        }


def _usage_int(usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        try:
            value = int(usage.get(key) or 0)
            if value > 0:
                return value
        except (TypeError, ValueError):
            continue
    return 0


def _embedded_urls(value: Any, *, urls: set[str], scan_body: bool = False) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = re.sub(r"[^a-z]", "", str(key).casefold())
            if normalized_key in ORIGINAL_LINK_KEYS:
                _embedded_urls(child, urls=urls, scan_body=True)
            elif normalized_key in BODY_LINK_KEYS:
                _embedded_urls(child, urls=urls, scan_body=True)
            elif isinstance(child, (dict, list, tuple)):
                _embedded_urls(child, urls=urls)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _embedded_urls(child, urls=urls, scan_body=scan_body)
    elif isinstance(value, str):
        if scan_body:
            for match in URL_RE.findall(html.unescape(value)):
                urls.add(match.rstrip(".,;:!?"))


def _publisher_article_url(source: str, source_item_id: str, source_url: str) -> str:
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    publisher_domains = PUBLISHER_DOMAINS.get(source.casefold(), ())
    if host and any(host == domain or host.endswith(f".{domain}") for domain in publisher_domains):
        return source_url
    if source.casefold() == "panews" and source_item_id:
        return f"https://www.panewslab.com/zh/articledetails/{source_item_id}.html"
    if source.casefold() == "jinse" and source_item_id.isdigit():
        return f"https://www.jinse.cn/lives/{source_item_id}.html"
    if source.casefold() == "blockbeats" and source_item_id.isdigit():
        return f"https://www.theblockbeats.info/flash/{source_item_id}"
    return ""


def _extract_original_sources(
    source: sqlite3.Row, *, known_handles: set[str], known_domains: set[str],
) -> list[dict[str, Any]]:
    urls: set[str] = set()
    source_url = str(source["source_url"] or "").strip()
    if source_url:
        urls.add(source_url.rstrip(".,;:!?"))
    for field in ("raw_payload", "metadata"):
        _embedded_urls(_decode(source[field], {}), urls=urls)
    _embedded_urls(str(source["content"] or ""), urls=urls, scan_body=True)

    candidates: dict[str, dict[str, Any]] = {}
    for url in sorted(urls):
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        if parsed.scheme not in {"http", "https"} or not host:
            continue
        if host in {"x.com", "twitter.com", "t.co"}:
            handle_match = X_URL_RE.search(url)
            if not handle_match:
                continue
            account = handle_match.group(1).casefold()
            source_domain = None
            tracked = account in known_handles
        else:
            if parsed.path.casefold().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")):
                continue
            if any(
                host == domain or host.endswith(f".{domain}")
                for domains in PUBLISHER_DOMAINS.values() for domain in domains
            ):
                continue
            account = None
            source_domain = host
            tracked = AIReviewService._is_tracked_domain(host, known_domains)
        identity = f"x:{account}" if account else f"domain:{source_domain}"
        candidate = candidates.setdefault(identity, {
            "identity": identity, "url": url, "urls": [], "sourceAccount": account,
            "sourceDomain": source_domain, "tracked": tracked, "discoveryOutlet": str(source["source"]),
        })
        normalized_url = url.casefold().rstrip("/")
        if all(existing.casefold().rstrip("/") != normalized_url for existing in candidate["urls"]):
            candidate["urls"].append(url)
        candidate["url"] = min(candidate["urls"], key=str.casefold)
    for candidate in candidates.values():
        candidate["urls"].sort(key=str.casefold)
    return list(candidates.values())


def _event_source_key(event_id: str, category: str, identity: str) -> str:
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
    return f"event-source:{event_id}:{category}:{digest}"


class AIReviewService:
    def __init__(
        self, *, database_path: Path, primary_database_path: Path, hottopic_database_path: Path,
        analyzer: AIReviewAnalyzer | None = None,
    ) -> None:
        self.repository = AIReviewRepository(database_path)
        self.primary_database_path = primary_database_path
        self.hottopic_database_path = hottopic_database_path
        self.analyzer = analyzer or AIReviewAnalyzer()

    def close(self) -> None:
        self.repository.close()

    def list_items(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.repository.list_items(
            status=str(payload.get("status") or "all"),
            category=str(payload.get("category") or "all"),
            offset=int(payload.get("offset") or 0), limit=int(payload.get("limit") or 30),
        )

    def get_item(self, item_key: str) -> dict[str, Any] | None:
        return self.repository.get_item(item_key)

    def decide(self, item_key: str, *, decision: str, actor: str, note: str = "") -> dict[str, Any]:
        item = self.repository.get_item(item_key)
        if item is None:
            raise ValueError("审阅项目不存在")
        return self.repository.decide(item_key, decision=decision, actor=actor, note=note)

    def run_if_due(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        local = current.astimezone(BEIJING_TZ)
        if local.hour < 6:
            return {"status": "not_due"}
        today = local.date().isoformat()
        event_scan_key = f"last_event_scan_date_v{EVENT_SCAN_VERSION}"
        event_analysis_key = f"event_analysis_version"
        event_scan_due = self.repository.get_state(event_scan_key) != today
        event_analysis_due = self.repository.get_state(event_analysis_key) != str(EVENT_ANALYSIS_VERSION)
        analysis_pending = self.repository.has_pending_analysis()
        if self.repository.get_state("last_completed_date") == today and not (
            event_scan_due or event_analysis_due or analysis_pending
        ):
            return {"status": "already_ran", "date": today}
        since = current - timedelta(days=REVIEW_LOOKBACK_DAYS)
        queued = 0
        if event_analysis_due:
            queued += self.repository.requeue_event_analyses()
            self.repository.set_state(event_analysis_key, str(EVENT_ANALYSIS_VERSION))
        if event_scan_due:
            queued += self._queue_events(since)
            self.repository.set_state(event_scan_key, today)
        if self.repository.get_state("budget_blocked_event") == today:
            event_result = {"analyzed": 0, "deferred": 1}
        else:
            event_result = self._analyze_pending(today)
            if event_result["deferred"]:
                self.repository.set_state("budget_blocked_event", today)
        event_pending = self.repository.has_pending_analysis()
        complete = not event_pending or event_result["deferred"] > 0
        if complete:
            self.repository.set_state("last_completed_date", today)
        return {
            "status": "completed" if complete else "in_progress", "date": today, "queued": queued,
            "analyzed": event_result["analyzed"], "deferred": event_result["deferred"],
            "usage": self.repository.usage(today),
        }

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def _queue_events(self, since: datetime) -> int:
        try:
            conn = _read_only(self.primary_database_path)
        except sqlite3.Error:
            return 0
        inserted = 0
        try:
            if not self._table_exists(conn, "newsflash_events") or not self._table_exists(conn, "newsflash_event_sources"):
                return 0
            rows = conn.execute(
                "SELECT e.event_id,e.representative_title,e.event_time,e.first_published_at,e.first_sources,e.first_source,"
                "e.competitor_source_count,e.has_odaily,i.source,i.source_item_id,i.source_url,i.title,i.content,"
                "i.published_at,i.raw_payload,i.metadata "
                "FROM newsflash_events e JOIN newsflash_event_sources s ON s.event_id=e.event_id "
                "JOIN newsflash_items i ON i.id=s.item_id WHERE e.status='active' AND datetime(e.event_time)>=datetime(?) "
                "ORDER BY datetime(e.event_time) DESC LIMIT 10000", (since.isoformat(),),
            ).fetchall()
            events: dict[str, list[sqlite3.Row]] = defaultdict(list)
            for row in rows:
                events[str(row["event_id"])].append(row)
            known_handles, known_domains = self._known_source_clues()
            retained_keys: set[str] = set()
            for event_id, sources in events.items():
                if any(self._is_hyperbot_source(row) for row in sources):
                    continue
                outlets = {str(row["source"]).casefold() for row in sources}
                competitors = [row for row in sources if str(row["source"]).casefold() != "odaily"]
                odaily = [row for row in sources if str(row["source"]).casefold() == "odaily"]
                category = ""
                publish_status = None
                if competitors and odaily:
                    comp_times = [_parse_datetime(row["published_at"]) for row in competitors]
                    odaily_times = [_parse_datetime(row["published_at"]) for row in odaily]
                    first_comp = min((value for value in comp_times if value), default=None)
                    first_odaily = min((value for value in odaily_times if value), default=None)
                    if first_comp and first_odaily and first_comp < first_odaily:
                        publish_status = next(
                            (status for row in odaily
                             if (status := self._ai_competitor_publish(conn, str(row["source_item_id"])))),
                            None,
                        )
                        if publish_status:
                            category = "competitor_first_ai"
                elif len(outlets - {"odaily"}) >= 2 and not odaily:
                    category = "multiple_competitors_no_odaily"
                if not category:
                    continue
                title = str(sources[0]["representative_title"] or sources[0]["title"] or "未命名事件")
                if category == "competitor_first_ai":
                    title = f"{title} · 竞品先发 / Odaily {'直发' if publish_status == 'auto_published' else '挂后台'}"
                else:
                    title = f"{title} · 多家竞品发布，Odaily 未发"
                ordered_competitors = sorted(
                    competitors,
                    key=lambda row: _parse_datetime(row["published_at"]) or datetime.max.replace(tzinfo=UTC),
                )
                candidates: dict[str, dict[str, Any]] = {}
                for source in ordered_competitors[:20]:
                    outlet = str(source["source"]).casefold()
                    article_url = _publisher_article_url(
                        outlet, str(source["source_item_id"] or ""), str(source["source_url"] or ""),
                    )
                    origins = _extract_original_sources(
                        source, known_handles=known_handles, known_domains=known_domains,
                    )
                    for origin in origins:
                        identity = str(origin["identity"])
                        candidate = candidates.setdefault(identity, {"origin": dict(origin), "urls": set(), "evidence": []})
                        candidate["urls"].update(origin.get("urls") or [origin["url"]])
                        article_evidence = {
                            "source": PUBLISHER_LABELS.get(outlet, str(source["source"])),
                            "sourceKind": "competitor", "url": article_url or None,
                            "title": str(source["title"] or ""), "publishedAt": source["published_at"],
                            "text": str(source["content"] or "")[:1800],
                        }
                        evidence_key = (outlet, article_url, article_evidence["title"])
                        if not any(
                            (row["_outlet"], row["url"], row["title"]) == evidence_key
                            for row in candidate["evidence"]
                        ):
                            candidate["evidence"].append({**article_evidence, "_outlet": outlet})
                if not candidates:
                    continue
                for identity, candidate in candidates.items():
                    origin = candidate["origin"]
                    origin["urls"] = sorted(candidate["urls"], key=str.casefold)
                    origin["url"] = min(origin["urls"], key=str.casefold)
                    evidence = []
                    for item in candidate["evidence"]:
                        item = {key: value for key, value in item.items() if key != "_outlet"}
                        evidence.append({**item, "originSources": [origin]})
                    account = str(origin.get("sourceAccount") or "")
                    domain = str(origin.get("sourceDomain") or "")
                    source_label = f"@{account}" if account else domain
                    key = _event_source_key(event_id, category, identity)
                    retained_keys.add(key)
                    clues = {
                        "xAccounts": [account] if account else [],
                        "domains": [domain] if domain else [],
                    }
                    latest_published = max(
                        (str(item.get("publishedAt") or "") for item in evidence),
                        key=lambda value: _parse_datetime(value) or datetime.min.replace(tzinfo=UTC),
                    )
                    inserted += int(self.repository.insert_candidate(
                        key=key, category=category,
                        title=f"{source_label} · {title}", evidence=evidence[:20], clues=clues,
                        source_updated_at=latest_published, refresh_existing=True,
                    ))
            self.repository.delete_stale_event_candidates(since, retained_keys)
        finally:
            conn.close()
        return inserted

    @staticmethod
    def _is_hyperbot_source(row: sqlite3.Row) -> bool:
        source = str(row["source"] or "").casefold()
        host = (urlparse(str(row["source_url"] or "")).hostname or "").casefold()
        return source == "hyperbot" or host == "hyperbot.network" or host.endswith(".hyperbot.network")

    @staticmethod
    def _is_tracked_domain(domain: str, configured: set[str]) -> bool:
        host = domain.casefold().removeprefix("www.")
        return any(host == known or host.endswith(f".{known}") for known in configured)

    def _known_source_clues(self) -> tuple[set[str], set[str]]:
        handles: set[str] = set()
        domains = set(TRACKED_SOURCE_DOMAINS)
        try:
            primary = _read_only(self.primary_database_path)
        except sqlite3.Error:
            primary = None
        if primary:
            try:
                if self._table_exists(primary, "x_capture_accounts"):
                    handles.update(str(row[0]).casefold().lstrip("@") for row in primary.execute(
                        "SELECT username_lower FROM x_capture_accounts WHERE enabled=1"
                    ))
                if self._table_exists(primary, "non_mainstream_media_sources"):
                    for row in primary.execute(
                        "SELECT homepage_url FROM non_mainstream_media_sources WHERE enabled=1"
                    ):
                        host = (urlparse(str(row[0] or "")).hostname or "").casefold().removeprefix("www.")
                        if host:
                            domains.add(host)
            finally:
                primary.close()
        try:
            hot = _read_only(self.hottopic_database_path)
        except sqlite3.Error:
            hot = None
        if hot:
            try:
                if self._table_exists(hot, "hottopic_accounts"):
                    handles.update(str(row[0]).casefold().lstrip("@") for row in hot.execute(
                        "SELECT screen_name_lower FROM hottopic_accounts WHERE status='followed' AND ("
                        "hot_topic_enabled=1 OR market_sentiment_enabled=1 OR project_promotion_enabled=1 OR investment_leads_enabled=1)"
                    ))
            finally:
                hot.close()
        return handles, domains

    @staticmethod
    def _ai_competitor_publish(conn: sqlite3.Connection, odaily_id: str) -> str | None:
        if not AIReviewService._table_exists(conn, "newsflash_production_metadata") or not AIReviewService._table_exists(conn, "tasks"):
            return None
        row = conn.execute(
            "SELECT t.status FROM newsflash_production_metadata m JOIN tasks t ON m.delivery_key='odaily-task-'||t.id "
            "WHERE m.odaily_id=? AND m.origin_key='competitor' AND m.outlet_category='ai_regular' "
            "AND t.status IN ('auto_published','ready_review') ORDER BY datetime(m.pushed_at) DESC LIMIT 1",
            (odaily_id,),
        ).fetchone()
        return str(row["status"]) if row else None

    def _analyze_pending(self, usage_date: str) -> dict[str, int]:
        items = self.repository.pending_analysis()
        analyzed = deferred = 0
        for item in items:
            examples = self.repository.history_examples(item["category"])
            prompt_material = _json(item["evidence"]) + item["title"] + item["category"] + _json(examples)
            reservation = max(estimate_tokens(prompt_material), len(prompt_material) * 4) + OUTPUT_TOKEN_RESERVE + 2_000
            if not self.repository.update_usage(usage_date, reserve=reservation):
                deferred += 1
                continue
            try:
                result = self.analyzer.analyze(item, examples)
                actual = result["input_tokens"] + result["output_tokens"]
                self.repository.update_usage(usage_date, settle=(reservation, actual))
                self.repository.complete_analysis(
                    item["id"], recommendation=result["recommendation"], reason=result["reason"], model=result["model"],
                    input_tokens=result["input_tokens"], output_tokens=result["output_tokens"],
                )
                analyzed += 1
            except Exception as exc:
                self.repository.release_usage(usage_date, reservation)
                self.repository.fail_analysis(item["id"], f"{type(exc).__name__}: {exc}")
        return {"analyzed": analyzed, "deferred": deferred}
