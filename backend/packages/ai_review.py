from __future__ import annotations

import hashlib
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
EVENT_SCAN_VERSION = 2
RECOMMENDATIONS = {"priority", "consider", "drop"}
RECOMMENDATION_LABELS = {"priority": "优先考虑", "consider": "可考虑", "drop": "不建议跟进"}
STATUS_VALUES = {"pending", "accepted", "rejected", "deferred"}
ATTITUDE_RE = re.compile(
    r"看多|看涨|偏多|买入|加仓|增持|强势|低估|上涨空间|利好|看空|看跌|偏空|卖出|减仓|做空|高估|下行|利空|"
    r"(?<![A-Za-z])(?:bullish|bull|long|buy(?:ing)?|accumulat\w*|undervalued|upside|bearish|bear|short|"
    r"sell(?:ing)?|overvalued|downside)(?![A-Za-z])",
    re.IGNORECASE,
)
CRYPTO_CONTEXT_RE = re.compile(
    r"加密|区块链|链上|代币|币种|合约地址|空投|质押|流动性|协议|主网|公链|DeFi|DEX|TVL|token|crypto|"
    r"blockchain|on.chain|airdrop|staking|protocol|mainnet|liquidity|defi|dex|tvl|\bCA\b",
    re.IGNORECASE,
)
LOGIC_RE = re.compile(
    r"因为|由于|原因|逻辑|基本面|价值|采用|收入|用户增长|生态|催化|供需|估值|相比|因此|若|如果|"
    r"because|since|fundamental|thesis|value|adoption|revenue|growth|ecosystem|catalyst|supply|valuation|therefore",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s<>\]\[(){}\"']+", re.IGNORECASE)
