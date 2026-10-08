"""Incremental topic aggregation backed by a local SQLite database.

The public seam is intentionally small: ``TopicAggregator.process_batch``.
Collectors and replay drivers pass content items and a virtual/real batch time;
all topic, claim, membership, lifecycle, retrieval and brief state stays here.
Aggregation and state transitions are deterministic.  Reader-facing briefs
are produced only by the configured AI writer; there is no local prose
fallback.  A future extractor/resolver can be injected without changing the
seam.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


UTC_NOW = lambda: datetime.now(UTC)
MIN_VISIBLE_PARTICIPANTS = 5
TRANSIENT_RETENTION_HOURS = 48
BRIEF_RETRY_BASE_MINUTES = 5
BRIEF_RETRY_MAX_MINUTES = 60
SEMANTIC_REVIEW_BATCH_SIZE = 40
SEMANTIC_REVIEW_BACKFILL_LIMIT = 120
SEMANTIC_REVIEW_DECISIONS = frozenset({"support", "context", "unrelated"})

RE_CASHTAG = re.compile(r"\$[A-Za-z\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{1,30}")
RE_HASHTAG = re.compile(r"#[A-Za-z\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{1,30}")
RE_MENTION = re.compile(r"@[A-Za-z0-9_]{2,30}")
RE_URL = re.compile(r"https?://[^\s)]+", re.IGNORECASE)
RE_CONTRACT = re.compile(r"\b(?:0x[a-fA-F0-9]{8,}|[1-9A-HJ-NP-Za-km-z]{32,44})\b")
RE_BARE_ASSET = re.compile(r"(?<![A-Za-z0-9$])([A-Z][A-Z0-9]{2,9})(?![A-Za-z0-9])")
RE_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_#$-]{1,30}|[\u4e00-\u9fff]{2,}")

STOPWORDS = {
    "about", "after", "again", "also", "been", "being", "from", "have",
    "into", "just", "more", "most", "only", "that", "their", "there",
    "these", "they", "this", "what", "when", "where", "which", "with",
    "would", "your", "一个", "一些", "因为", "可以", "已经", "这个", "那个",
    "以及", "我们", "你们", "现在", "真的", "就是", "没有", "不是", "如果",
}

LAUNCH_SIGNALS = (
    "宣布", "发布", "推出", "上线", "发射台", "新台子", "注意力榜单",
    "launchpad", "launched", "launching", "announced", "released", "rollout",
    "introduce", "introduces", "introducing",
)
LISTING_SIGNALS = (
    "上币", "现货交易", "交易路线图", "路线图",
    "listing", "listed", "roadmap", "launch trading", "launch of trading", "spot trading",
)
EVENT_FORMATION_SIGNALS = LAUNCH_SIGNALS + LISTING_SIGNALS
MECHANISM_SIGNALS = (
    "空投", "销毁", "回购", "排名", "冲榜", "曝光", "持有人", "机制",
    "airdrop", "burn", "buy+burn", "ranking", "rank", "holder", "spotlight",
)
IDENTITY_STOPWORDS = STOPWORDS | {
    "token", "tokens", "launch", "launched", "launching", "launchpad", "platform",
    "announced", "released", "team", "project", "projects", "market", "meme", "memes",
    "推出", "发布", "上线", "宣布", "发射台", "平台", "项目", "代币", "市场", "注意力",
}
GENERIC_NAMED_EVENT_ENTITIES = IDENTITY_STOPWORDS | {
    "access", "available", "campaign", "campaigns", "faq", "first", "introducing",
    "official", "pre", "protocol", "reading", "soon", "swap", "through", "wallet",
}
GENERIC_ASSET_IDENTITIES = {
    "btc", "eth", "sol", "bnb", "usdc", "usdt", "xrp", "doge",
}
GENERIC_BARE_ASSET_LABELS = {
    "AI", "API", "ATH", "ATL", "BSC", "CA", "CEX", "DAO", "DEX", "ERC",
    "FAQ", "FDV", "KOL", "L2", "NFT", "ROI", "TVL", "USD", "URL",
}
GENERIC_CHINESE_IDENTITY_TERMS = {
    "一个", "一些", "因为", "可以", "已经", "这个", "那个", "以及", "我们", "你们", "现在",
    "真的", "就是", "没有", "不是", "如果", "未来", "市场", "交易", "代币", "项目", "平台",
    "上线", "现货", "合约", "持仓", "流动", "动性", "社区", "生态", "上涨", "下跌", "继续",
    "开始", "集中", "讨论", "认为", "意义", "连接", "有人", "大家", "惊叹", "后悔", "卖飞",
}
# A hashtag becomes a cross-Topic subject anchor only when the same label is
# also used as an explicit asset somewhere in the batch.  This list is kept
# deliberately small: it protects against ordinary prose labels without
# classifying the events themselves.
GENERIC_TOPIC_LABELS = {
    "感觉", "挑战", "直觉", "市场", "交易", "社区", "生态", "项目", "平台", "代币",
}
EVENT_ASSERTION_SIGNALS = EVENT_FORMATION_SIGNALS + (
    "关闭", "停止", "提现", "截止", "收购", "合作", "协议", "获批", "批准", "签署", "供应",
    "closing", "closed", "shutdown", "withdraw", "deadline", "acquired", "acquisition",
    "partnership", "agreement", "signed", "approved", "approval", "supply", "provide",
)


def iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S %z"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"unsupported datetime: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def stable_id(prefix: str, *parts: object, length: int = 16) -> str:
    material = "\x1f".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha1(material.encode('utf-8')).hexdigest()[:length]}"


def compact(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def tokens(text: str) -> set[str]:
    result: set[str] = set()
    for raw in RE_WORD.findall(text.lower()):
        word = raw.strip("-_#")
        if not word or word in STOPWORDS:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]{2,}", word):
            result.update(word[i : i + 2] for i in range(len(word) - 1))
            result.add(word)
        elif len(word) >= 2:
            result.add(word)
    result.update(x.lower() for x in RE_CASHTAG.findall(text))
    result.update(x.lower() for x in RE_HASHTAG.findall(text))
    result.update(x.lower() for x in RE_MENTION.findall(text))
    return result


def subject_tags(text: str) -> set[str]:
    """Return normalized explicit asset labels.

    A dollar label establishes asset identity.  A hashtag is only a prose
    alias: tags such as #感觉, #挑战 or #币安 are topical metadata and must
    never gain irreversible merge authority by themselves.
    """
    values = {
        value.lower().lstrip("$")
        for value in RE_CASHTAG.findall(text)
        if value.lower().lstrip("$") not in GENERIC_ASSET_IDENTITIES
    }
    # Chinese cashtags are not followed by an ASCII word boundary.  In text
    # such as "$牛来上合约之前... $牛来", the greedy regex sees the prose after
    # the real label as part of the first tag.  When a shorter Chinese label
    # is present, discard its longer prefix-continuation.
    chinese_values = sorted(
        (value for value in values if all("\u4e00" <= char <= "\u9fff" for char in value)),
        key=len,
    )
    values = {
        value for value in values
        if not any(
            shorter != value and len(shorter) >= 2 and value.startswith(shorter)
            for shorter in chinese_values
        )
    }
    return values


def asset_labels(text: str) -> set[str]:
    """Return cashtags and unambiguous bare ticker-style asset labels."""
    values = subject_tags(text)
    scan_text = RE_URL.sub(" ", text)
    values.update(
        value.lower()
        for value in RE_BARE_ASSET.findall(scan_text)
        if value not in GENERIC_BARE_ASSET_LABELS
        and value.lower() not in GENERIC_ASSET_IDENTITIES
    )
    return values


def hard_keys(item: "ContentItem", text: str) -> set[str]:
    quote_id = item.references.get("quote_id")
    reply_to = item.references.get("reply_to")
    result = {f"quote:{quote_id}"} if quote_id else set()
    if reply_to:
        result.add(f"reply:{reply_to}")
    result.update(f"asset:{value}" for value in asset_labels(text))
    result.update(f"url:{x.lower().rstrip('.,')}" for x in RE_URL.findall(text))
    result.update(f"contract:{x.lower()}" for x in RE_CONTRACT.findall(text))
    return result


@dataclass(frozen=True)
class ContentItem:
    content_item_id: str
    tweet_id: str
    activity_account: str
    author: str
    activity_type: str
    content_text: str
    expanded_text: str
    created_at: str
    source_url: str
    metrics: dict[str, Any]
    references: dict[str, Any]
    raw_payload: dict[str, Any]

    @classmethod
    def from_any(cls, value: "ContentItem | dict[str, Any]") -> "ContentItem | None":
        if isinstance(value, cls):
            return value if value.activity_type not in {"repost", "retweet", "pure_repost"} else None
        row = dict(value)
        activity_type = compact(row.get("activity_type") or row.get("type") or "original").lower()
        if activity_type in {"repost", "retweet", "pure_repost"}:
            return None
        tweet_id = compact(row.get("tweet_id") or row.get("id"))
        text = compact(row.get("text") or row.get("content_text") or row.get("raw_text"))
        expanded = compact(row.get("expanded_text") or text)
        if not tweet_id or not expanded:
            return None
        account = compact(row.get("activity_account") or row.get("account_screen_name") or row.get("author_screen_name"))
        author = compact(row.get("author") or row.get("author_screen_name") or account)
        created = iso(row.get("created_at_iso") or row.get("created_at"))
        quote_id = compact(row.get("quote_id") or row.get("quoted_tweet_id")) or None
        reply_to = compact(row.get("reply_to") or row.get("reply_root") or row.get("replying_to")) or None
        references = {
            "quote_id": quote_id,
            "quote_author": compact(row.get("quote_author_screen_name") or row.get("quote_author")) or None,
            "reply_to": reply_to,
            "urls": RE_URL.findall(expanded),
        }
        metrics = {key: row.get(key, 0) for key in ("likes", "views", "reposts", "quotes", "replies")}
        raw = row.get("raw_payload")
        if not isinstance(raw, dict):
            raw = row
        return cls(
            content_item_id=compact(row.get("content_item_id")) or f"tweet:{tweet_id}",
            tweet_id=tweet_id,
            activity_account=account or author,
            author=author,
            activity_type=activity_type,
            content_text=text,
            expanded_text=expanded,
            created_at=created,
            source_url=compact(row.get("source_url") or row.get("url")) or f"https://x.com/{author}/status/{tweet_id}",
            metrics=metrics,
            references=references,
            raw_payload=raw,
        )


@dataclass(frozen=True)
class Claim:
    claim_id: str
    content_item_id: str
    claim_text: str
    claim_kind: str
    entities: tuple[str, ...]
    action_or_issue: str
    stance: str
    confidence: float
    information_value: float
    evidence_span: str


@dataclass(frozen=True)
class Candidate:
    topic_id: str
    score: float
    hard_relation: float
    entity_overlap: float
    semantic_similarity: float
    thread_match: float
    time_proximity: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TopicAssessment:
    qualified_accounts: frozenset[str]
    coherence_score: float
    event_anchor: str
    has_substantive_evidence: bool

    @property
    def qualifies(self) -> bool:
        return (
            len(self.qualified_accounts) >= MIN_VISIBLE_PARTICIPANTS
            and self.coherence_score >= 0.6
            and self.has_substantive_evidence
        )


@dataclass(frozen=True)
class EventIdentity:
    """Typed identity used only for irreversible local topic merges.

    Retrieval deliberately uses broad lexical and entity features.  Merge
    identity is narrower: exact non-generic assets/contracts, plus explicit
    product-name aliases that can connect prose to one of those anchors.
    """

    merge_anchors: frozenset[str]
    aliases: frozenset[str]
    named_entities: frozenset[str]
    name_tokens: frozenset[str]
    lookup_terms: frozenset[str]


Extractor = Callable[[ContentItem], Sequence[Claim]]
Resolver = Callable[[Claim, Sequence[Candidate], sqlite3.Connection], dict[str, Any] | None]
SemanticReviewer = Callable[[Sequence[dict[str, Any]]], Sequence[dict[str, Any]]]
TopicMergeReviewer = Callable[[Sequence[dict[str, Any]]], Sequence[dict[str, Any]]]


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS batches (
  batch_id TEXT PRIMARY KEY,
  batch_time TEXT NOT NULL,
  input_count INTEGER NOT NULL,
  committed_at TEXT NOT NULL,
  result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_items (
  content_item_id TEXT PRIMARY KEY,
  tweet_id TEXT NOT NULL UNIQUE,
  activity_account TEXT NOT NULL,
  author TEXT NOT NULL,
  activity_type TEXT NOT NULL,
  content_text TEXT NOT NULL,
  expanded_text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  source_url TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  references_json TEXT NOT NULL,
  raw_payload_json TEXT NOT NULL,
  fingerprint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_created ON content_items(created_at);
CREATE TABLE IF NOT EXISTS claims (
  claim_id TEXT PRIMARY KEY,
  content_item_id TEXT NOT NULL REFERENCES content_items(content_item_id),
  claim_text TEXT NOT NULL,
  claim_kind TEXT NOT NULL,
  entities_json TEXT NOT NULL,
  action_or_issue TEXT NOT NULL,
  stance TEXT NOT NULL,
  evidence_span TEXT NOT NULL,
  confidence REAL NOT NULL,
  information_value REAL NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_item ON claims(content_item_id);
CREATE TABLE IF NOT EXISTS topics (
  topic_id TEXT PRIMARY KEY,
  working_title TEXT NOT NULL,
  canonical_subject TEXT NOT NULL,
  core_entities_json TEXT NOT NULL,
  event_or_issue TEXT NOT NULL,
  started_at TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  seed_expires_at TEXT NOT NULL,
  last_evidence_at TEXT,
  last_participation_at TEXT,
  matching_status TEXT NOT NULL CHECK(matching_status IN ('seed','active','archived')),
  visibility TEXT NOT NULL CHECK(visibility IN ('hidden','visible')),
  archived_at TEXT,
  brief_status TEXT NOT NULL DEFAULT 'pending' CHECK(brief_status IN ('pending','ready','error')),
  brief_error TEXT,
  brief_error_at TEXT,
  brief_retry_count INTEGER NOT NULL DEFAULT 0,
  brief_retry_after TEXT,
  identity_revision INTEGER NOT NULL DEFAULT 1,
  participant_count_1h INTEGER NOT NULL DEFAULT 0,
  participant_count_6h INTEGER NOT NULL DEFAULT 0,
  participant_count_24h INTEGER NOT NULL DEFAULT 0,
  participant_velocity REAL NOT NULL DEFAULT 0,
  hotness_score REAL NOT NULL DEFAULT 0,
  retention_tier TEXT NOT NULL DEFAULT 'transient' CHECK(retention_tier IN ('transient','permanent'))
);
CREATE INDEX IF NOT EXISTS idx_topic_status ON topics(matching_status, visibility);
CREATE TABLE IF NOT EXISTS threads (
  thread_id TEXT PRIMARY KEY,
  topic_id TEXT NOT NULL REFERENCES topics(topic_id),
  working_label TEXT NOT NULL,
  current_thesis TEXT NOT NULL,
  relation_to_topic TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  source_count INTEGER NOT NULL DEFAULT 0,
  independent_account_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_topic_relation ON threads(topic_id, relation_to_topic);
CREATE TABLE IF NOT EXISTS memberships (
  membership_id TEXT PRIMARY KEY,
  claim_id TEXT NOT NULL REFERENCES claims(claim_id),
  topic_id TEXT NOT NULL REFERENCES topics(topic_id),
  thread_id TEXT REFERENCES threads(thread_id),
  topic_role TEXT NOT NULL,
  role TEXT NOT NULL,
  match_score REAL NOT NULL,
  decision_source TEXT NOT NULL,
  decision_reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  superseded_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_membership_unique ON memberships(claim_id, topic_id, thread_id);
CREATE INDEX IF NOT EXISTS idx_membership_topic ON memberships(topic_id);
CREATE TABLE IF NOT EXISTS topic_participations (
  topic_id TEXT NOT NULL REFERENCES topics(topic_id),
  activity_account TEXT NOT NULL,
  last_participation_at TEXT NOT NULL,
  last_content_item_id TEXT NOT NULL,
  PRIMARY KEY(topic_id, activity_account)
);
CREATE TABLE IF NOT EXISTS retrieval_state (
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  tokens_json TEXT NOT NULL,
  hard_keys_json TEXT NOT NULL,
  entities_json TEXT NOT NULL,
  representative_claim_ids_json TEXT NOT NULL,
  recent_claim_ids_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(scope_type, scope_id)
);
CREATE TABLE IF NOT EXISTS brief_revisions (
  topic_id TEXT NOT NULL REFERENCES topics(topic_id),
  revision INTEGER NOT NULL,
  title TEXT NOT NULL,
  brief TEXT NOT NULL,
  source_claim_ids_json TEXT NOT NULL,
  source_bindings_json TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  generation_reason TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  PRIMARY KEY(topic_id, revision)
);
CREATE TABLE IF NOT EXISTS topic_evidence (
  topic_id TEXT NOT NULL REFERENCES topics(topic_id),
  claim_id TEXT NOT NULL REFERENCES claims(claim_id),
  PRIMARY KEY(topic_id, claim_id)
);
CREATE INDEX IF NOT EXISTS idx_topic_evidence_claim ON topic_evidence(claim_id);
CREATE TABLE IF NOT EXISTS decision_audit (
  decision_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  content_item_id TEXT,
  claim_id TEXT,
  action TEXT NOT NULL,
  topic_ids_json TEXT NOT NULL,
  candidates_json TEXT NOT NULL,
  reason TEXT NOT NULL,
  decision_source TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_batch ON decision_audit(batch_id);
CREATE TABLE IF NOT EXISTS topic_merges (
  merge_id TEXT PRIMARY KEY,
  source_topic_id TEXT NOT NULL REFERENCES topics(topic_id),
  target_topic_id TEXT NOT NULL REFERENCES topics(topic_id),
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topic_merge_target ON topic_merges(target_topic_id);
CREATE TABLE IF NOT EXISTS topic_pair_reviews (
  left_topic_id TEXT NOT NULL REFERENCES topics(topic_id),
  right_topic_id TEXT NOT NULL REFERENCES topics(topic_id),
  input_hash TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('merge','separate','failed')),
  reason TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  model TEXT NOT NULL DEFAULT '',
  reasoning_effort TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  reviewed_at TEXT NOT NULL,
  next_attempt_at TEXT,
  PRIMARY KEY(left_topic_id,right_topic_id)
);
CREATE TABLE IF NOT EXISTS topic_claim_reviews (
  claim_id TEXT NOT NULL REFERENCES claims(claim_id),
  topic_id TEXT NOT NULL REFERENCES topics(topic_id),
  decision TEXT NOT NULL CHECK(decision IN ('support','context','unrelated')),
  confidence REAL NOT NULL DEFAULT 0.0,
  reason TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  reasoning_effort TEXT NOT NULL DEFAULT '',
  input_hash TEXT NOT NULL,
  reviewed_at TEXT NOT NULL,
  error TEXT,
  PRIMARY KEY(claim_id, topic_id)
);
CREATE INDEX IF NOT EXISTS idx_topic_claim_reviews_topic ON topic_claim_reviews(topic_id, decision);
CREATE TABLE IF NOT EXISTS topic_merge_scan_state (
  singleton_key TEXT PRIMARY KEY CHECK(singleton_key='global'),
  pending_new_visible INTEGER NOT NULL DEFAULT 0,
  request_seq INTEGER NOT NULL DEFAULT 0,
  last_periodic_success_at TEXT,
  next_retry_at TEXT
);
CREATE TABLE IF NOT EXISTS topic_merge_scans (
  scan_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  trigger_kind TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  topic_ids_json TEXT NOT NULL,
  coarse_json TEXT NOT NULL DEFAULT '[]',
  deep_json TEXT NOT NULL DEFAULT '[]',
  merge_ids_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,
  error TEXT
);
"""