HANDLE_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{1,15})")
X_URL_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:/|\b)", re.IGNORECASE)
TICKER_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{0,9})\b")
TRACKED_SOURCE_DOMAINS = {
    "odaily.news", "theblockbeats.info", "blockbeats.info", "panewslab.com", "jinse.cn",
    "coindesk.com", "cointelegraph.com", "decrypt.co", "theblock.co",
}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ai_review_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_key TEXT NOT NULL UNIQUE,
  item_type TEXT NOT NULL CHECK(item_type IN ('event','account')),
  category TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected','deferred')),
  analysis_status TEXT NOT NULL DEFAULT 'queued' CHECK(analysis_status IN ('queued','completed','failed')),
  title TEXT NOT NULL,
  recommendation TEXT NOT NULL DEFAULT 'consider',
  reason TEXT NOT NULL DEFAULT '',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  clues_json TEXT NOT NULL DEFAULT '{}',
  account_lower TEXT,
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
  kind TEXT NOT NULL CHECK(kind IN ('account','event')),
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
        self, *, key: str, item_type: str, category: str, title: str,
        evidence: list[dict[str, Any]], clues: dict[str, Any] | None = None,
        account_lower: str | None = None, source_updated_at: str | None = None,
        refresh_existing: bool = False,
    ) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO ai_review_items(item_key,item_type,category,title,evidence_json,clues_json,"
                "account_lower,source_updated_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (key, item_type, category, title[:300], _json(evidence), _json(clues or {}), account_lower,
                 source_updated_at, now, now),
            )
            if cursor.rowcount == 0 and refresh_existing and item_type == "event":
                self.db.execute(
                    "UPDATE ai_review_items SET evidence_json=?,clues_json=?,source_updated_at=?,updated_at=? "
                    "WHERE item_key=? AND item_type='event'",
                    (_json(evidence), _json(clues or {}), source_updated_at, now, key),
                )
        return cursor.rowcount > 0

    def pending_analysis(self, item_type: str, limit: int = MAX_ANALYSIS_BATCH) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM ai_review_items WHERE item_type=? AND analysis_status IN ('queued','failed') "
            "AND analysis_attempts<? ORDER BY datetime(COALESCE(source_updated_at,created_at)) DESC,id DESC LIMIT ?",
            (item_type, MAX_ANALYSIS_ATTEMPTS, limit),
        ).fetchall()
        return [self._item(row) for row in rows]

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

    def update_usage(self, usage_date: str, kind: str, *, reserve: int = 0, settle: tuple[int, int] | None = None) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO ai_review_daily_usage(usage_date,kind,updated_at) VALUES(?,?,?)",
                (usage_date, kind, now),
            )
            row = self.db.execute(
                "SELECT used_tokens,reserved_tokens FROM ai_review_daily_usage WHERE usage_date=? AND kind=?",
                (usage_date, kind),
            ).fetchone()
            if reserve:
                if int(row["used_tokens"]) + int(row["reserved_tokens"]) + reserve > DAILY_TOKEN_LIMIT:
                    return False
                self.db.execute(
                    "UPDATE ai_review_daily_usage SET reserved_tokens=reserved_tokens+?,updated_at=? WHERE usage_date=? AND kind=?",
                    (reserve, now, usage_date, kind),
                )
            elif settle is not None:
                reserved, actual = settle
                self.db.execute(
                    "UPDATE ai_review_daily_usage SET reserved_tokens=max(0,reserved_tokens-?),used_tokens=used_tokens+?,"
                    "calls=calls+1,updated_at=? WHERE usage_date=? AND kind=?",
                    (reserved, max(0, actual), now, usage_date, kind),
                )
        return True

    def release_usage(self, usage_date: str, kind: str, reserved: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE ai_review_daily_usage SET reserved_tokens=max(0,reserved_tokens-?),updated_at=? WHERE usage_date=? AND kind=?",
                (reserved, datetime.now(UTC).isoformat(), usage_date, kind),
            )

    def usage(self, usage_date: str) -> dict[str, dict[str, int]]:
        rows = self.db.execute(
            "SELECT kind,used_tokens,reserved_tokens,calls FROM ai_review_daily_usage WHERE usage_date=?", (usage_date,)
        ).fetchall()
        result = {
            "account": {"usedTokens": 0, "reservedTokens": 0, "calls": 0},
            "event": {"usedTokens": 0, "reservedTokens": 0, "calls": 0},
        }
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
        self, *, item_type: str = "all", status: str = "all", category: str = "all",
        offset: int = 0, limit: int = 30,
    ) -> dict[str, Any]:
        clauses, params = [
            "analysis_status='completed'", "recommendation<>'drop'",
            "(item_type<>'account' OR recommendation='priority')",
        ], []
        if item_type in {"event", "account"}:
            clauses.append("item_type=?")
            params.append(item_type)
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
            "FROM ai_review_items WHERE datetime(created_at)>=datetime(?) AND analysis_status='completed' "
            "AND recommendation<>'drop' AND (item_type<>'account' OR recommendation='priority')",
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
        row = self.db.execute("SELECT * FROM ai_review_items WHERE item_key=?", (item_key,)).fetchone()
        return self._item(row) if row else None

    def decide(self, item_key: str, *, decision: str, actor: str, note: str = "") -> dict[str, Any]:
        if decision not in {"accepted", "rejected", "deferred"}:
            raise ValueError("不支持的人工决定")
        now = datetime.now(UTC).isoformat()
        with self.db:
            row = self.db.execute("SELECT * FROM ai_review_items WHERE item_key=?", (item_key,)).fetchone()
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

    def has_pending_analysis(self, item_type: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM ai_review_items WHERE item_type=? AND analysis_status IN ('queued','failed') "
            "AND analysis_attempts<? LIMIT 1", (item_type, MAX_ANALYSIS_ATTEMPTS),
        ).fetchone() is not None

    def history_examples(self, item_type: str, category: str, limit: int = 2) -> list[dict[str, str]]:
        rows = self.db.execute(
            "SELECT category,human_decision,feedback_note FROM ai_review_items WHERE item_type=? AND human_decision IS NOT NULL "
            "AND category=? ORDER BY decided_at DESC LIMIT ?", (item_type, category, limit),
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
        if item["type"] == "event":
            system = (
                "你是 Crypto 垂直媒体的信源编辑，只分析给定事件原文，不搜索外部资料。你判断的是是否值得持续跟踪这个主题或信源，"
                "不是单条新闻是否重要。单次申领通知、一次性上线或孤立观点即使对读者有用，也不能据此推荐长期追踪；"
                "优先考虑持续更新且可能影响读者交易决策的 Crypto 信源，少量股票内容可纳入。对来源归因和名誉风险从严；"
                "若原文账号只是被提及而非信息来源，应选择 drop。"
                "只返回 JSON：{\"recommendation\":\"priority|consider|drop\",\"reason\":\"简短中文理由\"}。"
            )
        else:
            system = (
                "你是 Crypto 垂直媒体的信源编辑。只根据给定作者原文和聚合证据判断账号是否值得纳入市场情绪或项目推介观察；"
                "判断其是否持续表达可用于读者交易决策的态度，或是否提供有逻辑的 Crypto/链上项目观点。少量股票观点可用于市场情绪。"
                "不要将引用者观点归给作者。只返回 JSON：{\"recommendation\":\"priority|consider|drop\",\"reason\":\"简短中文理由\"}。"
            )
        prompt = _json({
            "item": {"title": item["title"], "category": item["category"], "evidence": item["evidence"][:6]},
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


def _is_pure_x_repost(handle: str, metadata: dict[str, Any], raw: dict[str, Any]) -> bool:
    if str(metadata.get("activity_type") or metadata.get("source") or "").casefold() in {
        "repost", "retweet", "retweeted", "pure_repost",
    } or metadata.get("is_retweet") is True or metadata.get("is_repost") is True:
        return True
    envelope = raw.get("raw_payload") if isinstance(raw.get("raw_payload"), dict) else {}
    nested = envelope.get("raw_payload") if isinstance(envelope.get("raw_payload"), dict) else {}
    timeline = nested.get("timeline") if isinstance(nested.get("timeline"), dict) else envelope.get("timeline")
    candidates = [raw, envelope, nested, timeline if isinstance(timeline, dict) else {}]
    target = handle.casefold().lstrip("@")
    author = str(metadata.get("author_username") or "").casefold().lstrip("@")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        reposted_by = candidate.get("reposted_by") if isinstance(candidate.get("reposted_by"), dict) else {}
        reposted_by_name = str(reposted_by.get("screen_name") or "").casefold().lstrip("@")
        candidate_author = candidate.get("author") if isinstance(candidate.get("author"), dict) else {}
        original_author = str(candidate_author.get("screen_name") or author).casefold().lstrip("@")
        if reposted_by_name == target and original_author and original_author != target:
            return True
        if any(isinstance(candidate.get(key), dict) and candidate.get(key) for key in ("retweeted_status", "repost")):
            if original_author and original_author != target:
                return True
    return False


def _match_alias(text: str, alias: str) -> bool:
    clean = alias.strip()
    if not clean:
        return False
    if re.fullmatch(r"\$?[A-Za-z][A-Za-z0-9]{0,9}", clean):
        token = re.escape(clean.lstrip("$").casefold())
        return re.search(rf"(?<![A-Za-z0-9_])\$?{token}(?![A-Za-z0-9_])", text, re.IGNORECASE) is not None
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(clean)}(?![A-Za-z0-9_])", text, re.IGNORECASE) is not None


def _compile_alias_matcher(aliases: dict[str, Any]) -> dict[Any, Any] | None:
    normalized = sorted(
        {str(alias).strip().casefold() for alias in aliases if str(alias).strip()},
        key=lambda alias: (-len(alias), alias),
    )
    if not normalized:
        return None
    root: dict[Any, Any] = {}
    for alias in normalized:
        searchable = alias.lstrip("$") if re.fullmatch(r"\$?[A-Za-z][A-Za-z0-9]{0,9}", alias) else alias
        node = root
        for character in searchable:
            node = node.setdefault(character, {})
        node.setdefault(None, []).append(alias)
    return root


def _find_alias_matches(text: str, matcher: dict[Any, Any] | None) -> set[str]:
    if matcher is None:
        return set()
    folded = text.casefold()
    original_positions: list[int] = []
    for index, character in enumerate(text):
        original_positions.extend([index] * len(character.casefold()))

    def is_word_character(character: str) -> bool:
        return character.isascii() and (character.isalnum() or character == "_")

    matches: set[str] = set()
    cursor = 0
    while cursor < len(folded):
        node = matcher
        scan = cursor
        best_alias = None
        best_end = cursor
        while scan < len(folded):
            node = node.get(folded[scan])
            if node is None:
                break
            scan += 1
            aliases = node.get(None)
            if aliases:
                start = original_positions[cursor]
                end = original_positions[scan - 1] + 1
                before = text[start - 1] if start else ""
                after = text[end] if end < len(text) else ""
                if not is_word_character(before) and not is_word_character(after):
                    best_alias, best_end = aliases[0], scan
        if best_alias is not None:
            matches.add(best_alias)
            cursor = best_end
        else:
            cursor += 1
    return matches


def _extract_clues(texts: list[str]) -> dict[str, list[str]]:
    accounts: set[str] = set()
    domains: set[str] = set()
    for text in texts:
        accounts.update(handle.lower() for handle in HANDLE_RE.findall(text))
        accounts.update(handle.lower() for handle in X_URL_RE.findall(text))
        for raw in URL_RE.findall(text):
            host = urlparse(raw.rstrip(".,;!?")).hostname or ""
            host = host.lower().removeprefix("www.")
            if host and host not in {"x.com", "twitter.com", "t.co"}:
                domains.add(host)
    return {"xAccounts": sorted(accounts)[:30], "domains": sorted(domains)[:30]}


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
            item_type=str(payload.get("type") or "all"), status=str(payload.get("status") or "all"),
            category=str(payload.get("category") or "all"),
            offset=int(payload.get("offset") or 0), limit=int(payload.get("limit") or 30),
        )

    def get_item(self, item_key: str) -> dict[str, Any] | None:
        return self.repository.get_item(item_key)

    def decide(self, item_key: str, *, decision: str, actor: str, note: str = "") -> dict[str, Any]:
        item = self.repository.get_item(item_key)
        if item is None:
            raise ValueError("审阅项目不存在")
        if item["type"] == "account" and decision not in {"accepted", "rejected"}:
            raise ValueError("账号建议只能认可或驳回")
        if item["type"] == "account" and item["status"] == "accepted":
            return item
        return self.repository.decide(item_key, decision=decision, actor=actor, note=note)

    def run_if_due(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        local = current.astimezone(BEIJING_TZ)
        if local.hour < 6:
            return {"status": "not_due"}
        today = local.date().isoformat()
        event_scan_key = f"last_event_scan_date_v{EVENT_SCAN_VERSION}"
        account_scan_due = self.repository.get_state("last_scan_date") != today
        event_scan_due = self.repository.get_state(event_scan_key) != today
        if self.repository.get_state("last_completed_date") == today and not (account_scan_due or event_scan_due):
            return {"status": "already_ran", "date": today}
        since = current - timedelta(days=REVIEW_LOOKBACK_DAYS)
        queued = {"account": 0, "event": 0}
        if account_scan_due:
            queued["account"] = self._queue_accounts(since)
            self.repository.set_state("last_scan_date", today)
        if event_scan_due:
            queued["event"] = self._queue_events(since)
            self.repository.set_state(event_scan_key, today)
        def analyze_kind(kind: str) -> dict[str, int]:
            if self.repository.get_state(f"budget_blocked_{kind}") == today:
                return {"analyzed": 0, "deferred": 1}
            result = self._analyze_pending(kind, today)
            if result["deferred"]:
                self.repository.set_state(f"budget_blocked_{kind}", today)
            return result

        account_result = analyze_kind("account")
        event_result = analyze_kind("event")
        account_pending = self.repository.has_pending_analysis("account")
        event_pending = self.repository.has_pending_analysis("event")
        account_done = not account_pending or account_result["deferred"] > 0
        event_done = not event_pending or event_result["deferred"] > 0
        complete = account_done and event_done
        if complete:
            self.repository.set_state("last_completed_date", today)
        return {
            "status": "completed" if complete else "in_progress", "date": today, **queued,
            "analyzed": account_result["analyzed"] + event_result["analyzed"],
            "deferred": account_result["deferred"] + event_result["deferred"],
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
                "e.competitor_source_count,e.has_odaily,i.source,i.source_item_id,i.source_url,i.title,i.content,i.published_at "
                "FROM newsflash_events e JOIN newsflash_event_sources s ON s.event_id=e.event_id "
                "JOIN newsflash_items i ON i.id=s.item_id WHERE e.status='active' AND datetime(e.event_time)>=datetime(?) "
                "ORDER BY datetime(e.event_time) DESC LIMIT 10000", (since.isoformat(),),
            ).fetchall()
            events: dict[str, list[sqlite3.Row]] = defaultdict(list)
            for row in rows:
                events[str(row["event_id"])].append(row)
            known_handles, known_domains = self._known_source_clues()
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
                elif odaily and not competitors:
                    category = "odaily_only"
                if not category:
                    continue
                title = str(sources[0]["representative_title"] or sources[0]["title"] or "未命名事件")
                if category == "competitor_first_ai":
                    title = f"{title} · 竞品先发 / Odaily {'直发' if publish_status == 'auto_published' else '挂后台'}"
                elif category == "multiple_competitors_no_odaily":
                    title = f"{title} · 多家竞品发布，Odaily 未发"
                else:
                    title = f"{title} · 仅 Odaily 发布"
                relevant_sources = odaily if category == "odaily_only" else competitors
                linked_outlets = {
                    str(row["source"]).casefold()
                    for row in relevant_sources if str(row["source_url"] or "").strip()
                }
                if not linked_outlets or (
                    category == "multiple_competitors_no_odaily" and len(linked_outlets) < 2
                ):
                    continue

                ordered_sources = sorted(
                    sources,
                    key=lambda row: (
                        str(row["source"]).casefold() == "odaily",
                        _parse_datetime(row["published_at"]) or datetime.max.replace(tzinfo=UTC),
                    ),
                )
                evidence, text_parts = [], []
                for source in ordered_sources[:20]:
                    source_url = str(source["source_url"] or "")
                    parsed_url = urlparse(source_url)
                    domain = (parsed_url.hostname or "").casefold().removeprefix("www.")
                    if domain in {"x.com", "twitter.com", "t.co"}:
                        domain = ""
                    handle_match = X_URL_RE.search(source_url)
                    source_account = handle_match.group(1).lower() if handle_match else ""
                    source_kind = "odaily" if str(source["source"]).casefold() == "odaily" else "competitor"
                    tracked = None
                    if source_account:
                        tracked = source_account in known_handles
                    elif domain:
                        tracked = self._is_tracked_domain(domain, known_domains)
                    source_title = str(source["title"] or "")
                    content = str(source["content"] or "")[:1800]
                    evidence.append({
                        "source": str(source["source"]), "sourceKind": source_kind,
                        "sourceDomain": domain or None, "sourceAccount": source_account or None,
                        "tracked": tracked, "title": source_title, "url": source_url,
                        "publishedAt": source["published_at"], "text": content,
                    })
                    text_parts.extend((source_title, content, source_url))
                key = f"event:{event_id}:{category}"
                clues = _extract_clues(text_parts)
                inserted += int(self.repository.insert_candidate(
                    key=key, item_type="event", category=category, title=title, evidence=evidence,
                    clues=clues, source_updated_at=str(sources[0]["published_at"] or ""), refresh_existing=True,
                ))
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

    def _vocabulary(self, since: datetime) -> tuple[dict[str, str | None], dict[str, str]]:
        sentiment: dict[str, str | None] = {}
        projects: dict[str, str] = {}
        try:
            conn = _read_only(self.hottopic_database_path)
        except sqlite3.Error:
            return sentiment, projects
        try:
            if self._table_exists(conn, "x_agent_sentiment_results"):
                for row in conn.execute(
                    "SELECT instrument_key,instrument_name,ticker,scope FROM x_agent_sentiment_results WHERE datetime(posted_at)>=datetime(?)",
                    (since.isoformat(),),
                ):
                    if str(row["scope"]) not in {"Crypto具体标的", "美股具体标的", "大盘"}:
                        continue
                    canonical = str(row["instrument_key"] or "").strip()
                    for alias in (row["ticker"], row["instrument_name"]):
                        clean = str(alias or "").strip()
                        if clean and len(clean) >= 2 and canonical:
                            normalized = clean.casefold()
                            if normalized not in sentiment:
                                sentiment[normalized] = canonical
                            elif sentiment[normalized] is not None and sentiment[normalized] != canonical:
                                sentiment[normalized] = None
            if self._table_exists(conn, "x_agent_project_observations"):
                for row in conn.execute(
                    "SELECT identity_key,project_name,ticker,chain_name FROM x_agent_project_observations WHERE datetime(last_mentioned_at)>=datetime(?)",
                    (since.isoformat(),),
                ):
                    for alias in (row["project_name"], row["ticker"], row["chain_name"]):
                        if alias and len(str(alias).strip()) >= 2:
                            projects.setdefault(str(alias).strip().casefold(), str(row["identity_key"] or alias))
        finally:
            conn.close()
        return sentiment, projects

    def _account_posts(self, since: datetime) -> list[dict[str, Any]]:
        posts: dict[str, dict[str, Any]] = {}
        try:
            hot = _read_only(self.hottopic_database_path)
        except sqlite3.Error:
            return []
        try:
            if not self._table_exists(hot, "hottopic_accounts"):
                return []
            tracked_accounts = {
                str(row["screen_name_lower"]).casefold()
                for row in hot.execute(
                    "SELECT screen_name_lower FROM hottopic_accounts WHERE status='followed' AND ("
                    "hot_topic_enabled=1 OR market_sentiment_enabled=1 OR project_promotion_enabled=1 OR investment_leads_enabled=1)"
                )
            }
        finally:
            hot.close()
        try:
            conn = _read_only(self.primary_database_path)
        except sqlite3.Error:
            conn = None
        if conn:
            try:
                if self._table_exists(conn, "tasks"):
                    rows = conn.execute(
                        "SELECT source_item_id,source_url,content,published_at,metadata,raw_payload FROM tasks "
                        "WHERE source='x' AND datetime(COALESCE(published_at,created_at))>=datetime(?) "
                        "ORDER BY datetime(COALESCE(published_at,created_at)) DESC LIMIT 20000",
                        (since.isoformat(),),
                    ).fetchall()
                    for row in rows:
                        meta = _decode(row["metadata"], {})
                        if not isinstance(meta, dict):
                            continue
                        handle = str(meta.get("account_username") or meta.get("author_username") or "").strip().lstrip("@")
                        text = str(row["content"] or "").strip()
                        if (handle.casefold() in tracked_accounts and text
                                and not _is_pure_x_repost(handle, meta, _decode(row["raw_payload"], {}))):
                            posts[str(row["source_item_id"])] = {
                                "account": handle, "text": text, "url": str(row["source_url"] or ""),
                                "postedAt": row["published_at"], "tweetId": str(row["source_item_id"]),
                            }
            finally:
                conn.close()
        try:
            hot = _read_only(self.hottopic_database_path)
        except sqlite3.Error:
            return []
        try:
            if self._table_exists(hot, "hottopic_inbox"):
                rows = hot.execute(
                    "SELECT tweet_id,screen_name_lower,payload_json,collected_at FROM hottopic_inbox "
                    "WHERE datetime(collected_at)>=datetime(?) LIMIT 20000", (since.isoformat(),),
                ).fetchall()
                for row in rows:
                    payload = _decode(row["payload_json"], {})
                    if not isinstance(payload, dict) or str(payload.get("activity_type") or "").lower() in {"repost", "retweet", "pure_repost"}:
                        continue
                    account = str(row["screen_name_lower"] or "").casefold()
                    if account not in tracked_accounts:
                        continue
                    own_text = str(payload.get("text") or "").strip()
                    if own_text:
                        posts.setdefault(str(row["tweet_id"]), {
                            "account": account, "text": own_text,
                            "url": str(payload.get("url") or ""),
                            "postedAt": payload.get("created_at_iso") or row["collected_at"], "tweetId": str(row["tweet_id"]),
                        })
        finally:
            hot.close()
        return list(posts.values())

    def _queue_accounts(self, since: datetime) -> int:
        assets, project_aliases = self._vocabulary(since)
        if not assets and not project_aliases:
            return 0
        posts = self._account_posts(since)
        usable_assets = {alias: target for alias, target in assets.items() if target}
        asset_matcher = _compile_alias_matcher(usable_assets)
        by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for post in posts:
            aliases = _find_alias_matches(post["text"], asset_matcher)
            if aliases and ATTITUDE_RE.search(post["text"]):
                post["matchedAssets"] = list(dict.fromkeys(usable_assets[alias] for alias in aliases))
                by_account[post["account"].lower()].append(post)
        inserted = 0
        enabled = self._enabled_modules()
        for account, rows in by_account.items():
            unique_assets: dict[str, dict[str, Any]] = {}
            for row in rows:
                for asset in row["matchedAssets"]:
                    unique_assets.setdefault(asset.casefold(), row)
            if len(unique_assets) >= 3 and "market_sentiment" not in enabled.get(account, set()):
                evidence = self._compact_account_evidence(list(unique_assets.values()))
                latest = max((str(row.get("postedAt") or "") for row in evidence), default="")
                inserted += int(self.repository.insert_candidate(
                    key=f"account:{account}:market_sentiment", item_type="account", category="market_sentiment",
                    title=f"@{account} · 市场情绪信源候选", evidence=evidence,
                    clues=_extract_clues([row["text"] for row in evidence]), account_lower=account, source_updated_at=latest,
                ))
        crypto_by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
        crypto_aliases = {
            **project_aliases,
            **{alias: target for alias, target in assets.items() if target and target.startswith("Crypto具体标的:")},
        }
        crypto_matcher = _compile_alias_matcher(crypto_aliases)
        for post in posts:
            known = _find_alias_matches(post["text"], crypto_matcher)
            if known or CRYPTO_CONTEXT_RE.search(post["text"]):
                post["matchedProjects"] = list(dict.fromkeys(crypto_aliases[alias] for alias in known))
                crypto_by_account[post["account"].lower()].append(post)
        for account, rows in crypto_by_account.items():
            unique_posts = {str(row.get("tweetId") or hashlib.sha1(row["text"].encode()).hexdigest()): row for row in rows}
            logic_rows = [row for row in unique_posts.values() if LOGIC_RE.search(row["text"])]
            if len(unique_posts) >= 3 and logic_rows and "project_promotion" not in enabled.get(account, set()):
                selected = sorted(unique_posts.values(), key=lambda row: str(row.get("postedAt") or ""), reverse=True)[:6]
                evidence = self._compact_account_evidence(selected)
                latest = max((str(row.get("postedAt") or "") for row in evidence), default="")
                inserted += int(self.repository.insert_candidate(
                    key=f"account:{account}:project_promotion", item_type="account", category="project_promotion",
                    title=f"@{account} · 项目推介信源候选", evidence=evidence,
                    clues=_extract_clues([row["text"] for row in evidence]), account_lower=account, source_updated_at=latest,
                ))
        return inserted

    @staticmethod
    def _compact_account_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selected = sorted(rows, key=lambda row: str(row.get("postedAt") or ""), reverse=True)[:6]
        return [{
            "source": "X", "url": row.get("url", ""), "postedAt": row.get("postedAt"), "tweetId": row.get("tweetId"),
            "text": str(row.get("text") or "")[:500], "matchedAssets": row.get("matchedAssets", []),
            "matchedProjects": row.get("matchedProjects", []),
        } for row in selected]

    def _enabled_modules(self) -> dict[str, set[str]]:
        try:
            conn = _read_only(self.hottopic_database_path)
        except sqlite3.Error:
            return {}
        try:
            if not self._table_exists(conn, "hottopic_accounts"):
                return {}
            result: dict[str, set[str]] = {}
            for row in conn.execute(
                "SELECT screen_name_lower,market_sentiment_enabled,project_promotion_enabled FROM hottopic_accounts"
            ):
                enabled: set[str] = set()
                if row["market_sentiment_enabled"]:
                    enabled.add("market_sentiment")
                if row["project_promotion_enabled"]:
                    enabled.add("project_promotion")
                result[str(row["screen_name_lower"]).lower()] = enabled
            return result
        finally:
            conn.close()

    def _analyze_pending(self, kind: str, usage_date: str) -> dict[str, int]:
        items = self.repository.pending_analysis(kind)
        analyzed = deferred = 0
        for item in items:
            examples = self.repository.history_examples(kind, item["category"])
            prompt_material = _json(item["evidence"]) + item["title"] + item["category"] + _json(examples)
            reservation = max(estimate_tokens(prompt_material), len(prompt_material) * 4) + OUTPUT_TOKEN_RESERVE + 2_000
            if not self.repository.update_usage(usage_date, kind, reserve=reservation):
                deferred += 1
                continue
            try:
                result = self.analyzer.analyze(item, examples)
                actual = result["input_tokens"] + result["output_tokens"]
                self.repository.update_usage(usage_date, kind, settle=(reservation, actual))
                self.repository.complete_analysis(
                    item["id"], recommendation=result["recommendation"], reason=result["reason"], model=result["model"],
                    input_tokens=result["input_tokens"], output_tokens=result["output_tokens"],
                )
                analyzed += 1
            except Exception as exc:
                self.repository.release_usage(usage_date, kind, reservation)
                self.repository.fail_analysis(item["id"], f"{type(exc).__name__}: {exc}")
        return {"analyzed": analyzed, "deferred": deferred}