class TopicAggregator:
    """Deep incremental topic module.

    Interface invariant: callers pass a batch and its event-time ``batch_time``;
    the module sorts content by event time, commits one SQLite transaction, and
    returns a JSON-serialisable result.  Repeating the same batch is idempotent.
    Archived topics never enter candidate retrieval again.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        extractor: Extractor | None = None,
        resolver: Resolver | None = None,
        brief_writer: Callable[..., dict[str, Any]] | None = None,
        semantic_reviewer: SemanticReviewer | None = None,
        topic_merge_reviewer: TopicMergeReviewer | None = None,
        visible_merge_reviewer: Any | None = None,
        semantic_review_batch_size: int = SEMANTIC_REVIEW_BATCH_SIZE,
        semantic_review_backfill_limit: int = SEMANTIC_REVIEW_BACKFILL_LIMIT,
    ) -> None:
        self.database = str(database)
        # The live dashboard reads the state from separate connections, while
        # shutdown and test harnesses may close the service from a supervisor
        # thread. SQLite remains serialized by the aggregator transaction.
        self.connection = sqlite3.connect(self.database, timeout=30.0, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        # This connection shares the worker SQLite file with the account
        # directory. Match its lock wait and WAL settings so a short console
        # write does not make an aggregation transaction fail after SQLite's
        # default five seconds.
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(SCHEMA)
        self.connection.execute(
            "INSERT OR IGNORE INTO topic_merge_scan_state(singleton_key) VALUES('global')"
        )
        self.connection.commit()
        self._migrate_single_brief_schema()
        self._migrate_brief_status_schema()
        self._migrate_retention_schema()
        self._migrate_topic_evidence()
        self._migrate_topic_claim_reviews()
        self._retrieval_cache: dict[str, dict[str, Any]] = {}
        self._feature_index: defaultdict[str, set[str]] = defaultdict(set)
        self._load_retrieval_cache()
        self.extractor = extractor or self._extract_claims
        self.resolver = resolver
        self.brief_writer = brief_writer
        self.semantic_reviewer = semantic_reviewer
        self.topic_merge_reviewer = topic_merge_reviewer
        self.visible_merge_reviewer = visible_merge_reviewer
        self.semantic_review_batch_size = max(1, int(semantic_review_batch_size))
        self.semantic_review_backfill_limit = max(0, int(semantic_review_backfill_limit))

    def _migrate_single_brief_schema(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(brief_revisions)")}
        if "variant" not in columns:
            return
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE brief_revisions_single (
                  topic_id TEXT NOT NULL REFERENCES topics(topic_id),
                  revision INTEGER NOT NULL,
                  title TEXT NOT NULL,
                  brief TEXT NOT NULL,
                  source_claim_ids_json TEXT NOT NULL,
                  source_bindings_json TEXT NOT NULL,
                  generated_at TEXT NOT NULL,
                  generation_reason TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  PRIMARY KEY(topic_id, revision)
                );
                INSERT OR REPLACE INTO brief_revisions_single
                SELECT topic_id,revision,title,brief,source_claim_ids_json,source_bindings_json,
                       generated_at,generation_reason,content_hash
                FROM brief_revisions
                ORDER BY CASE variant WHEN 'short' THEN 0 ELSE 1 END;
                DROP TABLE brief_revisions;
                ALTER TABLE brief_revisions_single RENAME TO brief_revisions;
                """
            )

    def _migrate_brief_status_schema(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(topics)")}
        with self.connection:
            if "brief_status" not in columns:
                self.connection.execute(
                    "ALTER TABLE topics ADD COLUMN brief_status TEXT NOT NULL DEFAULT 'pending'"
                )
            if "brief_error" not in columns:
                self.connection.execute("ALTER TABLE topics ADD COLUMN brief_error TEXT")
            if "brief_error_at" not in columns:
                self.connection.execute("ALTER TABLE topics ADD COLUMN brief_error_at TEXT")
            if "brief_retry_count" not in columns:
                self.connection.execute(
                    "ALTER TABLE topics ADD COLUMN brief_retry_count INTEGER NOT NULL DEFAULT 0"
                )
            if "brief_retry_after" not in columns:
                self.connection.execute("ALTER TABLE topics ADD COLUMN brief_retry_after TEXT")
            self.connection.execute(
                "UPDATE topics SET brief_status='ready' "
                "WHERE brief_status='pending' AND EXISTS "
                "(SELECT 1 FROM brief_revisions br WHERE br.topic_id=topics.topic_id)"
            )

    def _migrate_retention_schema(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(topics)")}
        with self.connection:
            if "retention_tier" not in columns:
                self.connection.execute(
                    "ALTER TABLE topics ADD COLUMN retention_tier TEXT NOT NULL DEFAULT 'transient'"
                )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_topic_retention ON topics(retention_tier, last_evidence_at)"
            )
            # A published brief proves that this Topic reached the reader-facing
            # hotspot stage before the retention policy existed.
            self.connection.execute(
                "UPDATE topics SET retention_tier='permanent' "
                "WHERE retention_tier='transient' AND (visibility='visible' OR EXISTS "
                "(SELECT 1 FROM brief_revisions br WHERE br.topic_id=topics.topic_id))"
            )

    def _migrate_topic_evidence(self) -> None:
        """Materialize brief citations so context evidence survives retention."""
        with self.connection:
            rows = self.connection.execute(
                "SELECT topic_id,source_claim_ids_json FROM brief_revisions"
            ).fetchall()
            for row in rows:
                for claim_id in json_loads(row["source_claim_ids_json"], []):
                    self.connection.execute(
                        "INSERT OR IGNORE INTO topic_evidence(topic_id,claim_id) "
                        "SELECT ?,claim_id FROM claims WHERE claim_id=?",
                        (row["topic_id"], str(claim_id)),
                    )

    def _migrate_topic_claim_reviews(self) -> None:
        """Keep semantic review records compatible with the first rollout."""
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(topic_claim_reviews)")}
        if not columns:
            return
        with self.connection:
            if "confidence" not in columns:
                self.connection.execute(
                    "ALTER TABLE topic_claim_reviews ADD COLUMN confidence REAL NOT NULL DEFAULT 0.0"
                )
            if "input_hash" not in columns:
                if "input_fingerprint" in columns:
                    self.connection.execute(
                        "ALTER TABLE topic_claim_reviews RENAME COLUMN input_fingerprint TO input_hash"
                    )
                else:
                    self.connection.execute(
                        "ALTER TABLE topic_claim_reviews ADD COLUMN input_hash TEXT NOT NULL DEFAULT ''"
                    )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_topic_claim_reviews_topic "
                "ON topic_claim_reviews(topic_id, decision)"
            )

    def close(self) -> None:
        self.connection.close()

    def prune_transient_state(
        self,
        at: datetime | str,
        *,
        retention_hours: int = TRANSIENT_RETENTION_HOURS,
    ) -> dict[str, int]:
        """Discard expired non-hotspot material while preserving permanent evidence.

        A Topic becomes permanent as soon as it meets the active-topic
        qualification, independently of whether a later model request writes a
        brief.  Only archived transient Topics and content with no permanent
        Topic membership are eligible for removal.
        """
        if retention_hours < 24:
            raise ValueError("transient retention must be at least 24 hours")
        cutoff = (dt(iso(at)) - timedelta(hours=retention_hours)).isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            topic_ids = [
                row["topic_id"]
                for row in self.connection.execute(
                    "SELECT topic_id FROM topics WHERE retention_tier='transient' "
                    "AND matching_status='archived' "
                    "AND COALESCE(last_evidence_at, started_at) < ?",
                    (cutoff,),
                )
            ]
            content_ids = [
                row["content_item_id"]
                for row in self.connection.execute(
                    "SELECT ci.content_item_id FROM content_items ci WHERE ci.created_at < ? "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM claims cl JOIN memberships m ON m.claim_id=cl.claim_id "
                    "JOIN topics t ON t.topic_id=m.topic_id "
                    "WHERE cl.content_item_id=ci.content_item_id "
                    "AND m.superseded_by IS NULL AND t.retention_tier='permanent') "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM claims cl JOIN topic_evidence te ON te.claim_id=cl.claim_id "
                    "JOIN topics t ON t.topic_id=te.topic_id "
                    "WHERE cl.content_item_id=ci.content_item_id AND t.retention_tier='permanent')",
                    (cutoff,),
                )
            ]
            claim_ids = [
                row["claim_id"]
                for row in self.connection.execute(
                    "SELECT claim_id FROM claims WHERE content_item_id IN "
                    f"({','.join('?' for _ in content_ids)})",
                    content_ids,
                )
            ] if content_ids else []
            thread_ids = [
                row["thread_id"]
                for row in self.connection.execute(
                    "SELECT thread_id FROM threads WHERE topic_id IN "
                    f"({','.join('?' for _ in topic_ids)})",
                    topic_ids,
                )
            ] if topic_ids else []

            if claim_ids:
                self.connection.execute(
                    "DELETE FROM decision_audit WHERE claim_id IN "
                    f"({','.join('?' for _ in claim_ids)})",
                    claim_ids,
                )
            if topic_ids:
                self.connection.execute(
                    "DELETE FROM topic_pair_reviews WHERE left_topic_id IN "
                    f"({','.join('?' for _ in topic_ids)}) OR right_topic_id IN "
                    f"({','.join('?' for _ in topic_ids)})",
                    [*topic_ids, *topic_ids],
                )
                # SQLite limits expression-tree depth, so large historical
                # cleanups cannot combine every Topic ID into one OR clause.
                for start in range(0, len(topic_ids), 100):
                    topic_like = [f'%{topic_id}%' for topic_id in topic_ids[start:start + 100]]
                    self.connection.execute(
                        "DELETE FROM decision_audit WHERE created_at < ? AND (" + " OR ".join(
                            "topic_ids_json LIKE ?" for _ in topic_like
                        ) + ")",
                        [cutoff, *topic_like],
                    )
                self.connection.execute(
                    "DELETE FROM topic_merges WHERE source_topic_id IN "
                    f"({','.join('?' for _ in topic_ids)}) OR target_topic_id IN "
                    f"({','.join('?' for _ in topic_ids)})",
                    [*topic_ids, *topic_ids],
                )
                self.connection.execute(
                    "DELETE FROM retrieval_state WHERE scope_type='topic' AND scope_id IN "
                    f"({','.join('?' for _ in topic_ids)})",
                    topic_ids,
                )
                if thread_ids:
                    self.connection.execute(
                        "DELETE FROM retrieval_state WHERE scope_type='thread' AND scope_id IN "
                        f"({','.join('?' for _ in thread_ids)})",
                        thread_ids,
                    )
                self.connection.execute(
                    "DELETE FROM topic_evidence WHERE topic_id IN "
                    f"({','.join('?' for _ in topic_ids)})",
                    topic_ids,
                )
                self.connection.execute(
                    "DELETE FROM topic_claim_reviews WHERE topic_id IN "
                    f"({','.join('?' for _ in topic_ids)})",
                    topic_ids,
                )
                self.connection.execute(
                    "DELETE FROM topic_participations WHERE topic_id IN "
                    f"({','.join('?' for _ in topic_ids)})",
                    topic_ids,
                )
                self.connection.execute(
                    "DELETE FROM memberships WHERE topic_id IN "
                    f"({','.join('?' for _ in topic_ids)})",
                    topic_ids,
                )
                self.connection.execute(
                    "DELETE FROM threads WHERE topic_id IN "
                    f"({','.join('?' for _ in topic_ids)})",
                    topic_ids,
                )
                self.connection.execute(
                    "DELETE FROM topics WHERE topic_id IN "
                    f"({','.join('?' for _ in topic_ids)})",
                    topic_ids,
                )
            if claim_ids:
                self.connection.execute(
                    "DELETE FROM topic_claim_reviews WHERE claim_id IN "
                    f"({','.join('?' for _ in claim_ids)})",
                    claim_ids,
                )
                self.connection.execute(
                    "DELETE FROM memberships WHERE claim_id IN "
                    f"({','.join('?' for _ in claim_ids)})",
                    claim_ids,
                )
                self.connection.execute(
                    "DELETE FROM claims WHERE claim_id IN "
                    f"({','.join('?' for _ in claim_ids)})",
                    claim_ids,
                )
            if content_ids:
                self.connection.execute(
                    "DELETE FROM content_items WHERE content_item_id IN "
                    f"({','.join('?' for _ in content_ids)})",
                    content_ids,
                )
            self.connection.execute("DELETE FROM batches WHERE batch_time < ?", (cutoff,))
            self.connection.execute(
                "DELETE FROM decision_audit WHERE created_at < ? AND (claim_id IS NULL OR NOT EXISTS ("
                "SELECT 1 FROM memberships m JOIN topics t ON t.topic_id=m.topic_id "
                "WHERE m.claim_id=decision_audit.claim_id AND m.superseded_by IS NULL "
                "AND t.retention_tier='permanent'))",
                (cutoff,),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        self._reload_retrieval_cache()
        return {
            "deleted_topics": len(topic_ids),
            "deleted_content_items": len(content_ids),
            "deleted_claims": len(claim_ids),
        }

    def __enter__(self) -> "TopicAggregator":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def process_batch(
        self,
        content_items: Iterable[ContentItem | dict[str, Any]],
        batch_time: datetime | str,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        batch_iso = iso(batch_time)
        normalized = [item for value in content_items if (item := ContentItem.from_any(value)) is not None]
        normalized.sort(key=lambda item: (item.created_at, item.tweet_id))
        batch_id = stable_id("batch", batch_iso, *(item.tweet_id for item in normalized))

        existing = self.connection.execute("SELECT result_json FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if existing:
            result = json_loads(existing["result_json"], {})
            result["idempotent_replay"] = True
            return result

        metrics: dict[str, Any] = {
            "batch_id": batch_id,
            "batch_time": batch_iso,
            "input_count": len(normalized),
            "deduplicated_count": 0,
            "claim_count": 0,
            "candidate_count": 0,
            "model_candidate_count": 0,
            "attach_count": 0,
            "create_seed_count": 0,
            "defer_count": 0,
            "ignore_count": 0,
            "affected_topic_ids": [],
            "brief_refresh_requests": [],
            "topic_merges": [],
            "semantic_review": {"enabled": bool(self.semantic_reviewer), "requested": 0, "support": 0, "context": 0, "unrelated": 0, "fallback": 0, "model_calls": 0},
            "errors": [],
            "token_usage": {"input": 0, "output": 0, "cached": 0, "total": 0},
        }
        metrics["state_before"] = {
            status: self.connection.execute("SELECT COUNT(*) FROM topics WHERE matching_status=?", (status,)).fetchone()[0]
            for status in ("seed", "active", "archived")
        }
        decisions: list[dict[str, Any]] = []
        affected: set[str] = set()
        phase_started = time.perf_counter()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for item in normalized:
                row = self.connection.execute(
                    "SELECT content_item_id FROM content_items WHERE tweet_id=?", (item.tweet_id,)
                ).fetchone()
                if row:
                    metrics["deduplicated_count"] += 1
                    decisions.append({"tweet_id": item.tweet_id, "action": "duplicate", "topic_ids": []})
                    continue
                fingerprint = hashlib.sha256((item.expanded_text + "\x1f" + item.activity_account).encode()).hexdigest()
                self.connection.execute(
                    "INSERT INTO content_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item.content_item_id, item.tweet_id, item.activity_account, item.author,
                        item.activity_type, item.content_text, item.expanded_text, item.created_at,
                        item.source_url, json_dumps(item.metrics), json_dumps(item.references),
                        json_dumps(item.raw_payload), fingerprint,
                    ),
                )
                claims = list(self.extractor(item))
                metrics["claim_count"] += len(claims)
                if not claims:
                    metrics["ignore_count"] += 1
                    decisions.append({"tweet_id": item.tweet_id, "action": "ignore", "topic_ids": [], "reason": "no_claim"})
                    continue
                item_topics: set[str] = set()
                for claim in claims:
                    self._insert_claim(claim, item.created_at)
                    candidates = self._retrieve_candidates(claim, item, batch_iso)
                    metrics["candidate_count"] += len(candidates)
                    action, topic_ids, reason, source = self._decide(claim, candidates)
                    if action == "attach":
                        metrics["attach_count"] += 1
                        for topic_id in topic_ids:
                            self._attach_claim(
                                claim, topic_id, candidates, reason, source, item, batch_iso,
                                defer_participation=False,
                            )
                            affected.add(topic_id)
                            item_topics.add(topic_id)
                    elif action == "create_seed":
                        metrics["create_seed_count"] += 1
                        topic_id = self._create_seed(claim, item, batch_iso)
                        self._attach_claim(
                            claim, topic_id, [], reason, source, item, batch_iso,
                            defer_participation=False,
                        )
                        if self.semantic_reviewer:
                            self._store_topic_claim_review(
                                claim.claim_id, topic_id, "support", "seed claim is the topic's initial evidence",
                                confidence=1.0, model="hard_rule", reasoning_effort="", input_hash=stable_id("review-input", claim.claim_id, topic_id),
                                reviewed_at=batch_iso,
                            )
                        affected.add(topic_id)
                        item_topics.add(topic_id)
                    elif action == "defer":
                        metrics["defer_count"] += 1
                    else:
                        metrics["ignore_count"] += 1
                    decisions.append({
                        "tweet_id": item.tweet_id,
                        "content_item_id": item.content_item_id,
                        "claim_id": claim.claim_id,
                        "action": action,
                        "topic_ids": sorted(topic_ids),
                        "candidates": [candidate.as_dict() for candidate in candidates],
                        "reason": reason,
                        "decision_source": source,
                    })

                if item_topics:
                    metrics.setdefault("item_topic_ids", {})[item.tweet_id] = sorted(item_topics)

            merge_requests = self._consolidate_dirty_topics(affected, batch_iso) if not (self.topic_merge_reviewer or self.visible_merge_reviewer) else []
            if self.topic_merge_reviewer and not self.visible_merge_reviewer:
                self.connection.commit()
                merge_requests.extend(self._review_topic_pairs(affected, batch_iso))
                self.connection.execute("BEGIN IMMEDIATE")
            metrics["topic_merges"] = merge_requests
            for request in merge_requests:
                affected.add(request["source_topic_id"])
                affected.add(request["target_topic_id"])
            status_requests = self._refresh_all_topics(batch_iso)
            affected.update(status_requests["affected_topic_ids"])
            # An active topic with no successful brief remains a real topic,
            # not a missing topic.  Include it on every later batch so a
            # transient model outage can be retried even when the next batch
            # contains no new claim for that topic.
            pending_briefs = {
                row["topic_id"]
                for row in self.connection.execute(
                    "SELECT t.topic_id FROM topics t "
                    "WHERE t.matching_status='active' "
                    "AND (t.brief_status='pending' OR t.brief_retry_after IS NOT NULL)"
                ).fetchall()
            }
            brief_topic_ids = affected | pending_briefs
            # Brief generation can make network calls and retries.  Persist
            # the deterministic topic state before it starts so it never owns
            # SQLite's single writer while waiting on a remote model.
            metrics["phase_ms"] = {"state_commit": round((time.perf_counter() - phase_started) * 1000, 2)}
            self.connection.commit()
            brief_requests = self._refresh_briefs(brief_topic_ids, batch_iso, status_requests["transitions"])
            newly_visible = {
                request["topic_id"]
                for request in brief_requests
                if request.get("brief_status") == "ready"
                and request.get("visibility") == "visible"
                and request.get("reason") in {"became_visible", "visibility_or_lifecycle_change"}
            }
            if newly_visible and self.visible_merge_reviewer:
                self.queue_visible_merge_scan()
            elif newly_visible:
                visible_merges = self._merge_visible_topics(newly_visible, batch_iso)
                for request in visible_merges:
                    affected.add(request["source_topic_id"])
                    affected.add(request["target_topic_id"])
                if visible_merges:
                    merge_targets = {request["target_topic_id"] for request in visible_merges}
                    brief_requests.extend(
                        self._refresh_briefs(
                            merge_targets,
                            batch_iso,
                            {},
                            force_topic_ids=merge_targets,
                        )
                    )
                metrics["topic_merges"].extend(visible_merges)
            metrics["affected_topic_ids"] = sorted(affected)
            metrics["brief_refresh_requests"] = brief_requests
            metrics["brief_deferred_count"] = sum(
                request.get("brief_status") == "deferred" for request in brief_requests
            )
            metrics["errors"].extend(
                {
                    "topic_id": request["topic_id"],
                    "error_code": request["error_code"],
                    "error_message": request["error_message"],
                    "retryable": request["retryable"],
                }
                for request in brief_requests
                if request.get("brief_status") == "error"
            )
            for decision in decisions:
                self.connection.execute(
                    "INSERT OR IGNORE INTO decision_audit VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        stable_id("decision", batch_id, decision.get("claim_id"), decision.get("tweet_id"), decision.get("action")),
                        batch_id, decision.get("content_item_id"), decision.get("claim_id"), decision.get("action", "unknown"),
                        json_dumps(decision.get("topic_ids", [])), json_dumps(decision.get("candidates", [])),
                        decision.get("reason", ""), decision.get("decision_source", "system"), batch_iso,
                    ),
                )
            result = {"metrics": metrics, "decisions": decisions, "idempotent_replay": False}
            self.connection.execute(
                "INSERT INTO batches VALUES (?,?,?,?,?)",
                (batch_id, batch_iso, len(normalized), iso(UTC_NOW()), json_dumps(result)),
            )
            self.connection.commit()
            metrics["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            return result
        except Exception:
            self.connection.rollback()
            # Retrieval state is updated while the transaction is open.  A
            # rollback removes those rows from SQLite, so discard any cache
            # entries created during the failed attempt before retrying.
            self._reload_retrieval_cache()
            raise

    def reconcile_recent_topics(self, at: datetime | str) -> dict[str, Any]:
        """Reapply topic merge identity to every visible open topic."""
        batch_iso = iso(at)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            topic_ids = {
                row["topic_id"]
                for row in self.connection.execute(
                    "SELECT topic_id FROM topics "
                    "WHERE matching_status IN ('seed','active') AND visibility='visible'"
                )
            }
            affected = set(topic_ids)
            if self.semantic_reviewer:
                review_topic_ids = {
                    row["topic_id"] for row in self.connection.execute(
                        "SELECT topic_id FROM topics WHERE matching_status IN ('seed','active')"
                    )
                }
                review_targets = self._unreviewed_review_targets(review_topic_ids, limit=self.semantic_review_backfill_limit)
                self.connection.commit()
                review_result = self._review_memberships(review_targets, batch_iso)
                self.connection.execute("BEGIN IMMEDIATE")
                reviewed_topic_ids = {topic_id for _, topic_id in review_targets}
                self._rebuild_topic_participations(reviewed_topic_ids, batch_iso)
                topic_ids.update(reviewed_topic_ids)
                affected.update(reviewed_topic_ids)
            else:
                review_result = {
                    "enabled": False, "requested": 0, "support": 0, "context": 0,
                    "unrelated": 0, "fallback": 0, "model_calls": 0,
                }
            merge_requests = self._consolidate_dirty_topics(topic_ids, batch_iso) if not (self.topic_merge_reviewer or self.visible_merge_reviewer) else []
            if self.topic_merge_reviewer and not self.visible_merge_reviewer:
                self.connection.commit()
                merge_requests.extend(self._review_topic_pairs(topic_ids, batch_iso))
                self.connection.execute("BEGIN IMMEDIATE")
            for request in merge_requests:
                affected.add(request["source_topic_id"])
                affected.add(request["target_topic_id"])
            status_requests = self._refresh_all_topics(batch_iso)
            affected.update(status_requests["affected_topic_ids"])
            pending_briefs = {
                row["topic_id"]
                for row in self.connection.execute(
                    "SELECT topic_id FROM topics WHERE matching_status='active' "
                    "AND (brief_status='pending' OR brief_retry_after IS NOT NULL)"
                )
            }
            self.connection.commit()
            brief_requests = self._refresh_briefs(affected | pending_briefs, batch_iso, status_requests["transitions"])
            newly_visible = {
                request["topic_id"]
                for request in brief_requests
                if request.get("brief_status") == "ready"
                and request.get("visibility") == "visible"
                and request.get("reason") in {"became_visible", "visibility_or_lifecycle_change"}
            }
            if newly_visible and self.visible_merge_reviewer:
                self.queue_visible_merge_scan()
            elif newly_visible:
                visible_merges = self._merge_visible_topics(newly_visible, batch_iso)
                for request in visible_merges:
                    affected.add(request["source_topic_id"])
                    affected.add(request["target_topic_id"])
                if visible_merges:
                    merge_targets = {request["target_topic_id"] for request in visible_merges}
                    brief_requests.extend(
                        self._refresh_briefs(
                            merge_targets,
                            batch_iso,
                            {},
                            force_topic_ids=merge_targets,
                        )
                    )
                merge_requests.extend(visible_merges)
            pending_reviews = self._count_pending_claim_reviews()
            return {"topic_merges": merge_requests, "brief_refresh_requests": brief_requests, "semantic_review": {**review_result, "pending": pending_reviews}}
        except Exception:
            self.connection.rollback()
            raise

    def _merge_visible_topics(self, topic_ids: set[str], at: str) -> list[dict[str, str]]:
        """Run the configured merge path after topics become visible."""
        self.connection.commit()
        requests = (
            self._review_topic_pairs(topic_ids, at)
            if self.topic_merge_reviewer
            else self._consolidate_dirty_topics(topic_ids, at)
        )
        self.connection.commit()
        return requests

    def queue_visible_merge_scan(self) -> None:
        self.connection.execute(
            "UPDATE topic_merge_scan_state SET pending_new_visible=1,request_seq=request_seq+1 WHERE singleton_key='global'"
        )
        self.connection.commit()

    def visible_merge_scan_due(self, at: datetime | str, *, force: bool = False) -> str | None:
        if not self.visible_merge_reviewer:
            return None
        row = self.connection.execute(
            "SELECT * FROM topic_merge_scan_state WHERE singleton_key='global'"
        ).fetchone()
        current = dt(iso(at))
        if not force and row["next_retry_at"] and current < dt(row["next_retry_at"]):
            return None
        periodic = not row["last_periodic_success_at"] or current - dt(row["last_periodic_success_at"]) >= timedelta(hours=4)
        if force or periodic:
            return "periodic"
        return "new_visible" if row["pending_new_visible"] else None

    def _visible_merge_cards(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT t.topic_id,t.first_seen_at,b.revision,b.title,b.brief,b.content_hash,b.source_claim_ids_json "
            "FROM topics t JOIN brief_revisions b ON b.topic_id=t.topic_id "
            "AND b.revision=(SELECT MAX(x.revision) FROM brief_revisions x WHERE x.topic_id=t.topic_id) "
            "WHERE t.matching_status='active' AND t.visibility='visible' AND t.brief_status='ready' "
            "ORDER BY t.hotness_score DESC,t.last_evidence_at DESC,t.topic_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def _visible_merge_evidence(self, topic_id: str) -> list[dict[str, str]]:
        brief = self.connection.execute(
            "SELECT source_claim_ids_json FROM brief_revisions WHERE topic_id=? ORDER BY revision DESC LIMIT 1",
            (topic_id,),
        ).fetchone()
        cited = set(json.loads(brief[0])) if brief else set()
        rows = self.connection.execute(
            "SELECT cl.claim_id,cl.claim_text,ci.activity_account FROM memberships m "
            "JOIN claims cl ON cl.claim_id=m.claim_id "
            "JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
            "LEFT JOIN topic_claim_reviews r ON r.claim_id=cl.claim_id AND r.topic_id=m.topic_id "
            "WHERE m.topic_id=? AND m.superseded_by IS NULL AND COALESCE(r.decision,'support')='support' "
            "GROUP BY cl.claim_id ORDER BY ci.created_at DESC",
            (topic_id,),
        ).fetchall()
        evidence = [{"claim_id": row["claim_id"], "text": row["claim_text"], "account": row["activity_account"]} for row in rows]
        return sorted(evidence, key=lambda row: row["claim_id"] not in cited)[:15]

    @staticmethod
    def _combine_merge_groups(groups: Sequence[dict[str, Any]], valid_ids: set[str]) -> list[list[str]]:
        components: list[set[str]] = []
        for group in groups:
            raw_ids = group.get("ids")
            if not isinstance(raw_ids, list) or any(not isinstance(value, str) or value not in valid_ids for value in raw_ids):
                raise ValueError("coarse review returned an unknown topic")
            ids = set(raw_ids)
            if len(ids) < 2:
                continue
            overlapping = [component for component in components if component & ids]
            for component in overlapping:
                ids.update(component)
                components.remove(component)
            components.append(ids)
        return [sorted(component) for component in sorted(components, key=lambda value: sorted(value)[0])]

    def scan_visible_topics(self, at: datetime | str, *, force: bool = False) -> dict[str, Any] | None:
        trigger = self.visible_merge_scan_due(at, force=force)
        if not trigger:
            return None
        scan_at = iso(at)
        request_seq = self.connection.execute(
            "SELECT request_seq FROM topic_merge_scan_state WHERE singleton_key='global'"
        ).fetchone()[0]
        cards = self._visible_merge_cards()
        evidence_by_id = {row["topic_id"]: self._visible_merge_evidence(row["topic_id"]) for row in cards}
        snapshot = {
            row["topic_id"]: (row["revision"], row["content_hash"], hashlib.sha256(json_dumps(evidence_by_id[row["topic_id"]]).encode()).hexdigest())
            for row in cards
        }
        input_hash = hashlib.sha256(json_dumps(snapshot).encode()).hexdigest()
        scan_id = stable_id("topic-merge-scan", scan_at, input_hash)
        self.connection.execute(
            "INSERT INTO topic_merge_scans(scan_id,started_at,trigger_kind,input_hash,topic_ids_json,status) "
            "VALUES(?,?,?,?,?,'running')",
            (scan_id, scan_at, trigger, input_hash, json_dumps(sorted(snapshot))),
        )
        self.connection.commit()
        coarse: list[dict[str, Any]] = []
        deep: list[dict[str, Any]] = []
        merges: list[dict[str, str]] = []
        try:
            if len(cards) >= 2:
                coarse = list(self.visible_merge_reviewer.propose([
                    {"id": row["topic_id"], "title": row["title"], "summary": row["brief"][:700]}
                    for row in cards
                ]))
                groups = self._combine_merge_groups(coarse, set(snapshot))
                by_id = {row["topic_id"]: row for row in cards}
                for ids in groups:
                    case = [{
                        "id": topic_id, "title": by_id[topic_id]["title"], "brief": by_id[topic_id]["brief"],
                        "evidence": evidence_by_id[topic_id],
                    } for topic_id in ids]
                    decision = dict(self.visible_merge_reviewer.review(case))
                    decision["ids"] = ids
                    deep.append(decision)
                    if decision.get("decision") != "merge" or float(decision.get("confidence") or 0) < 0.8:
                        continue
                    evidence = decision.get("evidence_claim_ids")
                    if not isinstance(evidence, dict) or any(
                        not isinstance(evidence.get(topic_id), list)
                        or not set(evidence[topic_id]) & {row["claim_id"] for row in topic["evidence"]}
                        for topic_id, topic in zip(ids, case)
                    ) or not compact(decision.get("shared_fact")):
                        raise ValueError("visible topic merge review lacks evidence for every topic")
                    self.connection.execute("BEGIN IMMEDIATE")
                    current = {
                        row["topic_id"]: (row["revision"], row["content_hash"], hashlib.sha256(json_dumps(self._visible_merge_evidence(row["topic_id"])).encode()).hexdigest())
                        for row in self._visible_merge_cards()
                    }
                    if any(current.get(topic_id) != snapshot[topic_id] for topic_id in ids):
                        self.connection.rollback()
                        raise RuntimeError("visible topic changed during merge review")
                    target_id = min(ids, key=lambda topic_id: (by_id[topic_id]["first_seen_at"], topic_id))
                    cited_ids = {
                        claim_id for topic_id in ids
                        for claim_id in json_loads(by_id[topic_id]["source_claim_ids_json"], [])
                    }
                    cited_ids.update(
                        claim_id for values in evidence.values() for claim_id in values
                    )
                    for source_id in ids:
                        if source_id != target_id:
                            merges.append(self._merge_topic(source_id, target_id, scan_at, f"editorial: {compact(decision.get('reason'))}", preserve_identity=True))
                    self._refresh_all_topics(scan_at)
                    self.connection.commit()
                    brief_requests = self._refresh_briefs(
                        {target_id}, scan_at, {}, force_topic_ids={target_id},
                        evidence_claim_ids=cited_ids, editorial_guidance=decision,
                    )
                    if any(request.get("brief_status") == "error" for request in brief_requests):
                        raise RuntimeError("merged topic brief refresh failed")
            self.connection.execute(
                "UPDATE topic_merge_scans SET completed_at=?,coarse_json=?,deep_json=?,merge_ids_json=?,status='succeeded' WHERE scan_id=?",
                (iso(UTC_NOW()), json_dumps(coarse), json_dumps(deep), json_dumps([row["merge_id"] for row in merges]), scan_id),
            )
            self.connection.execute(
                "UPDATE topic_merge_scan_state SET pending_new_visible=CASE WHEN request_seq=? THEN 0 ELSE pending_new_visible END,next_retry_at=NULL,"
                "last_periodic_success_at=CASE WHEN ?='periodic' THEN ? ELSE last_periodic_success_at END "
                "WHERE singleton_key='global'",
                (request_seq, trigger, scan_at),
            )
            self.connection.commit()
            return {"scan_id": scan_id, "topics": len(cards), "groups": len(deep), "merges": merges}
        except Exception as exc:
            self.connection.rollback()
            retry_at = iso(dt(scan_at) + timedelta(minutes=15))
            self.connection.execute(
                "UPDATE topic_merge_scans SET completed_at=?,coarse_json=?,deep_json=?,merge_ids_json=?,status='failed',error=? WHERE scan_id=?",
                (iso(UTC_NOW()), json_dumps(coarse), json_dumps(deep), json_dumps([row["merge_id"] for row in merges]),
                 f"{type(exc).__name__}: {exc}"[:1000], scan_id),
            )
            self.connection.execute(
                "UPDATE topic_merge_scan_state SET next_retry_at=? WHERE singleton_key='global'", (retry_at,)
            )
            self.connection.commit()
            return {"scan_id": scan_id, "topics": len(cards), "groups": len(deep), "merges": merges, "error": str(exc), "retry_at": retry_at}

    def repair_visible_merge_brief(self, merge_id: str, at: datetime | str) -> dict[str, Any]:
        merge = self.connection.execute(
            "SELECT * FROM topic_merges WHERE merge_id=? AND reason LIKE 'editorial:%'", (merge_id,)
        ).fetchone()
        if not merge:
            raise ValueError("editorial merge not found")
        target_id, source_id = merge["target_topic_id"], merge["source_topic_id"]
        topic = self.connection.execute("SELECT matching_status FROM topics WHERE topic_id=?", (target_id,)).fetchone()
        if not topic or topic["matching_status"] != "active":
            raise ValueError("editorial merge target is no longer active")
        prior = {}
        for topic_id in (target_id, source_id):
            row = self.connection.execute(
                "SELECT title,source_claim_ids_json FROM brief_revisions "
                "WHERE topic_id=? AND generated_at<? ORDER BY revision DESC LIMIT 1",
                (topic_id, merge["created_at"]),
            ).fetchone()
            if not row:
                raise ValueError("pre-merge brief missing")
            prior[topic_id] = row
        scan = self.connection.execute(
            "SELECT deep_json FROM topic_merge_scans WHERE merge_ids_json LIKE ? ORDER BY started_at DESC LIMIT 1",
            (f'%"{merge_id}"%',),
        ).fetchone()
        if not scan:
            raise ValueError("editorial review audit missing")
        decisions = json_loads(scan["deep_json"], [])
        decision = next((row for row in decisions if target_id in row.get("ids", []) and source_id in row.get("ids", [])), None)
        if not decision or decision.get("decision") != "merge":
            raise ValueError("editorial merge review missing")
        cited_ids = {
            claim_id for row in prior.values()
            for claim_id in json_loads(row["source_claim_ids_json"], [])
        }
        cited_ids.update(
            claim_id for values in decision["evidence_claim_ids"].values() for claim_id in values
        )
        title = prior[target_id]["title"]
        self.connection.execute(
            "UPDATE topics SET working_title=?,canonical_subject=? WHERE topic_id=?",
            (title, title, target_id),
        )
        self.connection.commit()
        requests = self._refresh_briefs(
            {target_id}, iso(at), {}, force_topic_ids={target_id},
            evidence_claim_ids=cited_ids, editorial_guidance=decision,
        )
        if not requests or requests[0].get("brief_status") != "ready":
            raise RuntimeError("editorial merge brief repair failed")
        return requests[0]

    def _extract_claims(self, item: ContentItem) -> Sequence[Claim]:
        text = item.expanded_text
        pieces = [compact(piece) for piece in re.split(r"\n+|(?<=[。！？!?])\s+|(?<=[.!?])\s+(?=[A-Z$@])", text)]
        pieces = [piece for piece in pieces if len(piece) >= 4]
        if not pieces:
            pieces = [text]
        # Avoid generating dozens of tiny claims from long quoted threads while
        # still preserving the one-content-to-many-claims relationship.
        pieces = pieces[:8]
        claims: list[Claim] = []
        for index, piece in enumerate(pieces):
            lower = piece.lower()
            kind = self._claim_kind(lower)
            entity_values = sorted(set(RE_CASHTAG.findall(piece) + RE_MENTION.findall(piece)))
            entity_values += sorted({value for value in tokens(piece) if value.startswith("$") or value.startswith("@")})
            if not entity_values:
                entity_values = sorted(tokens(piece))[:8]
            action = self._action_or_issue(lower)
            stance = "negative" if any(word in lower for word in ("fuck", "clown", "垃圾", "傻", "骂", "离开", "dead", "反对")) else "neutral"
            information_value = 0.25 if kind == "reaction" else 0.7
            claims.append(Claim(
                claim_id=stable_id("claim", item.content_item_id, index),
                content_item_id=item.content_item_id,
                claim_text=piece,
                claim_kind=kind,
                entities=tuple(dict.fromkeys(entity_values)),
                action_or_issue=action,
                stance=stance,
                confidence=0.7 if len(piece) >= 12 else 0.45,
                information_value=information_value,
                evidence_span=piece,
            ))
        return claims

    @staticmethod
    def _claim_kind(text: str) -> str:
        if any(word in text for word in ("可能", "大概率", "担心", "将会", "以后", "未来", "will ", "could ", "might ")):
            return "prediction"
        if any(word in text for word in ("认为", "觉得", "看来", "说明", "证明", "建议", "think", "believe", "should")):
            return "interpretation"
        if any(word in text for word in ("广告", "购买", "买入", "mint", "airdrop", "launch your", "contract")):
            return "promotion"
        if any(word in text for word in (
            "傻", "垃圾", "fuck", "clown", "哈哈", "lol", "笑死", "cooked", "dead", "disagree", "反对",
            "卖飞", "踏空", "后悔", "没在车上", "佩服", "谁在车上", "打螺丝", "穿云箭",
        )):
            return "reaction"
        if any(word in text for word in ("官方", "宣布", "发布", "上线", "confirmed", "launched", "announced")):
            return "official_statement"
        return "reported_fact"

    @staticmethod
    def _action_or_issue(text: str) -> str:
        terms = (
            ("withdraw", "停止/关闭"), ("关闭", "停止/关闭"), ("上线", "上线/开放"),
            ("open", "上线/开放"), ("launch", "发布/上线"), ("发布", "发布/上线"),
            ("换回", "头像变化"), ("pfp", "头像变化"), ("头像", "头像变化"),
            ("跌", "市场波动"), ("涨", "市场波动"), ("拉盘", "市场波动"), ("爆拉", "市场波动"),
            ("爆仓", "市场波动"), ("市值", "市场波动"), ("交易量", "市场波动"), ("吸筹", "市场分析"), ("合约", "市场分析"),
            ("drop", "市场波动"), ("price", "市场波动"),
            ("ai", "AI/模型"), ("memory", "内存/算力"), ("chip", "芯片/算力"),
            ("agent", "AI代理"), ("基地", "生态争议"), ("base", "生态争议"),
        )
        found = [label for needle, label in terms if needle in text]
        return ",".join(dict.fromkeys(found)) or "一般讨论"

    def _insert_claim(self, claim: Claim, created_at: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                claim.claim_id, claim.content_item_id, claim.claim_text, claim.claim_kind,
                json_dumps(list(claim.entities)), claim.action_or_issue, claim.stance,
                claim.evidence_span, claim.confidence, claim.information_value, created_at,
            ),
        )

    def _retrieve_candidates(self, claim: Claim, item: ContentItem, at: str) -> list[Candidate]:
        claim_tokens = tokens(claim.claim_text)
        claim_keys = hard_keys(item, claim.claim_text)
        # A long original can be split into several claims.  A later claim
        # may contain the subject cashtag while the first claim contains the
        # mechanism or event description.  Use the full content as recall
        # context, but only promote that context to a hard relation when the
        # claim itself also shares lexical/entity evidence with the topic.
        item_keys = hard_keys(item, item.expanded_text)
        item_keys.add(f"content:{item.tweet_id}")
        claim_entities = set(claim.entities)
        candidate_ids: set[str] = set()
        for feature in item_keys:
            candidate_ids.update(self._feature_index.get(f"k:{feature}", set()))
        for feature in claim_entities:
            candidate_ids.update(self._feature_index.get(f"e:{feature}", set()))
        for feature in claim_tokens:
            candidate_ids.update(self._feature_index.get(f"t:{feature}", set()))
        # No exact feature means there is no useful local neighbourhood.  Do
        # not compare against every historical topic; it is a new-seed case.
        rows = [self._retrieval_cache[topic_id] for topic_id in candidate_ids if topic_id in self._retrieval_cache]
        candidates: list[Candidate] = []
        claim_time = dt(item.created_at)
        for row in rows:
            if row["matching_status"] not in {"seed", "active"}:
                continue
            old_tokens = row["tokens"]
            old_keys = row["hard_keys"]
            old_entities = row["entities"]
            entity_overlap = len(claim_entities & old_entities) / max(1, len(claim_entities | old_entities))
            semantic = len(claim_tokens & old_tokens) / max(1, len(claim_tokens | old_tokens))
            direct_hard = bool(claim_keys & old_keys)
            content_hard = bool(item_keys & old_keys) and bool(claim_tokens & old_tokens or claim_entities & old_entities)
            hard = 1.0 if direct_hard or content_hard else 0.0
            thread_match = 0.0
            if claim.action_or_issue and claim.action_or_issue in row["event_or_issue"]:
                thread_match = 1.0
            last = row["last_evidence_at"] or row["started_at"]
            hours = abs((claim_time - dt(last)).total_seconds()) / 3600
            proximity = max(0.0, 1.0 - min(hours, 24.0) / 24.0)
            score = 0.64 * hard + 0.22 * entity_overlap + 0.34 * semantic + 0.08 * thread_match + 0.04 * proximity
            if score <= 0:
                continue
            reason_parts = []
            if hard:
                reason_parts.append("hard_relation")
            if entity_overlap:
                reason_parts.append(f"entity={entity_overlap:.2f}")
            if semantic:
                reason_parts.append(f"semantic={semantic:.2f}")
            candidates.append(Candidate(row["topic_id"], round(score, 4), hard, round(entity_overlap, 4), round(semantic, 4), thread_match, round(proximity, 4), ",".join(reason_parts)))
        candidates.sort(key=lambda candidate: (-candidate.score, candidate.topic_id))
        return candidates[:60]

    def _decide(self, claim: Claim, candidates: Sequence[Candidate]) -> tuple[str, set[str], str, str]:
        if not candidates:
            return "create_seed", set(), "no active candidate", "hard_rule"
        best = candidates[0]
        second = candidates[1] if len(candidates) > 1 else None
        margin = best.score - second.score if second else best.score
        if self.resolver and (second and margin < 0.12 or best.score < 0.55):
            resolved = self.resolver(claim, candidates[:10], self.connection)
            if resolved:
                action = compact(resolved.get("action")) or "defer"
                topics = {str(value) for value in resolved.get("topic_ids", [])}
                return action, topics, compact(resolved.get("reason")) or "resolver", "model"
        if best.hard_relation or best.score >= 0.55 or (best.score >= 0.42 and second is None):
            if second and margin < 0.10 and not best.hard_relation:
                return "defer", set(), "ambiguous local candidates", "local_score"
            return "attach", {best.topic_id}, f"best={best.score:.3f}; margin={margin:.3f}", "local_score"
        if best.score < 0.28:
            return "create_seed", set(), f"candidate below seed threshold: {best.score:.3f}", "hard_rule"
        return "defer", set(), f"candidate uncertain: {best.score:.3f}", "local_score"

    def _create_seed(self, claim: Claim, item: ContentItem, at: str) -> str:
        topic_id = stable_id("topic", claim.claim_id)
        subject = compact(claim.claim_text)[:120]
        self.connection.execute(
            # Seed IDs are deterministic so an inbox retry can encounter a
            # topic that was committed before the inbox marker was updated.
            "INSERT OR IGNORE INTO topics (topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,"
            "started_at,first_seen_at,seed_expires_at,last_evidence_at,last_participation_at,matching_status,"
            "visibility,archived_at,brief_status,brief_error,brief_error_at,brief_retry_count,brief_retry_after,identity_revision,participant_count_1h,"
            "participant_count_6h,participant_count_24h,participant_velocity,hotness_score) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                topic_id, subject, subject, json_dumps(list(claim.entities)), claim.action_or_issue,
                item.created_at, item.created_at, (dt(item.created_at) + timedelta(hours=24)).isoformat(),
                item.created_at, item.created_at, "seed", "hidden", None, "pending", None, None, 0, None,
                1, 0, 0, 0, 0.0, 0.0,
            ),
        )
        return topic_id

    def _attach_claim(
        self,
        claim: Claim,
        topic_id: str,
        candidates: Sequence[Candidate],
        reason: str,
        source: str,
        item: ContentItem,
        at: str,
        *,
        defer_participation: bool = False,
    ) -> None:
        best_score = next((candidate.score for candidate in candidates if candidate.topic_id == topic_id), 0.0)
        relation_values = self._relations(claim)
        topic = self.connection.execute("SELECT event_or_issue FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        for relation in relation_values:
            thread_id = self._ensure_thread(topic_id, relation, claim, at)
            membership_id = stable_id("membership", claim.claim_id, topic_id, thread_id)
            self.connection.execute(
                "INSERT OR IGNORE INTO memberships VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    membership_id, claim.claim_id, topic_id, thread_id, "primary",
                    self._membership_role(claim), best_score, source, reason, at, None,
                ),
            )
        if not defer_participation:
            self.connection.execute(
                "INSERT INTO topic_participations VALUES (?,?,?,?) ON CONFLICT(topic_id,activity_account) DO UPDATE SET "
                "last_participation_at=MAX(last_participation_at, excluded.last_participation_at), last_content_item_id=excluded.last_content_item_id",
                (topic_id, item.activity_account, item.created_at, item.content_item_id),
            )
        self.connection.execute(
            "UPDATE topics SET last_evidence_at=MAX(COALESCE(last_evidence_at, ''), ?) WHERE topic_id=?",
            (item.created_at, topic_id),
        )
        self._update_retrieval(topic_id, at)

    def _store_topic_claim_review(
        self,
        claim_id: str,
        topic_id: str,
        decision: str,
        reason: str,
        *,
        confidence: float,
        model: str,
        reasoning_effort: str,
        input_hash: str,
        reviewed_at: str,
        error: str | None = None,
    ) -> None:
        if decision not in SEMANTIC_REVIEW_DECISIONS:
            raise ValueError(f"invalid semantic review decision: {decision}")
        confidence = min(1.0, max(0.0, float(confidence)))
        self.connection.execute(
            "INSERT OR REPLACE INTO topic_claim_reviews("
            "claim_id,topic_id,decision,confidence,reason,model,reasoning_effort,input_hash,reviewed_at,error"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                claim_id, topic_id, decision, confidence, compact(reason), model, reasoning_effort,
                input_hash, reviewed_at, error,
            ),
        )

    def _semantic_review_case(self, claim_id: str, topic_id: str, pending: set[tuple[str, str]]) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT cl.*,ci.activity_account,ci.created_at,ci.tweet_id,ci.source_url,"
            "t.working_title,t.canonical_subject,t.core_entities_json,t.event_or_issue,t.identity_revision "
            "FROM claims cl JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
            "JOIN topics t ON t.topic_id=? WHERE cl.claim_id=?",
            (topic_id, claim_id),
        ).fetchone()
        if row is None:
            return None
        evidence_rows = self.connection.execute(
            "SELECT cl.claim_id,cl.claim_text,ci.activity_account,ci.created_at "
            "FROM memberships m JOIN claims cl ON cl.claim_id=m.claim_id "
            "JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
            "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
            "WHERE m.topic_id=? AND m.superseded_by IS NULL "
            "AND COALESCE(r.decision,'support') IN ('support','context') "
            "ORDER BY ci.created_at DESC LIMIT 12",
            (topic_id,),
        ).fetchall()
        evidence = [
            {
                "claim_id": evidence_row["claim_id"],
                "account": evidence_row["activity_account"],
                "created_at": evidence_row["created_at"],
                "text": evidence_row["claim_text"],
            }
            for evidence_row in evidence_rows
            if (evidence_row["claim_id"], topic_id) not in pending and evidence_row["claim_id"] != claim_id
        ][:8]
        topic = {
            "topic_id": topic_id,
            "title": row["working_title"],
            "canonical_subject": row["canonical_subject"],
            "core_entities": json_loads(row["core_entities_json"], []),
            "event_or_issue": row["event_or_issue"],
            "identity_revision": int(row["identity_revision"] or 0),
            "evidence": evidence,
        }
        claim = {
            "claim_id": claim_id,
            "account": row["activity_account"],
            "created_at": row["created_at"],
            "tweet_id": row["tweet_id"],
            "source_url": row["source_url"],
            "kind": row["claim_kind"],
            "entities": json_loads(row["entities_json"], []),
            "text": row["claim_text"],
        }
        fingerprint = hashlib.sha256(json_dumps({"claim": claim, "topic": topic}).encode("utf-8")).hexdigest()
        return {
            "case_id": stable_id("semantic-review-case", claim_id, topic_id),
            "claim_id": claim_id,
            "topic_id": topic_id,
            "claim": claim,
            "topic": topic,
            "input_hash": fingerprint,
        }

    def _unreviewed_review_targets(
        self,
        topic_ids: Iterable[str],
        *,
        limit: int,
        exclude: set[tuple[str, str]] | None = None,
    ) -> set[tuple[str, str]]:
        """Return a bounded queue for old links that predate semantic review."""
        topic_ids = sorted(set(topic_ids))
        if not topic_ids or limit <= 0:
            return set()
        marks = ",".join("?" for _ in topic_ids)
        rows = self.connection.execute(
            "SELECT m.claim_id,m.topic_id FROM memberships m "
            "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
            f"WHERE m.topic_id IN ({marks}) AND m.superseded_by IS NULL "
            "AND (r.claim_id IS NULL OR r.error IS NOT NULL) "
            "ORDER BY m.created_at,m.claim_id LIMIT ?",
            [*topic_ids, int(limit)],
        ).fetchall()
        excluded = exclude or set()
        return {
            (str(row["claim_id"]), str(row["topic_id"]))
            for row in rows
            if (str(row["claim_id"]), str(row["topic_id"])) not in excluded
        }

    def _review_memberships(self, targets: set[tuple[str, str]], at: str) -> dict[str, Any]:
        """Review new claim-topic links outside the SQLite write transaction."""
        if not self.semantic_reviewer or not targets:
            return {"enabled": bool(self.semantic_reviewer), "requested": 0, "support": 0, "context": 0, "unrelated": 0, "fallback": 0, "model_calls": 0}
        cases: list[dict[str, Any]] = []
        pending = set(targets)
        for claim_id, topic_id in sorted(targets):
            case = self._semantic_review_case(claim_id, topic_id, pending)
            if case is not None:
                cases.append(case)
        result = {"enabled": True, "requested": len(cases), "support": 0, "context": 0, "unrelated": 0, "fallback": 0, "model_calls": 0}
        misses: list[dict[str, Any]] = []
        for case in cases:
            cached = self.connection.execute(
                "SELECT decision,confidence,reason,model,reasoning_effort,error FROM topic_claim_reviews "
                "WHERE claim_id=? AND topic_id=? AND input_hash=?",
                (case["claim_id"], case["topic_id"], case["input_hash"]),
            ).fetchone()
            if cached is None or cached["error"]:
                misses.append(case)
                continue
            decision = str(cached["decision"])
            result[decision] = result.get(decision, 0) + 1
        for start in range(0, len(misses), self.semantic_review_batch_size):
            chunk = misses[start : start + self.semantic_review_batch_size]
            response_by_case: dict[str, dict[str, Any]] = {}
            error_message = ""
            try:
                result["model_calls"] += 1
                raw_response = self.semantic_reviewer(chunk)
                response_by_case = {
                    str(row.get("case_id")): dict(row)
                    for row in raw_response
                    if isinstance(row, dict) and row.get("case_id")
                }
            except Exception as exc:
                error_message = f"{type(exc).__name__}: {str(exc).strip() or 'semantic reviewer failed'}"
            for case in chunk:
                response = response_by_case.get(case["case_id"], {})
                decision = compact(response.get("decision"))
                fallback = not error_message and decision not in SEMANTIC_REVIEW_DECISIONS
                if error_message or fallback:
                    decision = "support"
                    result["fallback"] += 1
                    reason = "AI复核失败，沿用确定性聚合结果" if error_message else "AI复核返回了无效分类，沿用确定性聚合结果"
                    error = error_message or "invalid_decision"
                    confidence = 0.0
                else:
                    reason = compact(response.get("reason")) or "AI语义复核"
                    error = None
                    try:
                        confidence = min(1.0, max(0.0, float(response.get("confidence", 0.0))))
                    except (TypeError, ValueError):
                        confidence = 0.0
                result[decision] = result.get(decision, 0) + 1
                self._store_topic_claim_review(
                    case["claim_id"], case["topic_id"], decision, reason,
                    confidence=confidence,
                    model=compact(response.get("model")) or getattr(self.semantic_reviewer, "model", ""),
                    reasoning_effort=compact(response.get("reasoning_effort")) or getattr(self.semantic_reviewer, "reasoning_effort", ""),
                    input_hash=case["input_hash"], reviewed_at=at, error=error,
                )
            self.connection.commit()
        return result

    def _rebuild_topic_participations(self, topic_ids: set[str], at: str) -> None:
        """Recompute participants from support claims, excluding context-only links."""
        for topic_id in sorted(topic_ids):
            topic = self.connection.execute("SELECT topic_id FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
            if topic is None:
                continue
            self.connection.execute("DELETE FROM topic_participations WHERE topic_id=?", (topic_id,))
            rows = self.connection.execute(
                "SELECT ci.activity_account,ci.created_at,ci.content_item_id FROM memberships m "
                "JOIN claims cl ON cl.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
                "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
                "WHERE m.topic_id=? AND m.superseded_by IS NULL AND COALESCE(r.decision,'support')='support' "
                "ORDER BY ci.created_at",
                (topic_id,),
            ).fetchall()
            latest: dict[str, sqlite3.Row] = {}
            for row in rows:
                previous = latest.get(row["activity_account"])
                if previous is None or dt(row["created_at"]) >= dt(previous["created_at"]):
                    latest[row["activity_account"]] = row
            self.connection.executemany(
                "INSERT INTO topic_participations VALUES (?,?,?,?)",
                [(topic_id, row["activity_account"], row["created_at"], row["content_item_id"]) for row in latest.values()],
            )
            latest_evidence = self.connection.execute(
                "SELECT MAX(ci.created_at) AS latest FROM memberships m "
                "JOIN claims cl ON cl.claim_id=m.claim_id "
                "JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
                "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
                "WHERE m.topic_id=? AND m.superseded_by IS NULL "
                "AND COALESCE(r.decision,'support') IN ('support','context')",
                (topic_id,),
            ).fetchone()["latest"]
            self.connection.execute(
                "UPDATE topics SET last_evidence_at=? WHERE topic_id=?",
                (latest_evidence, topic_id),
            )
            self._update_retrieval(topic_id, at)

    @staticmethod
    def _normalized_identity(value: str) -> str:
        return value.lower().strip().lstrip("$#@").strip("-_.")

    @classmethod
    def _name_tokens(cls, value: str) -> set[str]:
        """Return non-generic components for cross-writing identity checks."""
        raw = value.strip().lstrip("$#@")
        return {
            cls._normalized_identity(part)
            for part in re.findall(r"[A-Z]+(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+|\d+", raw)
            if cls._normalized_identity(part) not in GENERIC_NAMED_EVENT_ENTITIES
            and cls._normalized_identity(part) not in GENERIC_ASSET_IDENTITIES
        }

    @classmethod
    def _plain_identity_aliases(cls, text: str) -> set[str]:
        """Find broad recall aliases without treating them as event identity."""
        aliases = set(asset_labels(text))
        aliases.update(value.lower().lstrip("#") for value in RE_HASHTAG.findall(text))
        for raw in RE_WORD.findall(text.lower()):
            if not re.fullmatch(r"[\u4e00-\u9fff]{2,}", raw):
                continue
            for size in (2, 3, 4):
                for index in range(max(0, len(raw) - size + 1)):
                    value = raw[index : index + size]
                    if value not in GENERIC_CHINESE_IDENTITY_TERMS:
                        aliases.add(value)
        return aliases

    @classmethod
    def _event_identity(cls, rows: Sequence[sqlite3.Row]) -> EventIdentity:
        merge_anchors: set[str] = set()
        aliases: set[str] = set()
        named_entities: set[str] = set()
        name_tokens: set[str] = set()
        lookup_terms: set[str] = set()
        for row in rows:
            text = compact(row["claim_text"])
            for normalized in asset_labels(text):
                merge_anchors.add(f"asset:{normalized}")
                if normalized not in GENERIC_ASSET_IDENTITIES:
                    named_entities.add(normalized)
                    name_tokens.update(cls._name_tokens(normalized))
                lookup_terms.update((normalized, f"${normalized}", f"#{normalized}"))
            for value in RE_CONTRACT.findall(text):
                normalized = value.lower()
                merge_anchors.add(f"contract:{normalized}")
                lookup_terms.add(normalized)
            for value in RE_MENTION.findall(text):
                normalized = cls._normalized_identity(value)
                if normalized and normalized not in GENERIC_NAMED_EVENT_ENTITIES:
                    named_entities.add(normalized)
                    name_tokens.update(cls._name_tokens(value))
            for value in re.findall(r"\b[A-Za-z][A-Za-z0-9_]{2,30}\b", text):
                normalized = cls._normalized_identity(value)
                if not normalized or normalized in IDENTITY_STOPWORDS or normalized in GENERIC_ASSET_IDENTITIES:
                    continue
                is_named = (
                    value.isupper()
                    or any(char.isupper() for char in value[1:])
                    or (value[0].isupper() and len(value) >= 5)
                    or bool(re.fullmatch(r"[A-Za-z]+\d+", value))
                )
                if is_named:
                    aliases.add(normalized)
                    if normalized not in GENERIC_NAMED_EVENT_ENTITIES:
                        named_entities.add(normalized)
                        name_tokens.update(cls._name_tokens(value))
                    lookup_terms.add(normalized)
            prose_aliases = cls._plain_identity_aliases(text)
            aliases.update(prose_aliases)
            lookup_terms.update(prose_aliases)
        return EventIdentity(
            frozenset(merge_anchors),
            frozenset(aliases),
            frozenset(named_entities),
            frozenset(name_tokens),
            frozenset(lookup_terms),
        )

    @staticmethod
    def _event_kinds(rows: Sequence[sqlite3.Row]) -> set[str]:
        kinds: set[str] = set()
        for row in rows:
            text = compact(row["claim_text"]).lower()
            if any(signal in text for signal in LAUNCH_SIGNALS):
                kinds.add("launch")
            if any(signal in text for signal in LISTING_SIGNALS):
                kinds.add("listing")
        return kinds

    @staticmethod
    def _primary_identity_values(accounts_by_value: dict[str, set[str]]) -> set[str]:
        if not accounts_by_value:
            return set()
        strongest = max(len(accounts) for accounts in accounts_by_value.values())
        leaders = {
            value for value, accounts in accounts_by_value.items()
            if len(accounts) == strongest
        }
        # A single post comparing several assets does not establish which one
        # owns the Topic.  Wait for another account to break the tie.
        if strongest == 1 and len(leaders) > 1:
            return set()
        return leaders

    def _topic_event_profile(self, topic_id: str) -> dict[str, Any] | None:
        topic = self.connection.execute(
            "SELECT * FROM topics WHERE topic_id=? AND matching_status IN ('seed','active')", (topic_id,)
        ).fetchone()
        if not topic:
            return None
        rows = self.connection.execute(
            "SELECT cl.*,ci.activity_account,ci.created_at,ci.tweet_id FROM memberships m "
            "JOIN claims cl ON cl.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
            "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
            "WHERE m.topic_id=? AND m.superseded_by IS NULL AND COALESCE(r.decision,'support')='support' "
            "GROUP BY cl.claim_id ORDER BY ci.created_at",
            (topic_id,),
        ).fetchall()
        if not rows:
            return None

        launch_accounts: set[str] = set()
        mechanism_accounts: set[str] = set()
        asset_accounts: defaultdict[str, set[str]] = defaultdict(set)
        subject_label_accounts: defaultdict[str, set[str]] = defaultdict(set)
        contract_accounts: defaultdict[str, set[str]] = defaultdict(set)
        for row in rows:
            text = compact(row["claim_text"])
            lower = text.lower()
            if any(signal in lower for signal in EVENT_FORMATION_SIGNALS):
                launch_accounts.add(row["activity_account"])
            if any(signal in lower for signal in MECHANISM_SIGNALS):
                mechanism_accounts.add(row["activity_account"])
            for asset in asset_labels(text):
                asset_accounts[asset].add(row["activity_account"])
            for label in RE_HASHTAG.findall(text):
                subject_label_accounts[label.lower().lstrip("#")].add(row["activity_account"])
            for asset in asset_labels(text):
                subject_label_accounts[asset].add(row["activity_account"])
            for contract in RE_CONTRACT.findall(text):
                contract_accounts[contract.lower()].add(row["activity_account"])
        identity = self._event_identity(rows)
        event_kinds = self._event_kinds(rows)
        participants = {
            row["activity_account"]
            for row in self.connection.execute(
                "SELECT activity_account FROM topic_participations WHERE topic_id=?", (topic_id,)
            )
        }
        event_score = (
            len(launch_accounts) * 4
            + len(mechanism_accounts) * 3
            + min(len(participants), 10)
            + min(len(rows), 12) * 0.1
        )

        return {
            "topic_id": topic_id,
            "topic": topic,
            "rows": rows,
            "identity": identity,
            "event_kinds": event_kinds,
            "participants": participants,
            "launch_accounts": launch_accounts,
            "mechanism_accounts": mechanism_accounts,
            "asset_accounts": asset_accounts,
            "subject_label_accounts": subject_label_accounts,
            "contract_accounts": contract_accounts,
            "primary_assets": self._primary_identity_values(asset_accounts),
            "primary_contracts": self._primary_identity_values(contract_accounts),
            "event_score": event_score,
        }

    @staticmethod
    def _topics_share_continuous_subject(
        left: dict[str, Any],
        right: dict[str, Any],
        label_support: dict[str, set[str]] | None = None,
        dollar_support: set[str] | None = None,
    ) -> tuple[bool, str]:
        left_start = dt(left["topic"]["started_at"])
        right_start = dt(right["topic"]["started_at"])
        if abs((left_start - right_start).total_seconds()) > 24 * 3600:
            return False, "outside 24h topic window"
        left_identity: EventIdentity = left["identity"]
        right_identity: EventIdentity = right["identity"]
        left_assets = set(left["primary_assets"])
        right_assets = set(right["primary_assets"])
        shared_assets = left_assets & right_assets
        if shared_assets:
            return True, f"same primary asset={sorted(shared_assets)[:6]}"
        shared_contracts = set(left["primary_contracts"]) & set(right["primary_contracts"])
        if shared_contracts:
            return True, f"same primary contract={sorted(shared_contracts)[:6]}"

        # Plain prose may be folded into an established asset Topic, but a
        # Topic with a different primary asset cannot be used as a bridge.
        left_aliases = left_identity.aliases
        right_aliases = right_identity.aliases
        if left_assets and not right_assets:
            aliases = left_assets & right_aliases
            if aliases:
                return True, f"primary asset prose alias={sorted(aliases)[:6]}"
        if right_assets and not left_assets:
            aliases = right_assets & left_aliases
            if aliases:
                return True, f"primary asset prose alias={sorted(aliases)[:6]}"

        # A subordinate asset can join a larger event only when at least two
        # independent accounts explicitly connect it to the other Topic's
        # primary asset and the surrounding evidence is event/mechanism based.
        if left_assets and right_assets and (left["launch_accounts"] or right["launch_accounts"]):
            left_in_right = {
                asset for asset in left_assets
                if len(right["asset_accounts"].get(asset, set())) >= 2
            }
            right_in_left = {
                asset for asset in right_assets
                if len(left["asset_accounts"].get(asset, set())) >= 2
            }
            connected = left_in_right | right_in_left
            if connected:
                return True, f"repeated subordinate event asset={sorted(connected)[:6]}"

        # A topic with a concrete asset cannot be merged into an unanchored
        # event merely because both happen to mention a platform or wallet.
        # The only valid asset-to-prose bridge was handled above through an
        # exact asset alias; continuing into named-entity matching would turn
        # unrelated Binance, exchange, and wallet events into asset evidence.
        left_has_explicit_asset = any(anchor.startswith(("asset:", "contract:")) for anchor in left_identity.merge_anchors)
        right_has_explicit_asset = any(anchor.startswith(("asset:", "contract:")) for anchor in right_identity.merge_anchors)
        if bool(left_assets) != bool(right_assets) and (left_has_explicit_asset or right_has_explicit_asset):
            return False, "asset topic lacks an exact prose alias"

        # Non-asset events (product releases, corporate actions, protocol
        # changes) need an identity path too.  Use only stable named entities,
        # never broad recall aliases or CJK n-grams, and require the same
        # event category plus independent cross-account support.  Two distinct
        # primary assets remain non-mergeable without an existing strong link.
        shared_named_entities = left_identity.named_entities & right_identity.named_entities
        shared_name_tokens = left_identity.name_tokens & right_identity.name_tokens
        named_event_support = set(shared_named_entities)
        if len(named_event_support) < 2:
            named_event_support.update(sorted(shared_name_tokens - named_event_support)[:2 - len(named_event_support)])
        shared_event_kinds = set(left.get("event_kinds", set())) & set(right.get("event_kinds", set()))
        independent_accounts = set(left["participants"]) | set(right["participants"])
        if (
            not (left_assets and right_assets)
            and shared_named_entities
            and len(named_event_support) >= 2
            and shared_event_kinds
            and len(independent_accounts) >= 3
        ):
            kind = sorted(shared_event_kinds)[0]
            return True, f"shared named event identity={sorted(named_event_support)[:6]}; kind={kind}"

        # A subject may be written as a plain name or hashtag in some posts
        # and as a dollar label in another.  Permit that bridge only when it
        # has independent-account support and an explicit dollar-label
        # witness.  This keeps generic hashtags from becoming merge keys while
        # allowing one token's narrative to remain one large Topic.
        left_labels = set(left.get("subject_label_accounts", {}))
        left_labels.update(
            alias for alias in left_identity.aliases
            if label_support is None or alias in label_support
        )
        right_labels = set(right.get("subject_label_accounts", {}))
        right_labels.update(
            alias for alias in right_identity.aliases
            if label_support is None or alias in label_support
        )
        for label in sorted(left_labels & right_labels):
            if label in GENERIC_TOPIC_LABELS:
                continue
            accounts = (label_support or {}).get(label, set())
            if len(accounts) < 2 or label not in (dollar_support or set()):
                continue
            # Do not let a multi-asset event use a secondary mention as a
            # bridge.  Check all explicit labels, not only primary_assets:
            # during the same consolidation pass a competing asset may not
            # have reached the primary threshold yet.
            left_explicit = set(left.get("asset_accounts", {}))
            right_explicit = set(right.get("asset_accounts", {}))
            if left_explicit and left_explicit != {label}:
                continue
            if right_explicit and right_explicit != {label}:
                continue
            return True, f"shared supported subject label={label}"
        return False, "no exact topic identity"

    @staticmethod
    def _claim_from_row(row: sqlite3.Row) -> Claim:
        return Claim(
            claim_id=row["claim_id"],
            content_item_id=row["content_item_id"],
            claim_text=row["claim_text"],
            claim_kind=row["claim_kind"],
            entities=tuple(json_loads(row["entities_json"], [])),
            action_or_issue=row["action_or_issue"],
            stance=row["stance"],
            evidence_span=row["evidence_span"],
            confidence=float(row["confidence"]),
            information_value=float(row["information_value"]),
        )

    def _refresh_topic_identity(self, topic_id: str) -> None:
        rows = self.connection.execute(
            "SELECT cl.*,ci.activity_account FROM memberships m JOIN claims cl ON cl.claim_id=m.claim_id "
            "JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
            "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
            "WHERE m.topic_id=? AND m.superseded_by IS NULL AND COALESCE(r.decision,'support')='support' "
            "GROUP BY cl.claim_id",
            (topic_id,),
        ).fetchall()
        if not rows:
            return

        asset_accounts: defaultdict[str, set[str]] = defaultdict(set)
        for row in rows:
            for asset in asset_labels(row["claim_text"]):
                asset_accounts[asset].add(row["activity_account"])
        primary_assets = self._primary_identity_values(asset_accounts)

        def mentions_primary(row: sqlite3.Row) -> bool:
            lower = row["claim_text"].lower()
            return any(
                asset in lower
                if any("\u4e00" <= char <= "\u9fff" for char in asset)
                else re.search(rf"(?<![a-z0-9_]){re.escape(asset)}(?![a-z0-9_])", lower)
                for asset in primary_assets
            )

        def identity_score(row: sqlite3.Row) -> tuple[float, int, str]:
            text = compact(row["claim_text"])
            lower = text.lower()
            launch = sum(signal in lower for signal in EVENT_FORMATION_SIGNALS)
            mechanism = sum(signal in lower for signal in MECHANISM_SIGNALS)
            market_effect = sum(
                signal in lower
                for signal in ("流动性", "吸血", "注意力", "首批", "liquidity", "absorbing", "pvp")
            )
            named = len(RE_CASHTAG.findall(text)) + len(RE_MENTION.findall(text))
            digits = 1 if re.search(r"\b\d+(?:\.\d+)?[kKmMbB万亿%]?\b", RE_CONTRACT.sub("", text)) else 0
            chinese = 1 if any("\u4e00" <= char <= "\u9fff" for char in text) else 0
            return (launch * 5 + mechanism * 3 + named + digits + chinese, min(len(text), 240), row["claim_id"])

        identity_rows = [row for row in rows if mentions_primary(row)] or list(rows)
        best = max(identity_rows, key=identity_score)
        title = compact(best["claim_text"])[:180]
        canonical = (
            f"${next(iter(primary_assets))}"
            if len(primary_assets) == 1
            else title
        )
        entities: set[str] = set()
        issues: set[str] = set()
        for row in rows:
            entities.update(json_loads(row["entities_json"], []))
            issues.update(value for value in row["action_or_issue"].split(",") if value)
        if any(any(signal in row["claim_text"].lower() for signal in EVENT_FORMATION_SIGNALS) for row in rows):
            issues.add("发布/上线")
        if any(any(signal in row["claim_text"].lower() for signal in MECHANISM_SIGNALS) for row in rows):
            issues.add("产品机制")
        self.connection.execute(
            "UPDATE topics SET working_title=?,canonical_subject=?,core_entities_json=?,event_or_issue=?,identity_revision=identity_revision+1 "
            "WHERE topic_id=?",
            (title, canonical, json_dumps(sorted(entities)), ",".join(sorted(issues)) or "一般讨论", topic_id),
        )

    def _merge_topic(self, source_topic_id: str, target_topic_id: str, at: str, reason: str, *, preserve_identity: bool = False) -> dict[str, str]:
        rows = self.connection.execute(
            "SELECT m.*,th.relation_to_topic,cl.*,ci.activity_account FROM memberships m "
            "JOIN claims cl ON cl.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
            "LEFT JOIN threads th ON th.thread_id=m.thread_id "
            "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
            "WHERE m.topic_id=? AND m.superseded_by IS NULL AND COALESCE(r.decision,'support') IN ('support','context')",
            (source_topic_id,),
        ).fetchall()
        for row in rows:
            claim = self._claim_from_row(row)
            relation = row["relation_to_topic"] or self._relations(claim)[0]
            thread_id = self._ensure_thread(target_topic_id, relation, claim, at)
            membership_id = stable_id("membership", claim.claim_id, target_topic_id, thread_id)
            self.connection.execute(
                "INSERT OR IGNORE INTO memberships VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    membership_id, claim.claim_id, target_topic_id, thread_id, row["topic_role"], row["role"],
                    row["match_score"], "local_merge", reason, at, None,
                ),
            )
            review = self.connection.execute(
                "SELECT decision,confidence,reason,model,reasoning_effort,input_hash,reviewed_at,error "
                "FROM topic_claim_reviews WHERE claim_id=? AND topic_id=?",
                (claim.claim_id, source_topic_id),
            ).fetchone()
            if review:
                self.connection.execute(
                    "INSERT OR IGNORE INTO topic_claim_reviews("
                    "claim_id,topic_id,decision,confidence,reason,model,reasoning_effort,input_hash,reviewed_at,error"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        claim.claim_id, target_topic_id, review["decision"], review["confidence"], review["reason"],
                        review["model"], review["reasoning_effort"], review["input_hash"], review["reviewed_at"], review["error"],
                    ),
                )
            self.connection.execute(
                "UPDATE memberships SET superseded_by=? WHERE membership_id=?", (membership_id, row["membership_id"])
            )

        for row in self.connection.execute(
            "SELECT activity_account,last_participation_at,last_content_item_id FROM topic_participations WHERE topic_id=?",
            (source_topic_id,),
        ).fetchall():
            existing = self.connection.execute(
                "SELECT last_participation_at FROM topic_participations WHERE topic_id=? AND activity_account=?",
                (target_topic_id, row["activity_account"]),
            ).fetchone()
            if not existing or dt(row["last_participation_at"]) >= dt(existing["last_participation_at"]):
                self.connection.execute(
                    "INSERT INTO topic_participations VALUES (?,?,?,?) ON CONFLICT(topic_id,activity_account) DO UPDATE SET "
                    "last_participation_at=excluded.last_participation_at,last_content_item_id=excluded.last_content_item_id",
                    (target_topic_id, row["activity_account"], row["last_participation_at"], row["last_content_item_id"]),
                )

        source = self.connection.execute("SELECT * FROM topics WHERE topic_id=?", (source_topic_id,)).fetchone()
        target = self.connection.execute("SELECT * FROM topics WHERE topic_id=?", (target_topic_id,)).fetchone()
        start = min(source["started_at"], target["started_at"])
        first_seen = min(source["first_seen_at"], target["first_seen_at"])
        seed_expires = (dt(start) + timedelta(hours=24)).isoformat()
        last_evidence = max(source["last_evidence_at"] or source["started_at"], target["last_evidence_at"] or target["started_at"])
        self.connection.execute(
            "UPDATE topics SET started_at=?,first_seen_at=?,seed_expires_at=?,last_evidence_at=? WHERE topic_id=?",
            (start, first_seen, seed_expires, last_evidence, target_topic_id),
        )
        self.connection.execute(
            "UPDATE topics SET matching_status='archived',visibility='hidden',archived_at=?,participant_count_1h=0,"
            "participant_count_6h=0,participant_count_24h=0,participant_velocity=0,hotness_score=0 WHERE topic_id=?",
            (at, source_topic_id),
        )
        merge_id = stable_id("topic-merge", source_topic_id, target_topic_id)
        self.connection.execute(
            "INSERT OR IGNORE INTO topic_merges VALUES (?,?,?,?,?)",
            (merge_id, source_topic_id, target_topic_id, reason, at),
        )
        if not preserve_identity:
            self._refresh_topic_identity(target_topic_id)
        self._update_retrieval(target_topic_id, at)
        cached = self._retrieval_cache.get(source_topic_id)
        if cached:
            cached["matching_status"] = "archived"
        return {
            "merge_id": merge_id,
            "source_topic_id": source_topic_id,
            "target_topic_id": target_topic_id,
            "reason": reason,
        }

    def _count_pending_claim_reviews(self) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) FROM memberships m JOIN topics t ON t.topic_id=m.topic_id "
            "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
            "WHERE t.matching_status IN ('seed','active') AND m.superseded_by IS NULL "
            "AND (r.claim_id IS NULL OR r.error IS NOT NULL)"
        ).fetchone()[0]

    def _review_topic_pairs(self, dirty_topic_ids: set[str], at: str) -> list[dict[str, str]]:
        if not self.topic_merge_reviewer:
            return []
        visible_ids = {
            row["topic_id"]
            for row in self.connection.execute(
                "SELECT topic_id FROM topics "
                "WHERE matching_status IN ('seed','active') AND visibility='visible'"
            )
        } & dirty_topic_ids
        pairs: dict[tuple[str, str], dict[str, Any]] = {}
        for topic_id in sorted(visible_ids):
            profile = self._topic_event_profile(topic_id)
            if not profile:
                continue
            candidates: set[str] = set()
            for feature in profile["identity"].lookup_terms:
                candidates.update(self._feature_index.get(f"t:{feature}", set()))
            for candidate_id in sorted(candidates - {topic_id}):
                other = self._topic_event_profile(candidate_id)
                if not other or other["topic"]["visibility"] != "visible":
                    continue
                left_id, right_id = sorted((topic_id, candidate_id))
                if (left_id, right_id) in pairs:
                    continue
                left, right = (profile, other) if topic_id == left_id else (other, profile)

                def snapshot(value: dict[str, Any]) -> dict[str, Any]:
                    return {
                        "topic_id": value["topic_id"],
                        "title": value["topic"]["working_title"],
                        "subject": value["topic"]["canonical_subject"],
                        "event_or_issue": value["topic"]["event_or_issue"],
                        "started_at": value["topic"]["started_at"],
                        "last_evidence_at": value["topic"]["last_evidence_at"],
                        "evidence": [
                            {"claim_id": row["claim_id"], "account": row["activity_account"], "text": row["claim_text"]}
                            for row in value["rows"][-6:]
                        ],
                    }

                case = {"case_id": stable_id("topic-pair", left_id, right_id), "left": snapshot(left), "right": snapshot(right)}
                case["input_hash"] = hashlib.sha256(json_dumps(case).encode("utf-8")).hexdigest()
                old = self.connection.execute(
                    "SELECT input_hash,decision,next_attempt_at FROM topic_pair_reviews WHERE left_topic_id=? AND right_topic_id=?",
                    (left_id, right_id),
                ).fetchone()
                if old and old["input_hash"] == case["input_hash"]:
                    if old["decision"] != "failed" or (old["next_attempt_at"] and old["next_attempt_at"] > at):
                        continue
                pairs[(left_id, right_id)] = case
        selected = list(pairs.items())[:40]
        requests: list[dict[str, str]] = []
        for offset in range(0, len(selected), 10):
            chunk = selected[offset:offset + 10]
            error = "invalid topic pair review"
            try:
                responses = self.topic_merge_reviewer([case for _, case in chunk])
                by_id = {str(row["case_id"]): row for row in responses}
            except Exception as exc:
                by_id = {}
                error = f"{type(exc).__name__}: {exc}"[:1000]
            for (left_id, right_id), case in chunk:
                response = by_id.get(case["case_id"], {})
                decision = str(response.get("decision") or "")
                evidence_ids = set(response.get("evidence_claim_ids") or [])
                left_ids = {row["claim_id"] for row in case["left"]["evidence"]}
                right_ids = {row["claim_id"] for row in case["right"]["evidence"]}
                try:
                    confidence = float(response.get("confidence") or 0)
                except (TypeError, ValueError):
                    confidence = 0.0
                if decision == "merge" and not (
                    confidence >= 0.8 and evidence_ids & left_ids and evidence_ids & right_ids
                    and compact(response.get("shared_event_fact"))
                ):
                    decision = "failed"
                if decision not in {"merge", "separate"}:
                    decision = "failed"
                reason = compact(response.get("reason")) if decision != "failed" else error
                next_attempt = iso(dt(at) + timedelta(minutes=15)) if decision == "failed" else None
                with self.connection:
                    self.connection.execute(
                        "INSERT INTO topic_pair_reviews(left_topic_id,right_topic_id,input_hash,decision,reason,evidence_json,model,reasoning_effort,attempts,reviewed_at,next_attempt_at) "
                        "VALUES(?,?,?,?,?,?,?,?,1,?,?) ON CONFLICT(left_topic_id,right_topic_id) DO UPDATE SET "
                        "input_hash=excluded.input_hash,decision=excluded.decision,reason=excluded.reason,evidence_json=excluded.evidence_json,"
                        "model=excluded.model,reasoning_effort=excluded.reasoning_effort,attempts=topic_pair_reviews.attempts+1,"
                        "reviewed_at=excluded.reviewed_at,next_attempt_at=excluded.next_attempt_at",
                        (left_id, right_id, case["input_hash"], decision, reason, json_dumps(response),
                         str(response.get("model") or ""), str(response.get("reasoning_effort") or ""), at, next_attempt),
                    )
                    if decision == "merge":
                        left = self._topic_event_profile(left_id)
                        right = self._topic_event_profile(right_id)
                        if left and right:
                            left_rank = (left["topic"]["matching_status"] == "active", -dt(left["topic"]["started_at"]).timestamp())
                            right_rank = (right["topic"]["matching_status"] == "active", -dt(right["topic"]["started_at"]).timestamp())
                            target, source = (left, right) if left_rank >= right_rank else (right, left)
                            requests.append(self._merge_topic(source["topic_id"], target["topic_id"], at, f"semantic: {reason}"))
        return requests

    def _consolidate_dirty_topics(self, dirty_topic_ids: set[str], at: str) -> list[dict[str, str]]:
        requests: list[dict[str, str]] = []
        label_support: defaultdict[str, set[str]] = defaultdict(set)
        dollar_support: set[str] = set()
        support_rows = self.connection.execute(
            "SELECT DISTINCT cl.claim_text,ci.activity_account FROM claims cl "
            "JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
            "JOIN memberships m ON m.claim_id=cl.claim_id AND m.superseded_by IS NULL "
            "JOIN topics t ON t.topic_id=m.topic_id "
            "WHERE t.matching_status IN ('seed','active') AND t.visibility='visible'"
        ).fetchall()
        for row in support_rows:
            text = compact(row["claim_text"])
            for label in RE_HASHTAG.findall(text):
                label_support[label.lower().lstrip("#")].add(row["activity_account"])
            for asset in asset_labels(text):
                label_support[asset].add(row["activity_account"])
                dollar_support.add(asset)
        visible_ids = {
            row["topic_id"]
            for row in self.connection.execute(
                "SELECT topic_id FROM topics "
                "WHERE matching_status IN ('seed','active') AND visibility='visible'"
            )
        } & dirty_topic_ids
        queue = list(sorted(visible_ids))
        considered: set[tuple[str, str]] = set()
        while queue:
            topic_id = queue.pop(0)
            profile = self._topic_event_profile(topic_id)
            if not profile:
                continue
            candidate_ids: set[str] = set()
            for feature in profile["identity"].lookup_terms:
                candidate_ids.update(self._feature_index.get(f"t:{feature}", set()))
            for candidate_id in sorted(candidate_ids - {topic_id}):
                pair = tuple(sorted((topic_id, candidate_id)))
                if pair in considered:
                    continue
                considered.add(pair)
                candidate = self._topic_event_profile(candidate_id)
                if not candidate or candidate["topic"]["visibility"] != "visible":
                    continue
                should_merge, reason = self._topics_share_continuous_subject(
                    profile, candidate, label_support, dollar_support
                )
                if not should_merge:
                    continue
                left_rank = (profile["event_score"], len(profile["participants"]), -dt(profile["topic"]["started_at"]).timestamp())
                right_rank = (candidate["event_score"], len(candidate["participants"]), -dt(candidate["topic"]["started_at"]).timestamp())
                target, source = (profile, candidate) if left_rank >= right_rank else (candidate, profile)
                request = self._merge_topic(source["topic_id"], target["topic_id"], at, reason)
                requests.append(request)
                topic_id = target["topic_id"]
                profile = self._topic_event_profile(topic_id)
                if profile:
                    queue.append(topic_id)
                break
        return requests

    @staticmethod
    def _relations(claim: Claim) -> list[str]:
        lower = claim.claim_text.lower()
        if any(signal in lower for signal in MECHANISM_SIGNALS):
            return ["mechanism"]
        relation = {
            "reported_fact": "progress", "official_statement": "progress", "reaction": "reaction",
            "interpretation": "attribution", "prediction": "future_effect", "promotion": "market_effect",
        }.get(claim.claim_kind, "other")
        if any(word in claim.action_or_issue for word in ("市场", "内存", "芯片")):
            return list(dict.fromkeys([relation, "market_effect"]))
        return [relation]

    @staticmethod
    def _membership_role(claim: Claim) -> str:
        return {"reaction": "reaction", "prediction": "prediction", "interpretation": "counterpoint"}.get(claim.claim_kind, "new_fact")

    def _ensure_thread(self, topic_id: str, relation: str, claim: Claim, at: str) -> str:
        labels = {
            "progress": "事件进展", "reaction": "社区反应", "attribution": "责任与解释",
            "market_effect": "市场影响", "future_effect": "未来影响", "mechanism": "产品机制", "other": "其他讨论",
        }
        thread_id = stable_id("thread", topic_id, relation)
        self.connection.execute(
            "INSERT OR IGNORE INTO threads VALUES (?,?,?,?,?,?,?,?,?,?)",
            (thread_id, topic_id, labels.get(relation, relation), claim.claim_text[:180], relation, at, at, 0, 0, "active"),
        )
        self.connection.execute(
            "UPDATE threads SET last_seen_at=?, source_count=(SELECT COUNT(DISTINCT claim_id) FROM memberships WHERE thread_id=?), "
            "independent_account_count=(SELECT COUNT(DISTINCT c.activity_account) FROM memberships m JOIN claims cl ON cl.claim_id=m.claim_id "
            "JOIN content_items c ON c.content_item_id=cl.content_item_id WHERE m.thread_id=?) WHERE thread_id=?",
            (at, thread_id, thread_id, thread_id),
        )
        return thread_id

    def _update_retrieval(self, topic_id: str, at: str) -> None:
        rows = self.connection.execute(
            "SELECT cl.claim_id, cl.claim_text, cl.entities_json, cl.content_item_id, ci.tweet_id, ci.created_at, "
            "ci.activity_account, ci.references_json "
            "FROM memberships m JOIN claims cl ON cl.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
            "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
            "WHERE m.topic_id=? AND m.superseded_by IS NULL AND COALESCE(r.decision,'support')='support' ORDER BY ci.created_at DESC", (topic_id,)
        ).fetchall()
        all_tokens: set[str] = set()
        all_keys: set[str] = set()
        all_entities: set[str] = set()
        asset_accounts: defaultdict[str, set[str]] = defaultdict(set)
        contract_accounts: defaultdict[str, set[str]] = defaultdict(set)
        claim_rows: list[str] = []
        for row in rows:
            all_tokens.update(tokens(row["claim_text"]))
            all_entities.update(json_loads(row["entities_json"], []))
            refs = json_loads(row["references_json"], {})
            if refs.get("quote_id"):
                all_keys.add(f"quote:{refs['quote_id']}")
            if refs.get("reply_to"):
                all_keys.add(f"reply:{refs['reply_to']}")
            all_keys.add(f"reply:{row['tweet_id']}")
            all_keys.add(f"quote:{row['tweet_id']}")
            all_keys.add(f"content:{row['tweet_id']}")
            for value in subject_tags(row["claim_text"]):
                asset_accounts[value].add(row["activity_account"])
            for value in RE_CONTRACT.findall(row["claim_text"]):
                contract_accounts[value.lower()].add(row["activity_account"])
            claim_rows.append(row["claim_id"])
        primary_assets = self._primary_identity_values(asset_accounts)
        primary_contracts = self._primary_identity_values(contract_accounts)
        all_keys.update(f"asset:{value}" for value in primary_assets)
        all_keys.update(f"contract:{value}" for value in primary_contracts)
        self.connection.execute(
            "INSERT INTO retrieval_state VALUES ('topic',?,?,?,?,?,?,?) ON CONFLICT(scope_type,scope_id) DO UPDATE SET "
            "tokens_json=excluded.tokens_json, hard_keys_json=excluded.hard_keys_json, entities_json=excluded.entities_json, "
            "representative_claim_ids_json=excluded.representative_claim_ids_json, recent_claim_ids_json=excluded.recent_claim_ids_json, updated_at=excluded.updated_at",
            (topic_id, json_dumps(sorted(all_tokens)), json_dumps(sorted(all_keys)), json_dumps(sorted(all_entities)),
             json_dumps(claim_rows[-4:]), json_dumps(claim_rows[:12]), at),
        )
        topic = self.connection.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        if topic:
            self._cache_topic(topic_id, {
                "topic_id": topic_id,
                "matching_status": topic["matching_status"],
                "event_or_issue": topic["event_or_issue"],
                "last_evidence_at": topic["last_evidence_at"],
                "started_at": topic["started_at"],
                "tokens": all_tokens,
                "hard_keys": all_keys,
                "entities": all_entities,
                "primary_assets": primary_assets,
                "primary_contracts": primary_contracts,
            })

    def _load_retrieval_cache(self) -> None:
        rows = self.connection.execute(
            "SELECT t.topic_id,t.matching_status,t.event_or_issue,t.last_evidence_at,t.started_at,"
            "r.tokens_json,r.hard_keys_json,r.entities_json FROM topics t JOIN retrieval_state r "
            "ON r.scope_type='topic' AND r.scope_id=t.topic_id"
        ).fetchall()
        for row in rows:
            self._cache_topic(row["topic_id"], {
                "topic_id": row["topic_id"], "matching_status": row["matching_status"],
                "event_or_issue": row["event_or_issue"], "last_evidence_at": row["last_evidence_at"],
                "started_at": row["started_at"], "tokens": set(json_loads(row["tokens_json"], [])),
                "hard_keys": set(json_loads(row["hard_keys_json"], [])), "entities": set(json_loads(row["entities_json"], [])),
            })

    def _reload_retrieval_cache(self) -> None:
        self._retrieval_cache.clear()
        self._feature_index.clear()
        self._load_retrieval_cache()

    def _cache_topic(self, topic_id: str, value: dict[str, Any]) -> None:
        old = self._retrieval_cache.get(topic_id)
        if old:
            for feature in old["tokens"]:
                self._feature_index[f"t:{feature}"].discard(topic_id)
            for feature in old["hard_keys"]:
                self._feature_index[f"k:{feature}"].discard(topic_id)
            for feature in old["entities"]:
                self._feature_index[f"e:{feature}"].discard(topic_id)
        self._retrieval_cache[topic_id] = value
        for feature in value["tokens"]:
            self._feature_index[f"t:{feature}"].add(topic_id)
        for feature in value["hard_keys"]:
            self._feature_index[f"k:{feature}"].add(topic_id)
        for feature in value["entities"]:
            self._feature_index[f"e:{feature}"].add(topic_id)

    def _refresh_all_topics(self, at: str) -> dict[str, Any]:
        rows = self.connection.execute("SELECT * FROM topics WHERE matching_status IN ('seed','active')").fetchall()
        transitions: dict[str, dict[str, str]] = {}
        affected: set[str] = set()
        for row in rows:
            old_status, old_visibility = row["matching_status"], row["visibility"]
            topic_id = row["topic_id"]
            events = self.connection.execute(
                "SELECT activity_account,last_participation_at FROM topic_participations WHERE topic_id=?", (topic_id,)
            ).fetchall()
            current = dt(at)
            assessment = self._assess_topic(topic_id, current)
            count_events = (
                [event for event in events if event["activity_account"] in assessment.qualified_accounts]
                if assessment.qualified_accounts
                else events
            )
            counts = []
            for hours in (1, 6, 24):
                cutoff = current - timedelta(hours=hours)
                counts.append(sum(1 for event in count_events if dt(event["last_participation_at"]) >= cutoff))
            latest = max((event["last_participation_at"] for event in events), default=None)
            status, visibility = old_status, old_visibility
            if old_status == "seed":
                if counts[2] >= MIN_VISIBLE_PARTICIPANTS and assessment.qualifies:
                    status, visibility = "active", "hidden"
                elif current >= dt(row["seed_expires_at"]):
                    status, visibility = "archived", "hidden"
            elif old_status == "active" and counts[2] < MIN_VISIBLE_PARTICIPANTS:
                status, visibility = "archived", "hidden"
            velocity = counts[0] / 1.0
            hotness = round(counts[0] * 1.0 + counts[1] * 0.5 + counts[2] * 0.25 + velocity * 0.1, 4)
            became_hotspot = counts[2] >= MIN_VISIBLE_PARTICIPANTS and assessment.qualifies
            archived_at = at if status == "archived" and old_status != "archived" else row["archived_at"]
            self.connection.execute(
                "UPDATE topics SET matching_status=?,visibility=?,last_participation_at=?,participant_count_1h=?,participant_count_6h=?,"
                "participant_count_24h=?,participant_velocity=?,hotness_score=?,archived_at=?,"
                "retention_tier=CASE WHEN ? THEN 'permanent' ELSE retention_tier END WHERE topic_id=?",
                (
                    status, visibility, latest, counts[0], counts[1], counts[2], velocity, hotness,
                    archived_at, int(became_hotspot), topic_id,
                ),
            )
            if status != old_status or visibility != old_visibility:
                transitions[topic_id] = {"from": f"{old_status}+{old_visibility}", "to": f"{status}+{visibility}"}
                affected.add(topic_id)
            cached = self._retrieval_cache.get(topic_id)
            if cached:
                cached["matching_status"] = status
        return {"transitions": transitions, "affected_topic_ids": sorted(affected)}

    def _assess_topic(self, topic_id: str, current: datetime) -> TopicAssessment:
        """Decide whether a coherent event exists without asking the brief renderer."""
        cutoff = (current - timedelta(hours=24)).isoformat()
        rows = self.connection.execute(
            "SELECT cl.*,ci.tweet_id,ci.activity_account,ci.references_json,ci.created_at FROM memberships m "
            "JOIN claims cl ON cl.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
            "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
            "WHERE m.topic_id=? AND m.superseded_by IS NULL AND COALESCE(r.decision,'support')='support' "
            "AND ci.created_at>=? GROUP BY cl.claim_id",
            (topic_id, cutoff),
        ).fetchall()
        all_accounts = {row["activity_account"] for row in rows}
        if not all_accounts:
            return TopicAssessment(frozenset(), 0.0, "", False)

        root_accounts: defaultdict[str, set[str]] = defaultdict(set)
        tag_accounts: defaultdict[str, set[str]] = defaultdict(set)
        contract_accounts: defaultdict[str, set[str]] = defaultdict(set)
        event_identity_accounts: defaultdict[str, set[str]] = defaultdict(set)
        for row in rows:
            references = json_loads(row["references_json"], {})
            for key in ("quote_id", "reply_to"):
                if references.get(key):
                    root_accounts[f"{key}:{references[key]}"].add(row["activity_account"])
            for tag in asset_labels(row["claim_text"]):
                tag_accounts[tag].add(row["activity_account"])
            for contract in RE_CONTRACT.findall(row["claim_text"]):
                contract_accounts[contract.lower()].add(row["activity_account"])
            identity = self._event_identity([row])
            names = sorted(identity.named_entities | identity.name_tokens)
            for index, left_name in enumerate(names):
                for right_name in names[index + 1:]:
                    event_identity_accounts[f"event:{left_name}|{right_name}"].add(row["activity_account"])
        accounts_by_tweet = {row["tweet_id"]: row["activity_account"] for row in rows}
        for root, accounts in root_accounts.items():
            root_tweet_id = root.split(":", 1)[1]
            root_account = accounts_by_tweet.get(root_tweet_id)
            if root_account:
                accounts.add(root_account)

        anchor_candidates = [(root, set(accounts)) for root, accounts in root_accounts.items()]
        anchor_candidates.extend((f"asset:{tag}", set(accounts)) for tag, accounts in tag_accounts.items())
        anchor_candidates.extend((f"contract:{contract}", set(accounts)) for contract, accounts in contract_accounts.items())
        if self._event_kinds(rows):
            anchor_candidates.extend((identity, set(accounts)) for identity, accounts in event_identity_accounts.items())
        anchor, qualified = max(
            anchor_candidates,
            key=lambda value: (len(value[1]), value[0].startswith(("asset:", "contract:", "event:")), value[0]),
            default=("", set()),
        )

        # An explicit $/# label establishes the stable subject.  Accounts that
        # use the same name in ordinary prose still belong to that Topic and
        # should count toward its coherence and lifecycle.
        if anchor.startswith("asset:"):
            subject = anchor.removeprefix("asset:")
            for row in rows:
                lower = row["claim_text"].lower()
                if (
                    subject in lower
                    if any("\u4e00" <= char <= "\u9fff" for char in subject)
                    else re.search(rf"(?<![a-z0-9_]){re.escape(subject)}(?![a-z0-9_])", lower)
                ):
                    qualified.add(row["activity_account"])

        coherence = len(qualified) / max(1, len(all_accounts))
        subject_keys = (
            set(anchor.removeprefix("asset:").removeprefix("contract:").removeprefix("event:").split("|"))
            if anchor.startswith(("asset:", "contract:", "event:"))
            else set()
        )
        substantive = any(
            row["activity_account"] in qualified
            and row["claim_kind"] != "reaction"
            and (
                any(signal in row["claim_text"].lower() for signal in EVENT_ASSERTION_SIGNALS)
                or self._substantive_score(dict(row), subject_keys) >= 2
            )
            for row in rows
        )
        return TopicAssessment(frozenset(qualified), round(coherence, 4), anchor, substantive)

    def _refresh_briefs(
        self,
        topic_ids: set[str],
        at: str,
        transitions: dict[str, dict[str, str]],
        *,
        force_topic_ids: set[str] | None = None,
        evidence_claim_ids: set[str] | None = None,
        editorial_guidance: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        current = dt(at)
        forced = force_topic_ids or set()
        for topic_id in sorted(topic_ids):
            row = self.connection.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
            if not row or row["matching_status"] != "active":
                continue
            retry_after = row["brief_retry_after"]
            if retry_after and current < dt(retry_after) and topic_id not in forced:
                requests.append(
                    {
                        "topic_id": topic_id,
                        "revision": self.connection.execute(
                            "SELECT COALESCE(MAX(revision), 0) + 1 AS revision "
                            "FROM brief_revisions WHERE topic_id=?",
                            (topic_id,),
                        ).fetchone()["revision"],
                        "reason": "retry_deferred",
                        "brief_status": "deferred",
                        "visibility": row["visibility"],
                        "retryable": True,
                        "retry_count": row["brief_retry_count"],
                        "retry_after": retry_after,
                    }
                )
                continue
            last_revision = self.connection.execute("SELECT MAX(revision) AS revision FROM brief_revisions WHERE topic_id=?", (topic_id,)).fetchone()["revision"] or 0
            last_generated = self.connection.execute("SELECT MAX(generated_at) AS generated_at FROM brief_revisions WHERE topic_id=?", (topic_id,)).fetchone()["generated_at"]
            transition = transitions.get(topic_id, {})
            if topic_id in forced:
                reason = "topic_merge"
            elif not last_revision:
                reason = "became_visible"
            elif transition.get("to", "").startswith("active+"):
                reason = "visibility_or_lifecycle_change"
            elif last_generated and current - dt(last_generated) < timedelta(minutes=15):
                continue
            else:
                reason = "ordinary_substantive_update"
            revision = last_revision + 1
            result = self._write_brief(
                topic_id, revision, at, reason, evidence_claim_ids=evidence_claim_ids,
                editorial_guidance=editorial_guidance,
            )
            # Do not hold a brief write transaction while generating a later
            # brief in this same batch.
            self.connection.commit()
            requests.append({"topic_id": topic_id, "revision": revision, "reason": reason, **result})
        return requests

    def _brief_generation_error(
        self,
        topic_id: str,
        at: str,
        error_code: str,
        error_message: str,
        *,
        retryable: bool = True,
    ) -> dict[str, Any]:
        topic = self.connection.execute(
            "SELECT visibility,brief_retry_count FROM topics WHERE topic_id=?", (topic_id,)
        ).fetchone()
        has_revision = self.connection.execute(
            "SELECT 1 FROM brief_revisions WHERE topic_id=? LIMIT 1", (topic_id,)
        ).fetchone() is not None
        # A failed refresh must not make an already published brief disappear.
        # For a first brief, keep the active topic hidden and mark the error so
        # the next batch can retry it explicitly.
        status = "ready" if has_revision else "error"
        previous_retry_count = int((topic["brief_retry_count"] if topic else 0) or 0)
        retry_count = previous_retry_count + 1 if retryable else 0
        retry_after = None
        if retryable:
            delay_minutes = min(
                BRIEF_RETRY_MAX_MINUTES,
                BRIEF_RETRY_BASE_MINUTES * (2 ** (retry_count - 1)),
            )
            retry_after = (dt(at) + timedelta(minutes=delay_minutes)).isoformat()
        self.connection.execute(
            "UPDATE topics SET brief_status=?,brief_error=?,brief_error_at=?,"
            "brief_retry_count=?,brief_retry_after=? WHERE topic_id=?",
            (status, error_message, at, retry_count, retry_after, topic_id),
        )
        return {
            "brief_status": "error",
            "visibility": topic["visibility"] if topic else "hidden",
            "error_code": error_code,
            "error_message": error_message,
            "retryable": retryable,
            "retry_count": retry_count,
            "retry_after": retry_after,
        }

    def _write_brief(
        self, topic_id: str, revision: int, at: str, reason: str, *,
        evidence_claim_ids: set[str] | None = None,
        editorial_guidance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        topic = self.connection.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        claims = self.connection.execute(
            "SELECT cl.*,ci.source_url,ci.tweet_id,ci.activity_account,ci.created_at,ci.references_json,"
            "COALESCE(r.decision,'support') AS semantic_decision FROM memberships m JOIN claims cl ON cl.claim_id=m.claim_id "
            "JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
            "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
            "WHERE m.topic_id=? AND m.superseded_by IS NULL AND COALESCE(r.decision,'support') IN ('support','context') "
            "GROUP BY cl.claim_id ORDER BY ci.created_at", (topic_id,)
        ).fetchall()
        claims = [dict(row) | {"context_only": row["semantic_decision"] == "context"} for row in claims]
        if evidence_claim_ids is not None:
            claims = [row for row in claims if row["claim_id"] in evidence_claim_ids]
        context_claims = [] if evidence_claim_ids is not None else self._nearby_context_claims(topic_id, topic, claims)
        subject_terms = asset_labels(topic["canonical_subject"])
        selected_claims = (
            sorted(claims, key=lambda row: row["created_at"])
            if evidence_claim_ids is not None
            else self._select_narrative_claims(list(claims), context_claims, subject_terms)
        )
        if not self.brief_writer:
            return self._brief_generation_error(
                topic_id,
                at,
                "ai_brief_writer_missing",
                "未配置 AI 正文生成器；不会生成本地规则正文。",
                retryable=False,
            )
        if not selected_claims:
            return self._brief_generation_error(
                topic_id,
                at,
                "ai_brief_evidence_missing",
                "没有可交给 AI 正文生成器的证据。",
                retryable=False,
            )
        try:
            if editorial_guidance and isinstance(self.brief_writer, ModelBriefWriter):
                payload = self.brief_writer(
                    self.connection, topic_id, at, selected_claims,
                    editorial_guidance=editorial_guidance,
                )
            else:
                payload = self.brief_writer(self.connection, topic_id, at, selected_claims)
        except Exception as exc:
            message = str(exc).strip() or "AI 正文生成器返回异常。"
            return self._brief_generation_error(
                topic_id, at, "ai_brief_generation_failed", f"{type(exc).__name__}: {message}"
            )
        if not isinstance(payload, dict):
            return self._brief_generation_error(
                topic_id, at, "ai_brief_invalid_response", "AI 正文生成器没有返回对象。"
            )
        title = compact(str(payload.get("title") or topic["working_title"]))
        text = str(payload.get("brief") or "").strip()
        if not text:
            return self._brief_generation_error(
                topic_id, at, "ai_brief_invalid_response", "AI 正文生成器返回了空正文。"
            )
        valid_ids = {row["claim_id"] for row in selected_claims}
        raw_source_ids = payload.get("source_claim_ids", [])
        if not isinstance(raw_source_ids, (list, tuple)):
            raw_source_ids = []
        source_claim_ids = [str(claim_id) for claim_id in raw_source_ids if str(claim_id) in valid_ids]
        if not source_claim_ids:
            source_claim_ids = [row["claim_id"] for row in selected_claims[:8]]

        used_ids = set(source_claim_ids)
        binding_rows: list[dict[str, Any]] = []
        seen_tweets: set[str] = set()
        selected_by_id = {row["claim_id"]: row for row in selected_claims}
        for claim_id in source_claim_ids:
            row = selected_by_id.get(claim_id)
            if not row or claim_id not in used_ids or row["tweet_id"] in seen_tweets:
                continue
            seen_tweets.add(row["tweet_id"])
            binding_rows.append(row)
        bindings = [
            {
                "claim_id": row["claim_id"],
                "tweet_id": row["tweet_id"],
                "url": row["source_url"],
                "contribution": self._clean_claim_text(row["claim_text"])[:160],
                "context_only": bool(row.get("context_only")),
            }
            for row in binding_rows[:8]
        ]
        content_hash = hashlib.sha256((title + "\n" + text).encode()).hexdigest()
        self.connection.execute(
            "INSERT OR REPLACE INTO brief_revisions VALUES (?,?,?,?,?,?,?,?,?)",
            (topic_id, revision, title, text, json_dumps(source_claim_ids), json_dumps(bindings), at, reason, content_hash),
        )
        self.connection.executemany(
            "INSERT OR IGNORE INTO topic_evidence(topic_id,claim_id) VALUES(?,?)",
            [(topic_id, claim_id) for claim_id in source_claim_ids],
        )
        self.connection.execute(
            "UPDATE topics SET visibility='visible',retention_tier='permanent',brief_status='ready',brief_error=NULL,brief_error_at=NULL,"
            "brief_retry_count=0,brief_retry_after=NULL "
            "WHERE topic_id=?",
            (topic_id,),
        )
        return {"brief_status": "ready", "visibility": "visible", "title": title, "brief": text}

    @staticmethod
    def _clean_claim_text(value: str) -> str:
        text = compact(value)
        quote_marker = re.search(r"引用\s+@[A-Za-z0-9_]{2,30}\s*[:：]?", text)
        if quote_marker and len(text[: quote_marker.start()].strip()) >= 12:
            text = text[: quote_marker.start()]
        text = re.sub(r"引用\s+@[A-Za-z0-9_]{2,30}\s*[:：]?", "", text)
        text = re.sub(r"https?://\S+", "", text)
        text = RE_CONTRACT.sub("", text)
        text = re.sub(r"\b(?:solana|ethereum|base|bsc):\S+", "", text, flags=re.I)
        for marker in ("关注，点赞", "关注点赞", "评论区留下", "抽50个人", "抽奖"):
            if marker in text:
                text = text.split(marker, 1)[0]
        if len(text) > 300:
            pieces = [piece.strip() for piece in re.split(r"[。！？!?；;]+", text) if piece.strip()]
            signal_words = (
                "涨", "跌", "拉盘", "爆仓", "爆多", "市值", "高点", "低点", "持续", "主力", "吸筹",
                "合约", "现货", "交易", "限制", "只能", "持仓", "流动性", "gateway", "tx.origin", "relay",
                *EVENT_FORMATION_SIGNALS, *MECHANISM_SIGNALS,
            )
            ranked = sorted(enumerate(pieces), key=lambda pair: (-sum(word in pair[1] for word in signal_words), pair[0]))
            chosen = sorted([piece for _, piece in ranked[:5]], key=lambda piece: text.find(piece))
            text = "；".join(chosen)
            if len(text) > 360:
                text = text[:357].rstrip("，。；; ") + "…"
        return compact(text).strip("；;，, ")

    @staticmethod
    def _claim_similarity(left: str, right: str) -> float:
        left_tokens, right_tokens = tokens(left), tokens(right)
        return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))

    def _nearby_context_claims(self, topic_id: str, topic: sqlite3.Row, own_claims: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
        """Fetch adjacent evidence for event context without changing membership.

        Only active/seed topics sharing a stable entity (cashtag, mention or
        contract) are eligible.  This lets a visible TUT topic see earlier
        price/analysis fragments that arrived as separate seeds, while keeping
        those fragments auditable as context-only evidence.
        """
        own_text = " ".join(row["claim_text"] for row in own_claims)
        stable = asset_labels(own_text)
        if not stable:
            stable = asset_labels(topic["working_title"]) | set(RE_MENTION.findall(topic["working_title"]))
        if not stable:
            stable = asset_labels(topic["canonical_subject"]) | set(RE_MENTION.findall(topic["canonical_subject"]))
        if not stable:
            return []
        rows = self.connection.execute(
            "SELECT t.topic_id,t.started_at,r.tokens_json FROM topics t JOIN retrieval_state r "
            "ON r.scope_type='topic' AND r.scope_id=t.topic_id "
            "WHERE t.topic_id<>? AND t.matching_status IN ('seed','active')",
            (topic_id,),
        ).fetchall()
        neighbor_scores: list[tuple[float, str]] = []
        for row in rows:
            old_tokens = set(json_loads(row["tokens_json"], []))
            overlap = stable & old_tokens
            if overlap:
                neighbor_scores.append((len(overlap), row["topic_id"]))
        neighbor_scores.sort(reverse=True)
        context: list[dict[str, Any]] = []
        own_ids = {row["claim_id"] for row in own_claims}
        for _, neighbor_id in neighbor_scores[:12]:
            rows = self.connection.execute(
                "SELECT cl.*,ci.source_url,ci.tweet_id,ci.activity_account,ci.created_at,ci.references_json FROM memberships m "
                "JOIN claims cl ON cl.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
                "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
                "WHERE m.topic_id=? AND m.superseded_by IS NULL AND COALESCE(r.decision,'support') IN ('support','context') "
                "GROUP BY cl.claim_id ORDER BY ci.created_at LIMIT 3", (neighbor_id,)
            ).fetchall()
            for row in rows:
                if row["claim_id"] not in own_ids:
                    context.append(dict(row) | {"context_only": True})
        return context[:16]

    def _select_narrative_claims(
        self,
        own_claims: list[sqlite3.Row],
        context_claims: list[dict[str, Any]],
        subject_terms: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Pick diverse, non-duplicate evidence for the AI writer.

        This is an evidence-size and provenance limit, not a decision about
        what kind of event the topic is or how the prose should be structured.
        The AI writer receives the resulting evidence and chooses the narrative
        itself.
        """
        rows = [dict(row) for row in own_claims] + context_claims
        subject_terms = {value.lower() for value in (subject_terms or set()) if value}
        if not subject_terms:
            subject_terms.update(
                asset
                for row in own_claims
                for asset in asset_labels(row["claim_text"])
            )
        # Context is allowed to add price or market background, but a claim
        # that explicitly names another asset is a separate narrative and must
        # not enter this topic just because its neighbor also mentioned QNT.
        if subject_terms:
            filtered: list[dict[str, Any]] = []
            for row in rows:
                row_assets = asset_labels(row["claim_text"])
                if row_assets and not row_assets & subject_terms:
                    continue
                filtered.append(row)
            rows = filtered
        citation_counts: Counter[str] = Counter()
        for row in rows:
            refs = json_loads(row.get("references_json"), {})
            for key in ("quote_id", "reply_to"):
                if refs.get(key):
                    citation_counts[str(refs[key])] += 1

        def provenance_rank(row: dict[str, Any]) -> tuple[bool, str, str]:
            refs = json_loads(row.get("references_json"), {})
            is_derived = bool(refs.get("quote_id") or refs.get("reply_to"))
            return is_derived, row.get("created_at", ""), row["claim_id"]

        rows.sort(key=provenance_rank)
        selected: list[dict[str, Any]] = []
        seen_accounts: set[str] = set()
        for row in rows:
            text = self._clean_claim_text(row["claim_text"])
            if len(text) < 5:
                continue
            if any(self._claim_similarity(text, prior["claim_text"]) >= 0.78 for prior in selected):
                continue
            # Prefer account diversity but do not discard the only available
            # event fact just because one account posted several updates.
            same_content_selected = any(
                prior.get("content_item_id") == row.get("content_item_id")
                for prior in selected
            )
            if row["activity_account"] in seen_accounts and len(selected) >= 16 and not same_content_selected:
                continue
            row["claim_text"] = text
            selected.append(row)
            seen_accounts.add(row["activity_account"])
        def priority(row: dict[str, Any]) -> tuple[float, bool, str, str]:
            # Keep the ranking generic: confidence, information value,
            # provenance and source support are useful for every topic shape.
            confidence = float(row.get("confidence") or 0.0)
            information_value = float(row.get("information_value") or 0.0)
            source_bonus = min(4, citation_counts[row.get("tweet_id", "")]) * 0.15
            length_bonus = min(len(row["claim_text"]) / 120.0, 1.0)
            context_penalty = 0.2 if row.get("context_only") else 0.0
            lower = row["claim_text"].lower()
            subject_bonus = 2.0 if any(term in lower for term in subject_terms) else 0.0
            score = confidence + information_value + source_bonus + length_bonus + subject_bonus - context_penalty
            return (-score, bool(row.get("context_only")), row.get("created_at", ""), row["claim_id"])

        ranked = sorted(selected, key=priority)
        result: list[dict[str, Any]] = []
        account_counts: Counter[str] = Counter()
        for row in ranked:
            if account_counts[row["activity_account"]] >= 4:
                continue
            result.append(row)
            account_counts[row["activity_account"]] += 1
            if len(result) >= 24:
                break
        return result

    @staticmethod
    def _substantive_score(row: dict[str, Any], subject_keys: set[str]) -> float:
        text = compact(row.get("claim_text"))
        lower = text.lower()
        signals = (
            "宣布", "发布", "推出", "上线", "关闭", "限制", "只能", "达到", "超过", "上涨", "下跌",
            "拉升", "爆拉", "回撤", "爆仓", "爆多", "市值", "交易", "合约", "现货", "持仓", "流动性",
            "教程", "步骤", "配置", "gateway", "transfer", "tx.origin", "relay", "because", "launch",
            "平台", "用户", "收入", "份额", "抢走", "迁移", "转移", "moved", "cold wallet",
            "transactions", "users", "listing", "memes", "correction", "bounce", "closing", "close",
            "shutdown", "moving", "explanation", "deadline", "serious", "头衔", "称呼", "身份", "变成",
            "发射台", "新台子", "注意力榜单", "空投", "销毁", "回购", "排名", "冲榜", "曝光",
            "launchpad", "airdrop", "burn", "ranking", "spotlight", "z500",
        )
        score = min(4, sum(signal.lower() in lower for signal in signals))
        score += 2 if any(key in lower for key in subject_keys) else 0
        score += 1 if re.search(r"\b\d+(?:\.\d+)?%?\b", RE_CONTRACT.sub("", text)) else 0
        score += 1 if row.get("action_or_issue") not in {"", "一般讨论", None} else 0
        score += 1 if row.get("claim_kind") in {"interpretation", "prediction"} else 0
        cashtags = asset_labels(text)
        score -= max(0, len(cashtags) - 1) * 2
        if any(marker in lower for marker in ("idk", "怎么/", "怎麼玩", "谁在车上", "join for free")):
            score -= 3
        if text.startswith("建议") and not any(signal in lower for signal in ("gateway", "tx.origin", "合约", "机制")):
            score -= 3
        if len(text) < 10:
            score -= 2
        return float(score)

    def _state_rows(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(query, params).fetchall()]

    def export_topics(self, *, visible_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM topics"
        params: tuple[Any, ...] = ()
        if visible_only:
            query += " WHERE visibility='visible'"
        query += " ORDER BY hotness_score DESC, started_at"
        result: list[dict[str, Any]] = []
        for topic in self.connection.execute(query, params).fetchall():
            topic_dict = dict(topic)
            topic_dict["core_entities"] = json_loads(topic_dict.pop("core_entities_json"), [])
            briefs = self.connection.execute(
                "SELECT * FROM brief_revisions WHERE topic_id=? ORDER BY revision DESC", (topic["topic_id"],)
            ).fetchall()
            topic_dict["briefs"] = []
            for row in briefs:
                brief = dict(row)
                brief["source_claim_ids"] = json_loads(brief["source_claim_ids_json"], [])
                brief["source_bindings"] = json_loads(brief["source_bindings_json"], [])
                topic_dict["briefs"].append(brief)
            topic_dict["threads"] = self._state_rows("SELECT * FROM threads WHERE topic_id=? ORDER BY first_seen_at", (topic["topic_id"],))
            topic_dict["participant_accounts"] = [row["activity_account"] for row in self.connection.execute("SELECT activity_account FROM topic_participations WHERE topic_id=? ORDER BY activity_account", (topic["topic_id"],))]
            result.append(topic_dict)
        return result

    def counts(self) -> dict[str, int]:
        return {key: self.connection.execute(f"SELECT COUNT(*) FROM {key}").fetchone()[0] for key in ("batches", "content_items", "claims", "topics", "threads", "memberships", "brief_revisions", "topic_evidence", "decision_audit")}


class TopicClaimReviewer:
    """Batch semantic gate for deterministic topic memberships.

    Interface: receive independent claim/topic cases and return one decision
    per case. The adapter owns model routing and HTTP only; SQLite caching,
    fallback-to-mechanical behaviour and participant rebuilding stay in
    ``TopicAggregator``.
    """

    def __init__(
        self,
        model: str = "gpt-5.6-luna",
        *,
        fallback_model: str = "gpt-5.6-luna",
        reasoning_effort: str = "high",
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 90.0,
        max_attempts: int = 2,
        retry_base_seconds: float = 1.0,
    ) -> None:
        self.model = model.strip()
        self.fallback_model = fallback_model.strip()
        self.reasoning_effort = reasoning_effort.strip() or "high"
        self.base_url = (
            base_url
            or os.environ.get("HOTTOPIC_CLAIM_REVIEW_OPENAI_BASE_URL", "")
            or os.environ.get("HOTTOPIC_OPENAI_BASE_URL", "")
            or os.environ.get("ODAILY_LLM_BASE_URL", "")
            or os.environ.get("X_PROCESS_OPENAI_BASE_URL", "")
        ).rstrip("/")
        self.api_key = api_key or self._default_api_key(self.base_url)
        self.timeout = float(timeout)
        self.max_attempts = int(max_attempts)
        self.retry_base_seconds = float(retry_base_seconds)
        if not self.model:
            raise ValueError("semantic review model is required")
        if self.timeout <= 0 or self.max_attempts < 1 or self.retry_base_seconds < 0:
            raise ValueError("invalid semantic review HTTP settings")
        if not self.api_key or not self.base_url:
            raise RuntimeError("semantic review requires a HotTopic-compatible API key and base URL")

    @staticmethod
    def _default_api_key(base_url: str) -> str:
        explicit = os.environ.get("HOTTOPIC_CLAIM_REVIEW_OPENAI_API_KEY", "")
        if explicit:
            return explicit
        if base_url.startswith(("http://127.0.0.1:", "http://localhost:", "https://127.0.0.1:", "https://localhost:")):
            return (
                os.environ.get("LITELLM_MASTER_KEY", "")
                or os.environ.get("ODAILY_LLM_API_KEY", "")
                or os.environ.get("OPENAI_API_KEY", "")
            )
        return os.environ.get("ODAILY_LLM_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")

    def __call__(self, cases: Sequence[dict[str, Any]]) -> Sequence[dict[str, Any]]:
        if not cases:
            return []
        prompt = self._prompt(cases)
        primary_error: Exception | None = None
        for model in dict.fromkeys((self.model, self.fallback_model)):
            if not model:
                continue
            try:
                parsed = self._call_model(model, prompt)
                expected_ids = {str(case["case_id"]) for case in cases}
                returned_ids = [str(row.get("case_id")) for row in parsed]
                if set(returned_ids) != expected_ids or len(returned_ids) != len(expected_ids):
                    raise ValueError("semantic review returned incomplete or duplicate case results")
                if any(row.get("decision") not in SEMANTIC_REVIEW_DECISIONS for row in parsed):
                    raise ValueError("semantic review returned an invalid decision")
                return [
                    {
                        **row,
                        "model": model,
                        "reasoning_effort": self.reasoning_effort,
                    }
                    for row in parsed
                ]
            except Exception as exc:
                if primary_error is None:
                    primary_error = exc
                continue
        assert primary_error is not None
        raise RuntimeError(
            f"HotTopic semantic review models failed: {type(primary_error).__name__}: {primary_error}"
        ) from primary_error

    def _prompt(self, cases: Sequence[dict[str, Any]]) -> str:
        topics: dict[str, dict[str, Any]] = {}
        claim_cases: list[dict[str, Any]] = []
        for case in cases:
            topic = case["topic"]
            claim = case["claim"]
            topic_key = str(topic.get("topic_id") or case["case_id"])
            topics.setdefault(
                topic_key,
                {
                    "title": topic["title"],
                    "canonical_subject": topic["canonical_subject"],
                    "core_entities": topic["core_entities"],
                    "event_or_issue": topic["event_or_issue"],
                    "representative_evidence": topic["evidence"][:6],
                },
            )
            claim_cases.append(
                {
                    "case_id": case["case_id"],
                    "topic_id": topic_key,
                    "claim": {
                        "account": claim.get("account", ""),
                        "created_at": claim.get("created_at", ""),
                        "kind": claim.get("kind", ""),
                        "entities": claim.get("entities", []),
                        "text": claim.get("text", ""),
                    },
                }
            )
        payload = {"topics": topics, "cases": claim_cases}
        return f"""
你是 X Agent 热点话题的语义复核器。确定性规则已经把每个 claim 放进一个候选话题；你的任务是逐条判断它是否真正属于该话题主线。

对每个 case 只返回一个分类：
- support：claim 讨论同一事件、公告、产品进展，或明确是该主线的独立市场反应；计入参与账号、热度和正文。
- context：claim 与主线有关，是背景、对比、价格环境或补充说明，但不是该话题的独立参与；不计入参与账号，可在正文需要时作为背景。
- unrelated：claim 实际讨论另一个事件/资产/争议，只因为共享 ticker、合约、平台、引用链或泛化实体而被机械归并；不计入参与账号和正文。

判定规则：
1. 按 claim 单独判断，不能因为同一账号的其他内容相关就放行；同一账号可以同时有三类结果。
2. 共享一个资产名、合约、平台或引用关系不是 support 的充分条件。优先比较事件、动作、时间、主体和材料是否指向同一主线。
3. 不要因为观点是预测、反对或质疑就判 unrelated；只要它明确围绕同一主线，仍可为 support 或 context。
4. 例如主线是 Aave 抵押品扩展时，单独讨论 HBM、Micron、NVIDIA 或 GPU 供需的 claim，即使共用了资产名或合约，也应判 unrelated；只有明确把这些内容用于解释 Aave 主线时才判 context 或 support。
5. 只依据输入材料，不补充外部事实。reason 用简短中文说明最关键的判断依据，confidence 为 0 到 1 的数字。

严格返回 JSON，不要 Markdown：
{{"reviews":[{{"case_id":"原样返回","decision":"support|context|unrelated","confidence":0.0,"reason":"简短中文原因"}}]}}

待复核材料：
{json.dumps(payload, ensure_ascii=False)}
""".strip()

    def _call_model(self, model: str, prompt: str) -> list[dict[str, Any]]:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "reasoning_effort": self.reasoning_effort,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        raw = self._post_json(request)
        content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or part.get("content") or "")
                for part in content if isinstance(part, dict)
            )
        match = re.search(r"\{.*\}", str(content), flags=re.S)
        if not match:
            raise ValueError("semantic review returned no JSON object")
        parsed = json.loads(match.group(0))
        reviews = parsed.get("reviews") if isinstance(parsed, dict) else parsed
        if not isinstance(reviews, list):
            raise ValueError("semantic review returned no reviews array")
        return [dict(row) for row in reviews if isinstance(row, dict)]

    def _post_json(self, request: urllib.request.Request) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("semantic review endpoint returned a non-object JSON response")
                return payload
            except Exception as exc:
                last_error = exc
                if attempt >= self.max_attempts or not self._is_retryable(exc):
                    break
                delay = self.retry_base_seconds * (2 ** (attempt - 1))
                if delay:
                    time.sleep(delay)
        assert last_error is not None
        raise RuntimeError(
            f"semantic review request failed after {attempt} attempts: "
            f"{type(last_error).__name__}: {last_error}"
        ) from last_error

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        if isinstance(error, urllib.error.HTTPError):
            return error.code in {408, 409, 425, 429} or 500 <= error.code < 600
        return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))


class TopicEventMergeReviewer(TopicClaimReviewer):
    """Use the existing model transport to compare two complete HotTopics."""

    def __call__(self, cases: Sequence[dict[str, Any]]) -> Sequence[dict[str, Any]]:
        prompt = self._prompt(cases)
        last_error: Exception | None = None
        for model in dict.fromkeys((self.model, self.fallback_model)):
            if not model:
                continue
            try:
                rows = self._call_model(model, prompt)
                expected = {case["case_id"] for case in cases}
                if len(rows) != len(expected) or {row.get("case_id") for row in rows} != expected:
                    raise ValueError("incomplete topic pair review")
                if any(row.get("decision") not in {"merge", "separate"} for row in rows):
                    raise ValueError("invalid topic pair decision")
                return [{**row, "model": model, "reasoning_effort": self.reasoning_effort} for row in rows]
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"topic merge models failed: {last_error}")

    def _prompt(self, cases: Sequence[dict[str, Any]]) -> str:
        return (
            "你是热点话题之间的真实事件复核器。判断每对话题是否为同一事件的连续阶段，"
            "例如事故、暂停提币、调查进展。社区反应和官方动作也可能属于同一事件。"
            "同一平台、资产、产品或普通词相同不足以归并；不同事故、发布或争议必须分开。"
            "只根据输入证据判断，不补充外部事实。不确定就 separate。"
            "严格返回 JSON 对象，reviews 数组每个 case 含 case_id、decision(merge|separate)、"
            "confidence(0到1)、reason(中文)、shared_event_fact(共同事件事实，分开时为空)、"
            "evidence_claim_ids(合并时两边各至少一条 claim_id)。\n"
            f"待复核话题对：{json.dumps(list(cases), ensure_ascii=False)}"
        )


class TopicVisibleMergeReviewer(TopicClaimReviewer):
    """Two-stage editorial review of the complete visible topic list."""

    def propose(self, cards: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        prompt = (
            "你是新闻编辑。请从全部热点卡片中找出可能属于同一个读者眼中的新闻话题的卡片组。"
            "优先按核心人物、具体行动、争议或公告判断；不同来源、原因、影响、技术细节和后续回应可以属于同一组，"
            "只要能写成一篇连贯且不失真的综合报道。仅同公司、资产、行业或会议但实际讨论不同独立事件时不要成组。"
            "这是初筛，宁可提出值得进一步核查的组，但不要把宏观行业话题混为一组。"
            "每组至少两条，允许重叠；只依据输入，不补充外部事实。"
            "严格返回 JSON：{\"reviews\":[{\"ids\":[\"原样话题id\"],\"reason\":\"共同具体主线\",\"uncertainty\":\"需核查的差异\"}]}。"
            "无候选则 reviews 为空数组。\n全部可见卡片："
            f"{json.dumps(list(cards), ensure_ascii=False)}"
        )
        return self._call_model(self.model, prompt)

    def review(self, cards: Sequence[dict[str, Any]]) -> dict[str, Any]:
        prompt = (
            "你是热点话题的复核编辑。判断这一组可见卡片能否合成一个读者眼中的热点，"
            "写成一篇连贯、准确的综合报道。细节和后续回应不同不阻止合并；"
            "若只是共同公司、资产、行业或会议，实际是不同独立事件则分开。"
            "必须逐个核对原始 claim，合并时为每个话题列出至少一个支持共同主线的 claim_id。"
            "若证据不足或不确定，输出 separate。只依据输入，不补充外部事实。"
            "严格返回 JSON：{\"reviews\":[{\"ids\":[\"原样话题id\"],"
            "\"decision\":\"merge|separate\",\"confidence\":0.0,\"reason\":\"简短理由\","
            "\"shared_fact\":\"共同的具体事实\",\"distinct_facts\":\"应保留的不同事实\","
            "\"risk\":\"误并或失真的风险\",\"evidence_claim_ids\":{\"话题id\":[\"claim_id\"]}}]}。"
            "\n待复核组："
            f"{json.dumps(list(cards), ensure_ascii=False)}"
        )
        reviews = self._call_model(self.model, prompt)
        if len(reviews) != 1 or set(reviews[0].get("ids", [])) != {card["id"] for card in cards}:
            raise ValueError("incomplete visible topic group review")
        if reviews[0].get("decision") not in {"merge", "separate"}:
            raise ValueError("invalid visible topic group decision")
        return reviews[0]


class ModelBriefWriter:
    """Evidence-grounded AI writer for visible topics.

    It receives selected evidence and writes the reader-facing narrative.  It
    does not choose from event-specific templates, and the caller never falls
    back to local prose when this adapter fails.
    """

    def __init__(
        self,
        model: str = "gpt-5.6-luna",
        *,
        fallback_model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_attempts: int = 3,
        retry_base_seconds: float = 2.0,
    ) -> None:
        self.model = model
        self.fallback_model = (
            fallback_model
            if fallback_model is not None
            else os.environ.get("HOTTOPIC_FALLBACK_MODEL", "gpt-5.6-luna")
        ).strip()
        self.base_url = (
            base_url
            or os.environ.get("HOTTOPIC_OPENAI_BASE_URL", "")
            or os.environ.get("ODAILY_LLM_BASE_URL", "")
            # Compatibility only for existing deployments. New HotTopic/X
            # Agent configuration never needs the quick-news variable.
            or os.environ.get("X_PROCESS_OPENAI_BASE_URL", "")
        ).rstrip("/")
        self.api_key = api_key or self._default_api_key(self.base_url)
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        if self.timeout <= 0:
            raise ValueError("model writer timeout must be positive")
        if self.max_attempts < 1:
            raise ValueError("model writer max_attempts must be at least 1")
        if self.retry_base_seconds < 0:
            raise ValueError("model writer retry_base_seconds cannot be negative")
        if not self.api_key or not self.base_url:
            raise RuntimeError("model writer requires a HotTopic-compatible API key and base URL")

    @staticmethod
    def _default_api_key(base_url: str) -> str:
        """Use the local proxy credential only when HotTopic targets LiteLLM."""
        explicit = os.environ.get("HOTTOPIC_OPENAI_API_KEY", "")
        if explicit:
            return explicit
        if base_url.startswith(("http://127.0.0.1:", "http://localhost:", "https://127.0.0.1:", "https://localhost:")):
            return (
                os.environ.get("LITELLM_MASTER_KEY", "")
                or os.environ.get("ODAILY_LLM_API_KEY", "")
                or os.environ.get("OPENAI_API_KEY", "")
            )
        return os.environ.get("ODAILY_LLM_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")

    def __call__(
        self, connection: sqlite3.Connection, topic_id: str, at: str,
        evidence: Sequence[dict[str, Any]], *, editorial_guidance: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        topic = connection.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        payload = [
            {
                "claim_id": row["claim_id"],
                "created_at": row.get("created_at", ""),
                "context_only": bool(row.get("context_only")),
                "text": row["claim_text"],
                "tweet_id": row["tweet_id"],
            }
            for row in evidence[:18]
        ]
        merge_guidance = ""
        if editorial_guidance:
            merge_guidance = (
                "\n本次已确认两个话题可合为一篇报道。请在材料支持的范围内同时写出共同主线和两侧的独有信息，"
                "明确区分已发生事实、计划上线和评论判断；不要把审查文字本身当作事实。"
                "正文须实际使用来自每个原话题的证据，并在 source_claim_ids 中包含每侧至少一个 claim_id。\n"
                f"共同主线：{compact(editorial_guidance.get('shared_fact'))}\n"
                f"必须保留的差异：{compact(editorial_guidance.get('distinct_facts'))}\n"
                f"失真风险：{compact(editorial_guidance.get('risk'))}\n"
                f"各话题证据 ID：{json.dumps(editorial_guidance.get('evidence_claim_ids', {}), ensure_ascii=False)}\n"
            )
        prompt = f"""
你是 HotTopic 的中文热点编辑。请把给定信息点写成一篇让终极读者获得有效信息的热点正文。

话题暂定标题：{topic['working_title']}

请根据材料自己决定叙事顺序和段落重点，不要把话题归入预设事件类型，也不要套用事件分类对应的写法。输出连续、自然、叙事化的中文正文，而不是信息点清单、表格或固定栏目。

通用写作要求：
1. 标题概括主线，正文第一句直接给出读者最需要知道的有效信息，不重复标题，不照抄原帖。
2. 把同一主线的多条内容合成连贯叙述；只有材料支持时才补充背景、时间顺序、机制、影响或后续，不强行凑出完整结构。
3. 合并重复表达和相似情绪，不逐条罗列谁说了什么；保留能改变理解的数字、术语、动作、结果和分歧。
4. 分析、预测、归因和押注必须贴着具体账号、机构或来源表达，不能改写成已经证实的事实。
5. 优先使用“明确主体 + 强动作动词 + 对象或结果”的主动句，少用空泛连接词和总结套话。
6. 只写材料中的事实和来源明确表达的观点，不要加入编辑者自己的判断、免责声明或结论性提醒；不要写“上述判断……并非已兑现”“不构成确定性结论”“这一说法来自市场参与者的解读”“风险已经上升”等编辑者总结。信息不足时直接写短，不用空话补长度。
7. 同一行情事实只保留一次；不同来源的数字只有在口径、时间或来源明确不同且对读者有增量信息时才并列，否则合并或舍弃重复数字。
8. 不出现 Topic、claim、source、context、聚类、监控账号等后台词，不新增材料之外的事实、因果、动机或数字；外文来源要准确转写为自然中文，专有名词、代码和数字保持原样。
9. 正文只使用自然段，不使用项目符号、编号清单、表格或“事实/分析/反应”等固定栏目标题。若材料包含明显不同的叙事层次，必须分成 2 至 4 个自然段：例如先写事件背景或前因，再写价格、市值、交易量等数据变化，最后写社区观点、争议或分歧；只有材料单一且很短时才使用一个自然段。每段围绕一个主要问题展开，不要为了分段切碎同一条因果链。

严格输出 JSON：{{"title":"准确具体的新闻式标题","brief":"信息密度高的自然中文正文","source_claim_ids":["实际使用的 claim_id"]}}
{merge_guidance}

信息点：
{json.dumps(payload, ensure_ascii=False)}
""".strip()
        try:
            draft = self._write_with_model(self.model, prompt, topic, evidence)
        except Exception as primary_error:
            if not self.fallback_model or self.fallback_model == self.model:
                raise
            try:
                draft = self._write_with_model(self.fallback_model, prompt, topic, evidence)
            except Exception as fallback_error:
                raise RuntimeError(
                    f"HotTopic brief models failed: primary={type(primary_error).__name__}: {primary_error}; "
                    f"fallback={type(fallback_error).__name__}: {fallback_error}"
                ) from fallback_error
        if not editorial_guidance:
            return draft
        revision_prompt = (
            "你是热点报道的终审编辑。下面的草稿来自两个已确认可合并的话题。"
            "保留草稿中有证据支持的主线和时间口径，并补齐草稿遗漏的、由原始证据支持的另一侧独有事实或观点。"
            "特别逐项检查‘必须保留的差异’；观点必须明确归因，计划不得写成已经全面上线。"
            "不要加入编辑者免责声明或审查过程文字。输出自然中文正文，不用清单。"
            "严格返回 JSON 对象，包含 title、brief、source_claim_ids；引用实际使用的两侧 claim_id。\n"
            f"草稿：{json.dumps(draft, ensure_ascii=False)}\n"
            f"共同事实：{compact(editorial_guidance.get('shared_fact'))}\n"
            f"必须保留的差异：{compact(editorial_guidance.get('distinct_facts'))}\n"
            f"失真风险：{compact(editorial_guidance.get('risk'))}\n"
            f"两侧证据 ID：{json.dumps(editorial_guidance.get('evidence_claim_ids', {}), ensure_ascii=False)}\n"
            f"原始证据：{json.dumps(payload, ensure_ascii=False)}"
        )
        return self._write_with_model(self.model, revision_prompt, topic, evidence)

    def _write_with_model(
        self,
        model: str,
        prompt: str,
        topic: sqlite3.Row,
        evidence: Sequence[dict[str, Any]],
    ) -> dict[str, str]:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "reasoning_effort": "none",
                }
            ).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        raw = self._post_json(request)
        content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = self._parse_json(content)
        valid_ids = {row["claim_id"] for row in evidence}
        source_ids = [str(value) for value in parsed.get("source_claim_ids", []) if str(value) in valid_ids]
        if not source_ids:
            source_ids = [row["claim_id"] for row in evidence[:8]]
        return {
            "title": compact(parsed.get("title") or topic["working_title"]),
            "brief": self._clean_generated_brief(str(parsed.get("brief") or "")),
            "source_claim_ids": source_ids,
        }

    @staticmethod
    def _clean_generated_brief(value: str) -> str:
        """Remove recurring editor meta-text without rewriting source facts."""
        text = value.strip()
        text = re.sub(
            r"(?:^|(?<=[。！？!?])|\n)\s*上述判断属于.*?并非已兑现的结果[。.!！]?\s*",
            "",
            text,
        )
        text = re.sub(
            r"[，,]\s*(?:但\s*)?(?:这些内容|上述内容|上述说法)并不构成确定性结论[。.!！]?",
            "。",
            text,
        )
        text = re.sub(r"(?:^|\n)\s*(?:这些内容|上述内容|上述说法)并不构成确定性结论[。.!！]?\s*", "", text)
        text = re.sub(
            r"[，,]\s*(?:但\s*)?(?:这一|该)(?:说法|观点|判断)(?:来自|属于)(?:市场参与者|交易者|社群|部分人士|业内人士|外界)(?:的|对[^。！？!?]{0,20}的)?(?:解读|分析|看法|观点|判断)[。.!！]?",
            "。",
            text,
        )

        paragraphs: list[str] = []
        seen: set[str] = set()
        for paragraph in re.split(r"\n+", text):
            unique: list[str] = []
            for sentence in re.split(r"(?<=[。！？!?])\s*", paragraph):
                normalized = compact(sentence)
                if not normalized:
                    continue
                key = re.sub(r"[。！？!?]$", "", normalized)
                if key in seen:
                    continue
                seen.add(key)
                unique.append(normalized)
            if unique:
                paragraphs.append("".join(unique))
        return "\n".join(paragraphs).strip()

    def _post_json(self, request: urllib.request.Request) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("model endpoint returned a non-object JSON response")
                return payload
            except Exception as exc:
                last_error = exc
                if attempt >= self.max_attempts or not self._is_retryable(exc):
                    break
                delay = self.retry_base_seconds * (2 ** (attempt - 1))
                if delay:
                    time.sleep(delay)
        assert last_error is not None
        raise RuntimeError(
            f"model request failed after {attempt} attempts: "
            f"{type(last_error).__name__}: {last_error}"
        ) from last_error

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        if isinstance(error, urllib.error.HTTPError):
            return error.code in {408, 409, 425, 429} or 500 <= error.code < 600
        return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        match = re.search(r"\{.*\}", content, flags=re.S)
        if not match:
            raise ValueError("model writer returned no JSON object")
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict) or not str(parsed.get("brief") or "").strip():
            raise ValueError("model writer returned an incomplete brief")
        return parsed

__all__ = ["Claim", "ContentItem", "Candidate", "ModelBriefWriter", "TopicClaimReviewer", "TopicAggregator"]
