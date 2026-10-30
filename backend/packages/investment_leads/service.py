from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import os
import sqlite3
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .analysis import (
    DERIVE_PROMPT_VERSION,
    IDENTITY_SEARCH_PROMPT_VERSION,
    SCAN_PROMPT_VERSION,
    InvestmentLeadAnalyzer,
    ModelOutput,
)
from .schema import ensure_schema

MAX_ATTEMPTS = 3
LEASE = timedelta(minutes=30)
RETRY_DELAY = timedelta(minutes=15)
RETENTION = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(UTC).isoformat()


def _utc_date() -> str:
    return _now().date().isoformat()


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name) or default)
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _decoded(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def _text_hash(material: dict[str, Any]) -> str:
    identity = {
        "id": material.get("material_id"),
        "author": material.get("author_screen_name"),
        "text": material.get("text"),
        "url": material.get("url"),
        "links": material.get("links"),
        "attachments": material.get("attachments"),
    }
    return hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "head"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "head"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth and data.strip():
            self.parts.append(data.strip())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _request, _fp, _code, _msg, _headers, _new_url):
        return None


def _public_web_url(value: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            return None
        host = parsed.hostname.rstrip(".").lower()
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            addresses = [
                ipaddress.ip_address(result[4][0].split("%", 1)[0])
                for result in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            ]
        if not addresses or any(not address.is_global for address in addresses):
            return None
        return urllib.parse.urlunsplit(parsed)
    except (OSError, ValueError):
        return None


def _read_public_page(url: str) -> dict[str, str] | None:
    safe_url = _public_web_url(url)
    if not safe_url:
        return None
    request = urllib.request.Request(
        safe_url,
        headers={"User-Agent": "OdAIly-X-Agent-Investment-Leads/1.0", "Accept": "text/html,text/plain"},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            content_type = response.headers.get_content_type().lower()
            if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                return None
            body = response.read(256_001)
            if len(body) > 256_000:
                body = body[:256_000]
            encoding = response.headers.get_content_charset() or "utf-8"
            decoded = body.decode(encoding, errors="replace")
            if content_type == "text/plain":
                text = " ".join(decoded.split())
            else:
                parser = _VisibleTextParser()
                parser.feed(decoded)
                text = " ".join(html.unescape(part) for part in parser.parts)
            text = " ".join(text.split())[:4_000]
            if not text:
                return None
            return {"url": safe_url, "text": text}
    except (OSError, TimeoutError, urllib.error.URLError, ValueError):
        return None


def persist_capture_materials(
    connection: sqlite3.Connection,
    *,
    account_lower: str,
    materials: list[dict[str, Any]],
    observed_at: str,
) -> int:
    """Persist one FXTwitter response, deduping source text while retaining each observer."""
    if not materials:
        return 0
    capture = connection.execute(
        "SELECT m.capture_started_at,a.created_at,a.investment_leads_enabled_at "
        "FROM investment_leads_meta m JOIN hottopic_accounts a ON a.screen_name_lower=? "
        "WHERE m.singleton_key='global'",
        (account_lower,),
    ).fetchone()
    if capture is None or not capture["investment_leads_enabled_at"]:
        return 0
    try:
        capture_cutoff = datetime.fromisoformat(str(capture["capture_started_at"]))
        enabled_at = datetime.fromisoformat(str(capture["investment_leads_enabled_at"]))
        created_at = datetime.fromisoformat(str(capture["created_at"]))
        # A newly added account gets one bounded 24-hour warm start. Later
        # re-enables begin exactly at the new switch time and do not replay.
        account_cutoff = enabled_at - timedelta(hours=24) if enabled_at == created_at else enabled_at
        eligible_from = max(capture_cutoff, account_cutoff)
    except (TypeError, ValueError):
        return 0
    related = [item for item in materials if item.get("role") != "source_post"]
    saved = 0
    for original in materials:
        material = dict(original)
        if material.get("role") == "source_post":
            material["related_materials"] = related[:4]
        material_id = str(material.get("material_id") or "").strip()
        if not material_id:
            continue
        if material.get("role") == "source_post":
            activity_at = (
                (material.get("activity_at") if material.get("activity_time_complete") else material.get("created_at_iso"))
                if material.get("relation") == "repost" else material.get("created_at_iso")
            )
            try:
                if not activity_at or datetime.fromisoformat(str(activity_at)) < eligible_from:
                    continue
            except ValueError:
                continue
        content_hash = _text_hash(material)
        relation = str(material.get("relation") or "original")
        completeness = "incomplete" if not material.get("text") else "complete"
        payload_material = dict(material)
        if completeness == "incomplete":
            payload_material["completeness"] = "incomplete"
        existing = connection.execute(
            "SELECT payload_json FROM investment_lead_materials WHERE material_id=? AND content_hash=?",
            (material_id, content_hash),
        ).fetchone()
        if existing is not None:
            prior = _decoded(existing["payload_json"], {})
            preserved = {key: prior[key] for key in ("supplement_materials", "supplement_attempted", "supplemented_urls") if key in prior}
            prior_related = prior.get("related_materials") if isinstance(prior.get("related_materials"), list) else []
            next_related = payload_material.get("related_materials") if isinstance(payload_material.get("related_materials"), list) else []
            merged_related = {
                (str(item.get("material_id") or ""), str(item.get("role") or "")): item
                for item in [*prior_related, *next_related] if isinstance(item, dict)
            }
            payload_material = {**prior, **payload_material, **preserved}
            if merged_related:
                payload_material["related_materials"] = list(merged_related.values())[:4]
        connection.execute(
            "INSERT INTO investment_lead_materials("
            "material_id,content_hash,posted_at,activity_at,activity_time_complete,relation,payload_json,"
            "first_observed_at,last_observed_at,completeness"
            ") VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(material_id,content_hash) DO UPDATE SET "
            "posted_at=COALESCE(excluded.posted_at,investment_lead_materials.posted_at),"
            "activity_at=COALESCE(excluded.activity_at,investment_lead_materials.activity_at),"
            "activity_time_complete=MAX(excluded.activity_time_complete,investment_lead_materials.activity_time_complete),"
            "relation=excluded.relation,last_observed_at=excluded.last_observed_at",
            (
                material_id, content_hash, material.get("created_at_iso"), material.get("activity_at"),
                int(bool(material.get("activity_time_complete"))), relation, _json(payload_material), observed_at,
                observed_at, completeness,
            ),
        )
        connection.execute(
            "UPDATE investment_lead_materials SET payload_json=? WHERE material_id=? AND content_hash=?",
            (_json(payload_material), material_id, content_hash),
        )
        connection.execute(
            "INSERT INTO investment_lead_observations("
            "material_id,content_hash,screen_name_lower,relation,first_observed_at,last_observed_at"
            ") VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(material_id,content_hash,screen_name_lower,relation) DO UPDATE SET "
            "last_observed_at=excluded.last_observed_at",
            (material_id, content_hash, account_lower, relation, observed_at, observed_at),
        )
        if material.get("role") == "source_post":
            cursor = connection.execute(
                "INSERT OR IGNORE INTO investment_lead_tasks("
                "material_id,content_hash,stage,prompt_version,status,created_at,updated_at"
                ") VALUES(?,?, 'scan', ?, 'pending', ?, ?)",
                (material_id, content_hash, SCAN_PROMPT_VERSION, observed_at, observed_at),
            )
            connection.execute(
                "UPDATE investment_lead_tasks SET status='pending',attempts=0,next_attempt_at=NULL,"
                "lease_until=NULL,last_error=NULL,completed_at=NULL,updated_at=? "
                "WHERE material_id=? AND content_hash=? AND status='ignored'",
                (observed_at, material_id, content_hash),
            )
            connection.execute(
                "UPDATE investment_lead_identity_searches SET status='pending',attempts=0,next_attempt_at=NULL,"
                "lease_until=NULL,last_error=NULL,completed_at=NULL,updated_at=? "
                "WHERE status='ignored' AND lead_id IN (SELECT lead_id FROM investment_lead_sources "
                "WHERE material_id=? AND content_hash=?)",
                (observed_at, material_id, content_hash),
            )
            saved += cursor.rowcount
    return saved


def cancel_ineligible_jobs(connection: sqlite3.Connection, *, account_lower: str, now: str) -> int:
    """Cancel queued work only when no other enabled directory account observed it."""
    material_jobs = connection.execute(
        "UPDATE investment_lead_tasks SET status='ignored',next_attempt_at=NULL,lease_until=NULL,last_error=NULL,"
        "completed_at=?,updated_at=? WHERE status IN ('pending','failed','budget_paused') AND EXISTS("
        "SELECT 1 FROM investment_lead_observations target WHERE target.material_id=investment_lead_tasks.material_id "
        "AND target.content_hash=investment_lead_tasks.content_hash AND target.screen_name_lower=?"
        ") AND NOT EXISTS(SELECT 1 FROM investment_lead_observations other "
        "JOIN hottopic_accounts a ON a.screen_name_lower=other.screen_name_lower "
        "WHERE other.material_id=investment_lead_tasks.material_id AND other.content_hash=investment_lead_tasks.content_hash "
        "AND a.status='followed' AND a.investment_leads_enabled=1)",
        (now, now, account_lower),
    ).rowcount
    identity_jobs = connection.execute(
        "UPDATE investment_lead_identity_searches SET status='ignored',next_attempt_at=NULL,lease_until=NULL,last_error=NULL,"
        "completed_at=?,updated_at=? WHERE status IN ('pending','failed','budget_paused') AND EXISTS("
        "SELECT 1 FROM investment_lead_sources ls JOIN investment_lead_observations target "
        "ON target.material_id=ls.material_id AND target.content_hash=ls.content_hash "
        "WHERE ls.lead_id=investment_lead_identity_searches.lead_id AND target.screen_name_lower=?"
        ") AND NOT EXISTS(SELECT 1 FROM investment_lead_sources ls JOIN investment_lead_observations other "
        "ON other.material_id=ls.material_id AND other.content_hash=ls.content_hash JOIN hottopic_accounts a "
        "ON a.screen_name_lower=other.screen_name_lower WHERE ls.lead_id=investment_lead_identity_searches.lead_id "
        "AND a.status='followed' AND a.investment_leads_enabled=1)",
        (now, now, account_lower),
    ).rowcount
    return material_jobs + identity_jobs


class InvestmentLeadService:
    def __init__(
        self,
        database_path: Path,
        *,
        analyzer: InvestmentLeadAnalyzer | None = None,
        connection: sqlite3.Connection | None = None,
        reader_only: bool = False,
    ) -> None:
        self.path = database_path
        self._owns_connection = connection is None
        self.db = connection or _open_database(database_path)
        if not reader_only:
            ensure_schema(self.db, capture_started_at=_iso())
        self.batch_size = _env_int("X_AGENT_INVESTMENT_LEADS_BATCH_SIZE", 20, 1, 20)
        self.flush_seconds = _env_int("X_AGENT_INVESTMENT_LEADS_BATCH_WAIT_SECONDS", 60, 0, 300)
        self.daily_input_tokens = _env_int("X_AGENT_INVESTMENT_LEADS_DAILY_INPUT_TOKENS", 500_000, 0, 100_000_000)
        self.daily_derive_calls = _env_int("X_AGENT_INVESTMENT_LEADS_DAILY_DERIVE_CALLS", 2_000, 0, 100_000)
        self.daily_supplement_reads = _env_int("X_AGENT_INVESTMENT_LEADS_DAILY_SUPPLEMENT_READS", 100, 0, 10_000)
        self.daily_identity_searches = _env_int("X_AGENT_INVESTMENT_LEADS_DAILY_SEARCH_CALLS", 50, 0, 10_000)
        self.last_error: str | None = None
        self.analyzer_error: str | None = None
        if reader_only:
            self.analyzer = None
        elif analyzer is None:
            try:
                analyzer = InvestmentLeadAnalyzer()
            except RuntimeError as exc:
                self.analyzer_error = f"{type(exc).__name__}: {exc}"
            self.analyzer = analyzer
        else:
            self.analyzer = analyzer

    def close(self) -> None:
        if self._owns_connection:
            self.db.close()

    def process_once(self) -> dict[str, int]:
        now = _iso()
        self._heartbeat(now)
        self._reclaim_stale(now)
        self._resume_budget_paused(now)
        scan_count = self._process_scan_batch(now)
        derive_count = self._process_one_derive(now)
        supplement_count = self._process_one_supplement(now)
        identity_search_count = self._process_one_identity_search(now)
        self._maintain(now)
        return {
            "scanned": scan_count, "derived": derive_count, "supplemented": supplement_count,
            "identitySearched": identity_search_count,
        }

    def _heartbeat(self, now: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE investment_leads_meta SET worker_last_seen_at=?,model_error=? WHERE singleton_key='global'",
                (now, self.analyzer_error),
            )

    def _reclaim_stale(self, now: str) -> None:
        cutoff = _iso(datetime.fromisoformat(now) - LEASE)
        with self.db:
            self.db.execute(
                "UPDATE investment_lead_tasks SET status='failed',lease_until=NULL,last_error=COALESCE(last_error,'任务租约过期'),"
                "completed_at=?,updated_at=? WHERE status='processing' AND attempts>=? AND updated_at<?",
                (now, now, MAX_ATTEMPTS, cutoff),
            )
            self.db.execute(
                "UPDATE investment_lead_tasks SET status='pending',lease_until=NULL,"
                "last_error=COALESCE(last_error,'任务租约过期，已重新排队'),updated_at=? "
                "WHERE status='processing' AND attempts<? AND updated_at<?",
                (now, MAX_ATTEMPTS, cutoff),
            )
            self.db.execute(
                "UPDATE investment_lead_identity_searches SET status='failed',lease_until=NULL,last_error=COALESCE(last_error,'任务租约过期'),"
                "completed_at=?,updated_at=? WHERE status='processing' AND attempts>=? AND updated_at<?",
                (now, now, MAX_ATTEMPTS, cutoff),
            )
            self.db.execute(
                "UPDATE investment_lead_identity_searches SET status='pending',lease_until=NULL,"
                "last_error=COALESCE(last_error,'任务租约过期，已重新排队'),updated_at=? "
                "WHERE status='processing' AND attempts<? AND updated_at<?",
                (now, MAX_ATTEMPTS, cutoff),
            )

    def _resume_budget_paused(self, now: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE investment_lead_tasks SET status='pending',updated_at=? "
                "WHERE status='budget_paused' AND stage IN ('scan','supplement') AND substr(updated_at,1,10)<?",
                (now, _utc_date()),
            )
            self.db.execute(
                "UPDATE investment_lead_tasks SET status='pending',updated_at=? "
                "WHERE status='budget_paused' AND stage='derive' AND substr(updated_at,1,10)<?",
                (now, _utc_date()),
            )
            self.db.execute(
                "UPDATE investment_lead_identity_searches SET status='pending',updated_at=? "
                "WHERE status='budget_paused' AND substr(updated_at,1,10)<?",
                (now, _utc_date()),
            )

    def _eligible(self, material_id: str, content_hash: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM investment_lead_observations o JOIN hottopic_accounts a "
            "ON a.screen_name_lower=o.screen_name_lower "
            "WHERE o.material_id=? AND o.content_hash=? AND a.status='followed' "
            "AND a.investment_leads_enabled=1 LIMIT 1",
            (material_id, content_hash),
        ).fetchone() is not None

    def _next_fair_account(self, stage: str, now: str) -> str | None:
        cursor = self.db.execute(
            "SELECT account_lower FROM investment_lead_scheduler_cursor WHERE stage=?", (stage,)
        ).fetchone()
        last_account = str(cursor[0]) if cursor else ""
        if stage == "identity_search":
            row = self.db.execute(
                "SELECT a.screen_name_lower FROM investment_lead_identity_searches s "
                "JOIN investment_lead_sources ls ON ls.lead_id=s.lead_id "
                "JOIN investment_lead_observations o ON o.material_id=ls.material_id AND o.content_hash=ls.content_hash "
                "JOIN hottopic_accounts a ON a.screen_name_lower=o.screen_name_lower "
                "WHERE s.status='pending' AND s.attempts<? AND (s.next_attempt_at IS NULL OR s.next_attempt_at<=?) "
                "AND a.status='followed' AND a.investment_leads_enabled=1 GROUP BY a.screen_name_lower "
                "ORDER BY CASE WHEN a.screen_name_lower>? THEN 0 ELSE 1 END,a.screen_name_lower LIMIT 1",
                (MAX_ATTEMPTS, now, last_account),
            ).fetchone()
        else:
            conditions = ""
            params: list[Any] = [stage, MAX_ATTEMPTS, now]
            if stage == "derive":
                conditions = "AND EXISTS(SELECT 1 FROM investment_lead_events e WHERE e.material_id=t.material_id " \
                    "AND e.content_hash=t.content_hash AND e.prompt_version=?) "
                params.append(SCAN_PROMPT_VERSION)
            params.append(last_account)
            row = self.db.execute(
                "SELECT a.screen_name_lower FROM investment_lead_tasks t "
                "JOIN investment_lead_observations o ON o.material_id=t.material_id AND o.content_hash=t.content_hash "
                "JOIN hottopic_accounts a ON a.screen_name_lower=o.screen_name_lower "
                "WHERE t.stage=? AND t.status='pending' AND t.attempts<? "
                "AND (t.next_attempt_at IS NULL OR t.next_attempt_at<=?) " + conditions +
                "AND a.status='followed' AND a.investment_leads_enabled=1 GROUP BY a.screen_name_lower "
                "ORDER BY CASE WHEN a.screen_name_lower>? THEN 0 ELSE 1 END,a.screen_name_lower LIMIT 1",
                params,
            ).fetchone()
        return str(row[0]) if row else None

    def _advance_fair_cursor(self, stage: str, account_lower: str) -> None:
        self.db.execute(
            "INSERT INTO investment_lead_scheduler_cursor(stage,account_lower) VALUES(?,?) "
            "ON CONFLICT(stage) DO UPDATE SET account_lower=excluded.account_lower",
            (stage, account_lower),
        )

    def _process_scan_batch(self, now: str) -> int:
        with self.db:
            fair_account = self._next_fair_account("scan", now)
            if fair_account is None:
                return 0
            rows = self.db.execute(
                "SELECT t.id,t.material_id,t.content_hash,t.attempts,m.payload_json,t.created_at "
                "FROM investment_lead_tasks t JOIN investment_lead_materials m "
                "ON m.material_id=t.material_id AND m.content_hash=t.content_hash "
                "WHERE t.stage='scan' AND t.status='pending' AND t.attempts<? "
                "AND (t.next_attempt_at IS NULL OR t.next_attempt_at<=?) "
                "AND EXISTS(SELECT 1 FROM investment_lead_observations o JOIN hottopic_accounts a "
                "ON a.screen_name_lower=o.screen_name_lower WHERE o.material_id=t.material_id "
                "AND o.content_hash=t.content_hash AND o.screen_name_lower=? "
                "AND a.status='followed' AND a.investment_leads_enabled=1) "
                "ORDER BY t.created_at,t.id LIMIT ?",
                (MAX_ATTEMPTS, now, fair_account, self.batch_size),
            ).fetchall()
            if not rows:
                return 0
            if len(rows) < self.batch_size and self.flush_seconds:
                oldest = datetime.fromisoformat(str(rows[0]["created_at"]))
                if _now() - oldest < timedelta(seconds=self.flush_seconds):
                    return 0
            self._advance_fair_cursor("scan", fair_account)
            jobs = [dict(row) for row in rows]
            for job in jobs:
                if not self._eligible(job["material_id"], job["content_hash"]):
                    self.db.execute(
                        "UPDATE investment_lead_tasks SET status='ignored',completed_at=?,updated_at=? WHERE id=?",
                        (now, now, job["id"]),
                    )
                    continue
                self.db.execute(
                    "UPDATE investment_lead_tasks SET status='processing',attempts=attempts+1,lease_until=?,updated_at=? "
                    "WHERE id=? AND status='pending'",
                    (_iso(_now() + LEASE), now, job["id"]),
                )
                job["attempts"] = int(job["attempts"]) + 1
        claimed = [job for job in jobs if self._eligible(job["material_id"], job["content_hash"])
                   and self.db.execute("SELECT status FROM investment_lead_tasks WHERE id=?", (job["id"],)).fetchone()[0] == "processing"]
        if not claimed:
            return 0
        without_text = [
            job for job in claimed
            if not _has_readable_material(_decoded(job["payload_json"], {}))
        ]
        for job in without_text:
            self._save_scan_result(
                job,
                {"classification": "incomplete", "reason": "原帖正文缺失，需读取附件或链接"},
                ModelOutput([], "", None, 0, 0, 0),
                now,
            )
        claimed = [job for job in claimed if job not in without_text]
        if not claimed:
            return len(without_text)
        if self.analyzer is None:
            self._fail_batch(claimed, self.analyzer_error or "X Agent 模型路由不可用", now)
            return len(claimed) + len(without_text)
        estimated = sum(
            max(1, len(_json(_decoded(job["payload_json"], {})))) for job in claimed
        )
        if self._remaining_input_tokens() < estimated:
            with self.db:
                for job in claimed:
                    self.db.execute(
                        "UPDATE investment_lead_tasks SET status='budget_paused',lease_until=NULL,updated_at=? WHERE id=?",
                        (now, job["id"]),
                    )
            return 0
        materials = []
        for job in claimed:
            material = _decoded(job["payload_json"], {})
            material["content_hash"] = str(job["content_hash"])
            materials.append(material)
        try:
            output = self.analyzer.scan(materials)
            self._record_usage("scan", output, now)
            results = {
                (str(item.get("material_id") or ""), str(item.get("content_hash") or "")): item
                for item in output.items
            }
            for job in claimed:
                result = results.get((str(job["material_id"]), str(job["content_hash"])))
                if result is None:
                    self._complete_scan_no_output(job, output, now)
                elif not self._eligible(job["material_id"], job["content_hash"]):
                    self._mark_ignored(job["id"], now)
                else:
                    self._save_scan_result(job, result, output, now)
            return len(claimed) + len(without_text)
        except Exception as exc:
            self._fail_batch(claimed, f"{type(exc).__name__}: {exc}", now)
            self.last_error = str(exc)
            return len(claimed) + len(without_text)

    def _save_scan_result(self, job: dict[str, Any], result: dict[str, Any], output: ModelOutput, now: str) -> None:
        classification = str(result.get("classification") or "incomplete")
        if classification not in {"driver_candidate", "composable", "no_candidate", "incomplete"}:
            classification = "incomplete"
        with self.db:
            self.db.execute("UPDATE investment_lead_tasks SET updated_at=updated_at WHERE id=?", (job["id"],))
            if not self._eligible(str(job["material_id"]), str(job["content_hash"])):
                self.db.execute(
                    "UPDATE investment_lead_tasks SET status='ignored',lease_until=NULL,completed_at=?,updated_at=? WHERE id=?",
                    (now, now, job["id"]),
                )
                return
            self.db.execute(
                "UPDATE investment_lead_materials SET completeness=? WHERE material_id=? AND content_hash=?",
                ("incomplete" if classification == "incomplete" else "complete", job["material_id"], job["content_hash"]),
            )
            self.db.execute(
                "INSERT INTO investment_lead_events("
                "material_id,content_hash,classification,subject,action,change_object,event_time,impact_relations_json,"
                "summary,reason,actual_model,prompt_version,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(material_id,content_hash,prompt_version) DO UPDATE SET "
                "classification=excluded.classification,subject=excluded.subject,action=excluded.action,"
                "change_object=excluded.change_object,event_time=excluded.event_time,"
                "impact_relations_json=excluded.impact_relations_json,summary=excluded.summary,reason=excluded.reason,"
                "actual_model=excluded.actual_model,created_at=excluded.created_at",
                (
                    job["material_id"], job["content_hash"], classification, result.get("subject") or "",
                    result.get("action") or "", result.get("change_object") or "", result.get("event_time") or "",
                    _json(result.get("impact_relations") or []), result.get("summary") or "", result.get("reason") or "",
                    output.actual_model, SCAN_PROMPT_VERSION, now,
                ),
            )
            status = "no_candidate" if classification == "no_candidate" else "needs_material" if classification == "incomplete" else "succeeded"
            self.db.execute(
                "UPDATE investment_lead_tasks SET status=?,lease_until=NULL,next_attempt_at=NULL,actual_model=?,"
                "fallback_reason=?,input_tokens=?,output_tokens=?,last_error=NULL,completed_at=?,updated_at=? WHERE id=?",
                (status, output.actual_model, output.fallback_reason, output.input_tokens, output.output_tokens, now, now, job["id"]),
            )
            if classification in {"driver_candidate", "composable"}:
                self.db.execute(
                    "INSERT OR IGNORE INTO investment_lead_tasks("
                    "material_id,content_hash,stage,prompt_version,status,created_at,updated_at"
                    ") VALUES(?,?, 'derive', ?, 'pending', ?, ?)",
                    (job["material_id"], job["content_hash"], DERIVE_PROMPT_VERSION, now, now),
                )
            if classification == "incomplete":
                material_row = self.db.execute(
                    "SELECT payload_json FROM investment_lead_materials WHERE material_id=? AND content_hash=?",
                    (job["material_id"], job["content_hash"]),
                ).fetchone()
                material = _decoded(material_row["payload_json"], {}) if material_row else {}
                if not material.get("supplement_attempted"):
                    self.db.execute(
                        "INSERT OR IGNORE INTO investment_lead_tasks("
                        "material_id,content_hash,stage,prompt_version,status,created_at,updated_at"
                        ") VALUES(?,?, 'supplement', 'investment_leads_supplement_v1', 'pending', ?, ?)",
                        (job["material_id"], job["content_hash"], now, now),
                    )

    def _complete_scan_no_output(self, job: dict[str, Any], output: ModelOutput, now: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE investment_lead_tasks SET status='failed',lease_until=NULL,last_error='模型未返回该材料的判定',"
                "actual_model=?,fallback_reason=?,input_tokens=?,output_tokens=?,completed_at=?,updated_at=? WHERE id=?",
                (output.actual_model, output.fallback_reason, output.input_tokens, output.output_tokens, now, now, job["id"]),
            )

    def _process_one_supplement(self, now: str) -> int:
        with self.db:
            row = self.db.execute(
                "SELECT t.id,t.material_id,t.content_hash,t.attempts,m.payload_json "
                "FROM investment_lead_tasks t JOIN investment_lead_materials m "
                "ON m.material_id=t.material_id AND m.content_hash=t.content_hash "
                "WHERE t.stage='supplement' AND t.status='pending' AND t.attempts<? "
                "AND (t.next_attempt_at IS NULL OR t.next_attempt_at<=?) "
                "AND EXISTS(SELECT 1 FROM investment_lead_observations o JOIN hottopic_accounts a "
                "ON a.screen_name_lower=o.screen_name_lower WHERE o.material_id=t.material_id "
                "AND o.content_hash=t.content_hash AND a.status='followed' AND a.investment_leads_enabled=1) "
                "ORDER BY t.created_at,t.id LIMIT 1",
                (MAX_ATTEMPTS, now),
            ).fetchone()
            if row is None:
                return 0
            job = dict(row)
            material = _decoded(job["payload_json"], {})
            urls = list(dict.fromkeys(str(url).strip() for url in material.get("links", []) if str(url).strip()))[:3]
            has_alt_text = any(
                isinstance(attachment, dict) and str(attachment.get("alt_text") or "").strip()
                for attachment in (material.get("attachments") or [])[:3]
            )
            if urls and self._remaining_supplement_reads() <= 0 and not has_alt_text:
                self.db.execute(
                    "UPDATE investment_lead_tasks SET status='budget_paused',updated_at=? WHERE id=?", (now, job["id"])
                )
                return 0
            self.db.execute(
                "UPDATE investment_lead_tasks SET status='processing',attempts=attempts+1,lease_until=?,updated_at=? "
                "WHERE id=? AND status='pending'",
                (_iso(_now() + LEASE), now, job["id"]),
            )
            job["attempts"] = int(job["attempts"]) + 1
        if not self._eligible(job["material_id"], job["content_hash"]):
            self._mark_ignored(job["id"], now)
            return 0
        material = _decoded(job["payload_json"], {})
        supplemented: list[dict[str, str]] = []
        for existing in (material.get("supplement_materials") or [])[:3]:
            if isinstance(existing, dict) and existing.get("text"):
                supplemented.append({"url": str(existing.get("url") or ""), "text": str(existing["text"])[:4000]})
        for attachment in (material.get("attachments") or [])[:3]:
            if isinstance(attachment, dict) and str(attachment.get("alt_text") or "").strip():
                alt_material = {
                    "url": str(attachment.get("url") or ""),
                    "text": "图片替代文本：" + " ".join(str(attachment["alt_text"]).split())[:1000],
                }
                if alt_material not in supplemented:
                    supplemented.append(alt_material)
        attempted_urls = set(str(url) for url in material.get("supplemented_urls", []) if str(url))
        pending_urls = [url for url in urls if url not in attempted_urls]
        eligible_urls = pending_urls[:max(0, 3 - len(supplemented))]
        read_limit = min(len(eligible_urls), self._remaining_supplement_reads())
        urls_to_read = eligible_urls[:read_limit]
        deferred_urls = eligible_urls[read_limit:]
        for url in urls_to_read:
            attempted_urls.add(url)
            page = _read_public_page(url)
            if page:
                supplemented.append(page)
        material["supplement_materials"] = supplemented[:3]
        material["supplemented_urls"] = sorted(attempted_urls)
        material["supplement_attempted"] = not deferred_urls
        try:
            if not self._eligible(job["material_id"], job["content_hash"]):
                self._mark_ignored(job["id"], now)
                return 0
            with self.db:
                self.db.execute("UPDATE investment_lead_tasks SET updated_at=updated_at WHERE id=?", (job["id"],))
                if not self._eligible(job["material_id"], job["content_hash"]):
                    self.db.execute(
                        "UPDATE investment_lead_tasks SET status='ignored',lease_until=NULL,completed_at=?,updated_at=? WHERE id=?",
                        (now, now, job["id"]),
                    )
                    return 0
                self.db.execute(
                    "UPDATE investment_lead_materials SET payload_json=?,last_observed_at=? "
                    "WHERE material_id=? AND content_hash=?",
                    (_json(material), now, job["material_id"], job["content_hash"]),
                )
                if deferred_urls:
                    self.db.execute(
                        "UPDATE investment_lead_tasks SET status='budget_paused',attempts=MAX(0,attempts-1),"
                        "lease_until=NULL,next_attempt_at=NULL,last_error=NULL,completed_at=NULL,updated_at=? WHERE id=?",
                        (now, job["id"]),
                    )
                else:
                    self.db.execute(
                        "UPDATE investment_lead_tasks SET status='succeeded',lease_until=NULL,next_attempt_at=NULL,"
                        "last_error=NULL,completed_at=?,updated_at=? WHERE id=?",
                        (now, now, job["id"]),
                    )
                    self.db.execute(
                        "UPDATE investment_lead_tasks SET status='pending',attempts=0,next_attempt_at=NULL,"
                        "completed_at=NULL,updated_at=? WHERE material_id=? AND content_hash=? AND stage='scan' "
                        "AND prompt_version=? AND status='needs_material'",
                        (now, job["material_id"], job["content_hash"], SCAN_PROMPT_VERSION),
                    )
                if urls_to_read:
                    self.db.execute(
                        "INSERT INTO investment_lead_usage(usage_date,kind,calls,input_tokens,output_tokens,updated_at) "
                        "VALUES(?,'supplement',?,0,0,?) ON CONFLICT(usage_date,kind) DO UPDATE SET "
                        "calls=calls+excluded.calls,updated_at=excluded.updated_at",
                        (_utc_date(), len(urls_to_read), now),
                    )
            return 1
        except Exception as exc:
            self._fail_batch([job], f"{type(exc).__name__}: {exc}", now)
            self.last_error = str(exc)
            return 1

    def _remaining_supplement_reads(self) -> int:
        if self.daily_supplement_reads <= 0:
            return 0
        row = self.db.execute(
            "SELECT COALESCE(calls,0) FROM investment_lead_usage WHERE usage_date=? AND kind='supplement'",
            (_utc_date(),),
        ).fetchone()
        used = int(row[0]) if row else 0
        return max(0, self.daily_supplement_reads - used)

    def _remaining_identity_searches(self) -> int:
        if self.daily_identity_searches <= 0:
            return 0
        row = self.db.execute(
            "SELECT COALESCE(calls,0) FROM investment_lead_usage WHERE usage_date=? AND kind='search'",
            (_utc_date(),),
        ).fetchone()
        used = int(row[0]) if row else 0
        return max(0, self.daily_identity_searches - used)

    def _process_one_identity_search(self, now: str) -> int:
        with self.db:
            fair_account = self._next_fair_account("identity_search", now)
            if fair_account is None:
                return 0
            row = self.db.execute(
                "SELECT s.id,s.lead_id,s.asset_key,s.asset_name,s.context_json,s.attempts "
                "FROM investment_lead_identity_searches s WHERE s.status='pending' AND s.attempts<? "
                "AND (s.next_attempt_at IS NULL OR s.next_attempt_at<=?) AND EXISTS("
                "SELECT 1 FROM investment_lead_sources ls JOIN investment_lead_observations o "
                "ON o.material_id=ls.material_id AND o.content_hash=ls.content_hash "
                "JOIN hottopic_accounts a ON a.screen_name_lower=o.screen_name_lower "
                "WHERE ls.lead_id=s.lead_id AND a.status='followed' AND a.investment_leads_enabled=1 "
                "AND a.screen_name_lower=?) "
                "ORDER BY s.created_at,s.id LIMIT 1",
                (MAX_ATTEMPTS, now, fair_account),
            ).fetchone()
            if row is None:
                return 0
            job = dict(row)
            if self._remaining_identity_searches() <= 0:
                self.db.execute(
                    "UPDATE investment_lead_identity_searches SET status='budget_paused',updated_at=? WHERE id=?",
                    (now, job["id"]),
                )
                return 0
            estimated_input_tokens = 800 + len(str(job["asset_name"])) + len(str(job["context_json"]))
            if self._remaining_input_tokens() < estimated_input_tokens:
                self.db.execute(
                    "UPDATE investment_lead_identity_searches SET status='budget_paused',updated_at=? WHERE id=?",
                    (now, job["id"]),
                )
                return 0
            self.db.execute(
                "UPDATE investment_lead_identity_searches SET status='processing',attempts=attempts+1,"
                "lease_until=?,updated_at=? WHERE id=? AND status='pending'",
                (_iso(_now() + LEASE), now, job["id"]),
            )
            self._advance_fair_cursor("identity_search", fair_account)
            job["attempts"] = int(job["attempts"]) + 1
        if self.analyzer is None:
            self._fail_identity_search(job, self.analyzer_error or "X Agent 模型路由不可用", now)
            return 1
        if not self._lead_has_eligible_observation(str(job["lead_id"])):
            self._set_identity_search_status(int(job["id"]), "ignored", now)
            return 0
        usage_recorded = False
        try:
            context = _decoded(job["context_json"], {})
            output = self.analyzer.search_identity(str(job["asset_name"]), context)
            self._record_usage(
                "search",
                ModelOutput([], output.actual_model, None, output.input_tokens, output.output_tokens, 1),
                now,
            )
            usage_recorded = True
            resolved = bool(output.identity.get("ticker"))
            result = {
                "status": "resolved" if resolved else "unresolved",
                "identity": output.identity,
                "summaries": output.summaries[:3],
                "citations": output.citations[:3],
                "actualModel": output.actual_model,
            }
            with self.db:
                self.db.execute(
                    "UPDATE investment_lead_identity_searches SET updated_at=updated_at WHERE id=?", (job["id"],)
                )
                if not self._lead_has_eligible_observation(str(job["lead_id"])):
                    self.db.execute(
                        "UPDATE investment_lead_identity_searches SET status='ignored',lease_until=NULL,completed_at=?,updated_at=? WHERE id=?",
                        (now, now, job["id"]),
                    )
                    return 0
                self.db.execute(
                    "UPDATE investment_lead_identity_searches SET status=?,lease_until=NULL,next_attempt_at=NULL,"
                    "result_json=?,last_error=NULL,completed_at=?,updated_at=? WHERE id=?",
                    ("succeeded" if resolved else "unresolved", _json(result), now, now, job["id"]),
                )
                if resolved:
                    self._apply_identity_search_result(str(job["lead_id"]), job, output, now)
            return 1
        except Exception as exc:
            if not usage_recorded:
                self._record_usage(
                    "search", ModelOutput([], self.analyzer.primary_model, None, 0, 0, 1), now
                )
            self._fail_identity_search(job, f"{type(exc).__name__}: {exc}", now)
            self.last_error = str(exc)
            return 1

    def _lead_has_eligible_observation(self, lead_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM investment_lead_sources ls JOIN investment_lead_observations o "
            "ON o.material_id=ls.material_id AND o.content_hash=ls.content_hash "
            "JOIN hottopic_accounts a ON a.screen_name_lower=o.screen_name_lower "
            "WHERE ls.lead_id=? AND a.status='followed' AND a.investment_leads_enabled=1 LIMIT 1",
            (lead_id,),
        ).fetchone() is not None

    def _apply_identity_search_result(self, lead_id: str, job: dict[str, Any], output: Any, now: str) -> None:
        row = self.db.execute("SELECT * FROM investment_leads WHERE id=?", (lead_id,)).fetchone()
        if row is None:
            return
        assets = _decoded(row["assets_json"], [])
        changed = False
        target_key = str(job["asset_key"])
        for asset in assets:
            if not isinstance(asset, dict) or _identity_asset_key(str(asset.get("name") or "")) != target_key:
                continue
            if not str(asset.get("ticker") or "").strip() and output.identity.get("ticker"):
                asset["ticker"] = output.identity["ticker"]
                asset["market"] = output.identity.get("market") or asset.get("market") or ""
                changed = True
        if not changed:
            return
        assets_json = _json(assets)
        revision = int(row["revision"]) + 1
        self.db.execute(
            "UPDATE investment_leads SET assets_json=?,last_substantive_at=?,revision=?,updated_at=? WHERE id=?",
            (assets_json, now, revision, now, lead_id),
        )
        self.db.execute(
            "INSERT INTO investment_lead_revisions(lead_id,revision,subject,driver_logic,conditions,assets_json,"
            "source_claims_json,source_materials_json,actual_model,prompt_version,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (lead_id, revision, row["subject"], row["driver_logic"], row["conditions"], assets_json,
             row["source_claims_json"], _json([{
                 "type": "identity_search", "asset_name": job["asset_name"],
                 "identity": output.identity, "summaries": output.summaries[:3], "citations": output.citations[:3],
             }]), output.actual_model, IDENTITY_SEARCH_PROMPT_VERSION, now),
        )

    def _set_identity_search_status(self, search_id: int, status: str, now: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE investment_lead_identity_searches SET status=?,lease_until=NULL,next_attempt_at=NULL,"
                "last_error=NULL,completed_at=?,updated_at=? WHERE id=?",
                (status, now, now, search_id),
            )

    def _fail_identity_search(self, job: dict[str, Any], message: str, now: str) -> None:
        attempts = int(job.get("attempts") or 1)
        exhausted = attempts >= MAX_ATTEMPTS
        with self.db:
            self.db.execute(
                "UPDATE investment_lead_identity_searches SET status=?,lease_until=NULL,next_attempt_at=?,last_error=?,"
                "completed_at=?,updated_at=? WHERE id=?",
                ("failed" if exhausted else "pending", None if exhausted else _iso(_now() + RETRY_DELAY),
                 message[:1200], now if exhausted else None, now, job["id"]),
            )

    def _process_one_derive(self, now: str) -> int:
        with self.db:
            fair_account = self._next_fair_account("derive", now)
            if fair_account is None:
                return 0
            row = self.db.execute(
                "SELECT t.id,t.material_id,t.content_hash,t.attempts,e.classification,e.subject,e.action,e.change_object,e.event_time,"
                "e.impact_relations_json,e.summary,e.reason,m.payload_json,t.created_at "
                "FROM investment_lead_tasks t JOIN investment_lead_events e "
                "ON e.material_id=t.material_id AND e.content_hash=t.content_hash AND e.prompt_version=? "
                "JOIN investment_lead_materials m ON m.material_id=t.material_id AND m.content_hash=t.content_hash "
                "WHERE t.stage='derive' AND t.status='pending' AND t.attempts<? "
                "AND (t.next_attempt_at IS NULL OR t.next_attempt_at<=?) "
                "AND EXISTS(SELECT 1 FROM investment_lead_observations o JOIN hottopic_accounts a "
                "ON a.screen_name_lower=o.screen_name_lower WHERE o.material_id=t.material_id "
                "AND o.content_hash=t.content_hash AND o.screen_name_lower=? "
                "AND a.status='followed' AND a.investment_leads_enabled=1) "
                "ORDER BY t.created_at,t.id LIMIT 1",
                (SCAN_PROMPT_VERSION, MAX_ATTEMPTS, now, fair_account),
            ).fetchone()
            if row is None:
                return 0
            job = dict(row)
            if self._remaining_derive_calls() <= 0 or self._remaining_input_tokens() <= max(1, len(str(job["payload_json"]))):
                self.db.execute(
                    "UPDATE investment_lead_tasks SET status='budget_paused',updated_at=? WHERE id=?", (now, job["id"])
                )
                return 0
            self.db.execute(
                "UPDATE investment_lead_tasks SET status='processing',attempts=attempts+1,lease_until=?,updated_at=? "
                "WHERE id=? AND status='pending'",
                (_iso(_now() + LEASE), now, job["id"]),
            )
            self._advance_fair_cursor("derive", fair_account)
            job["attempts"] = int(job["attempts"]) + 1
        if self.analyzer is None:
            self._fail_batch([job], self.analyzer_error or "X Agent 模型路由不可用", now)
            return 1
        if not self._eligible(job["material_id"], job["content_hash"]):
            self._mark_ignored(job["id"], now)
            return 0
        candidate_material = _decoded(job["payload_json"], {})
        candidate = {
            "material_id": job["material_id"], "content_hash": job["content_hash"],
            "classification": job["classification"], "subject": job["subject"], "action": job["action"],
            "change_object": job["change_object"], "event_time": job["event_time"],
            "impact_relations": _decoded(job["impact_relations_json"], []), "summary": job["summary"],
            "reason": job["reason"], "material": candidate_material,
        }
        related_events = self._related_events(candidate, job["material_id"], job["content_hash"])
        if candidate["classification"] == "composable" and not related_events:
            self._set_job_status(job["id"], "no_candidate", now)
            return 1
        existing_leads = self._recent_leads()
        try:
            output = self.analyzer.derive(candidate, related_events, existing_leads)
            self._record_usage("derive", output, now)
            if not self._eligible(job["material_id"], job["content_hash"]):
                self._mark_ignored(job["id"], now)
                return 0
            linked_materials: set[tuple[str, str]] = {(job["material_id"], job["content_hash"])}
            for related in related_events:
                linked_materials.add((str(related.get("material_id") or ""), str(related.get("content_hash") or "")))
            with self.db:
                self.db.execute("UPDATE investment_lead_tasks SET updated_at=updated_at WHERE id=?", (job["id"],))
                if not self._eligible(job["material_id"], job["content_hash"]):
                    self.db.execute(
                        "UPDATE investment_lead_tasks SET status='ignored',lease_until=NULL,completed_at=?,updated_at=? WHERE id=?",
                        (now, now, job["id"]),
                    )
                    return 0
                result_count = 0
                for lead in output.items:
                    lead_sources = [pair for pair in linked_materials if pair[0] in lead["source_material_ids"]]
                    if not lead_sources:
                        lead_sources = [(job["material_id"], job["content_hash"])]
                    lead_id = self._save_lead(lead, lead_sources, output, now)
                    result_count += bool(lead_id)
                status = "succeeded" if result_count else "no_candidate"
                self.db.execute(
                    "UPDATE investment_lead_tasks SET status=?,lease_until=NULL,next_attempt_at=NULL,actual_model=?,"
                    "fallback_reason=?,input_tokens=?,output_tokens=?,last_error=NULL,completed_at=?,updated_at=? WHERE id=?",
                    (status, output.actual_model, output.fallback_reason, output.input_tokens, output.output_tokens, now, now, job["id"]),
                )
            return 1
        except Exception as exc:
            self._fail_batch([job], f"{type(exc).__name__}: {exc}", now)
            self.last_error = str(exc)
            return 1

    def _related_events(self, candidate: dict[str, Any], material_id: str, content_hash: str) -> list[dict[str, Any]]:
        cutoff_7d = _iso(_now() - timedelta(days=7))
        cutoff_30d = _iso(_now() - timedelta(days=30))
        rows = self.db.execute(
            "SELECT e.material_id,e.content_hash,e.classification,e.subject,e.action,e.change_object,e.event_time,"
            "e.impact_relations_json,e.summary,e.created_at,m.payload_json,m.posted_at,m.activity_at,m.activity_time_complete "
            "FROM investment_lead_events e JOIN investment_lead_materials m "
            "ON m.material_id=e.material_id AND m.content_hash=e.content_hash "
            "WHERE e.created_at>=? AND e.classification IN ('driver_candidate','composable') "
            "AND NOT(e.material_id=? AND e.content_hash=?) "
            "AND EXISTS(SELECT 1 FROM investment_lead_observations o JOIN hottopic_accounts a "
            "ON a.screen_name_lower=o.screen_name_lower WHERE o.material_id=e.material_id "
            "AND o.content_hash=e.content_hash AND a.status='followed' AND a.investment_leads_enabled=1) "
            "ORDER BY e.created_at DESC LIMIT 100",
            (cutoff_30d, material_id, content_hash),
        ).fetchall()
        current_terms = _terms(" ".join(str(candidate.get(key) or "") for key in ("subject", "action", "change_object", "summary", "impact_relations")))
        selected: list[tuple[float, dict[str, Any]]] = []
        seen_materials: set[str] = set()
        for row in rows:
            data = dict(row)
            if data["material_id"] in seen_materials:
                continue
            other_terms = _terms(" ".join(str(data.get(key) or "") for key in (
                "subject", "action", "change_object", "summary", "impact_relations_json"
            )))
            shared = len(current_terms & other_terms)
            score = shared / max(1, len(current_terms | other_terms))
            same_subject = bool(candidate.get("subject") and str(candidate.get("subject")).casefold() == str(data.get("subject") or "").casefold())
            activity_time = data.get("activity_at") if data.get("activity_time_complete") else data.get("posted_at")
            activity_time = activity_time or data.get("posted_at") or data.get("created_at")
            if str(activity_time or "") < cutoff_7d and not same_subject:
                continue
            if str(activity_time or "") < cutoff_30d:
                continue
            if same_subject or shared >= 2 or (not current_terms and len(selected) < 5):
                material = _decoded(data["payload_json"], {})
                selected.append((2.0 if same_subject else score, {
                    "material_id": data["material_id"], "content_hash": data["content_hash"],
                    "classification": data["classification"], "subject": data["subject"], "action": data["action"],
                    "change_object": data["change_object"], "event_time": data["event_time"],
                    "impact_relations": _decoded(data["impact_relations_json"], []), "summary": data["summary"],
                    "created_at": data["created_at"], "posted_at": data["posted_at"],
                    "activity_at": data["activity_at"], "author_screen_name": material.get("author_screen_name"),
                    "role": material.get("role"), "url": material.get("url"), "text": str(material.get("text") or "")[:2000],
                }))
                seen_materials.add(str(data["material_id"]))
        selected.sort(key=lambda item: item[0], reverse=True)
        return [item for _, item in selected[:20]]

    def _recent_leads(self) -> list[dict[str, Any]]:
        cutoff = _iso(_now() - timedelta(days=30))
        rows = self.db.execute(
            "SELECT id,subject,driver_logic,conditions,assets_json,status,last_substantive_at "
            "FROM investment_leads WHERE last_substantive_at>=? ORDER BY last_substantive_at DESC LIMIT 30",
            (cutoff,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _save_lead(
        self,
        lead: dict[str, Any],
        sources: list[tuple[str, str]],
        output: ModelOutput,
        now: str,
    ) -> str:
        lead_id = str(lead.get("matched_lead_id") or "")
        row = self.db.execute("SELECT * FROM investment_leads WHERE id=?", (lead_id,)).fetchone() if lead_id else None
        assets_json = _json(lead.get("assets") or [])
        claims_json = _json(lead.get("source_claims") or [])
        if row is None:
            lead_id = str(uuid.uuid4())
            status = "active" if lead.get("assets") else "unlocated"
            self.db.execute(
                "INSERT INTO investment_leads("
                "id,subject,driver_logic,conditions,assets_json,source_claims_json,first_seen_at,last_substantive_at,"
                "revision,status,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,1,?,?,?)",
                (lead_id, lead["subject"], lead["driver_logic"], lead.get("conditions") or "", assets_json,
                 claims_json, now, now, status, now, now),
            )
            revision = 1
            substantive = True
        else:
            old = dict(row)
            substantive = (
                old["subject"] != lead["subject"]
                or old["driver_logic"] != lead["driver_logic"]
                or old["conditions"] != (lead.get("conditions") or "")
                or old["assets_json"] != assets_json
            )
            revision = int(old["revision"]) + (1 if substantive else 0)
            status = "active" if lead.get("assets") else "unlocated"
            if substantive:
                self.db.execute(
                    "UPDATE investment_leads SET subject=?,driver_logic=?,conditions=?,assets_json=?,source_claims_json=?,"
                    "last_substantive_at=?,revision=?,status=?,updated_at=? WHERE id=?",
                    (lead["subject"], lead["driver_logic"], lead.get("conditions") or "", assets_json,
                     claims_json, now, revision, status, now, lead_id),
                )
        if row is None or substantive:
            self.db.execute(
                "INSERT INTO investment_lead_revisions("
                "lead_id,revision,subject,driver_logic,conditions,assets_json,source_claims_json,source_materials_json,"
                "actual_model,prompt_version,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (lead_id, revision, lead["subject"], lead["driver_logic"], lead.get("conditions") or "",
                 assets_json, claims_json, _json(sources), output.actual_model, DERIVE_PROMPT_VERSION, now),
            )
        for material_id, content_hash in sources:
            if not material_id or not content_hash:
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO investment_lead_sources(lead_id,material_id,content_hash,role,created_at) "
                "VALUES(?,?,?,?,?)",
                (lead_id, material_id, content_hash, "evidence", now),
            )
        for asset in lead.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            asset_name = str(asset.get("name") or "").strip()
            if not asset_name or str(asset.get("ticker") or "").strip():
                continue
            asset_key = _identity_asset_key(asset_name)
            if not asset_key:
                continue
            context = {
                "subject": lead["subject"], "driver_logic": lead["driver_logic"],
                "asset_direction": asset.get("direction") or "", "asset_relation": asset.get("relation") or "",
            }
            self.db.execute(
                "INSERT OR IGNORE INTO investment_lead_identity_searches("
                "lead_id,asset_key,asset_name,context_json,status,created_at,updated_at"
                ") VALUES(?,?,?,?, 'pending', ?, ?)",
                (lead_id, asset_key, asset_name, _json(context), now, now),
            )
            self.db.execute(
                "UPDATE investment_lead_identity_searches SET status='pending',attempts=0,next_attempt_at=NULL,"
                "lease_until=NULL,last_error=NULL,completed_at=NULL,updated_at=? WHERE lead_id=? AND asset_key=? AND status='ignored'",
                (now, lead_id, asset_key),
            )
        return lead_id

    def _record_usage(self, kind: str, output: ModelOutput, now: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO investment_lead_usage(usage_date,kind,calls,input_tokens,output_tokens,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(usage_date,kind) DO UPDATE SET "
                "calls=calls+excluded.calls,input_tokens=input_tokens+excluded.input_tokens,"
                "output_tokens=output_tokens+excluded.output_tokens,updated_at=excluded.updated_at",
                (_utc_date(), kind, output.calls, output.input_tokens, output.output_tokens, now),
            )

    def _remaining_input_tokens(self) -> int:
        if self.daily_input_tokens <= 0:
            return 0
        used = int(self.db.execute(
            "SELECT COALESCE(SUM(input_tokens),0) FROM investment_lead_usage WHERE usage_date=?", (_utc_date(),)
        ).fetchone()[0])
        return max(0, self.daily_input_tokens - used)

    def _remaining_derive_calls(self) -> int:
        if self.daily_derive_calls <= 0:
            return 0
        row = self.db.execute(
            "SELECT COALESCE(calls,0) FROM investment_lead_usage WHERE usage_date=? AND kind='derive'",
            (_utc_date(),),
        ).fetchone()
        used = int(row[0]) if row else 0
        return max(0, self.daily_derive_calls - used)

    def _fail_batch(self, jobs: Iterable[dict[str, Any]], message: str, now: str) -> None:
        with self.db:
            for job in jobs:
                attempts = int(job.get("attempts") or 1)
                exhausted = attempts >= MAX_ATTEMPTS
                self.db.execute(
                    "UPDATE investment_lead_tasks SET status=?,lease_until=NULL,next_attempt_at=?,last_error=?,"
                    "completed_at=?,updated_at=? WHERE id=?",
                    ("failed" if exhausted else "pending", None if exhausted else _iso(_now() + RETRY_DELAY),
                     message[:1200], now if exhausted else None, now, job["id"]),
                )

    def _mark_ignored(self, job_id: int, now: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE investment_lead_tasks SET status='ignored',lease_until=NULL,next_attempt_at=NULL,last_error=NULL,"
                "completed_at=?,updated_at=? WHERE id=?",
                (now, now, job_id),
            )

    def _set_job_status(self, job_id: int, status: str, now: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE investment_lead_tasks SET status=?,lease_until=NULL,next_attempt_at=NULL,last_error=NULL,"
                "completed_at=?,updated_at=? WHERE id=?",
                (status, now, now, job_id),
            )

    def _maintain(self, now: str) -> None:
        last = self.db.execute(
            "SELECT last_maintenance_at FROM investment_leads_meta WHERE singleton_key='global'"
        ).fetchone()
        if last and last[0]:
            try:
                if _now() - datetime.fromisoformat(str(last[0])) < timedelta(hours=1):
                    return
            except ValueError:
                pass
        cutoff = _iso(_now() - RETENTION)
        with self.db:
            self.db.execute(
                "UPDATE investment_lead_tasks SET status='expired',lease_until=NULL,last_error=COALESCE(last_error,'超过材料保留期限'),"
                "completed_at=?,updated_at=? WHERE status IN ('pending','failed','budget_paused') AND created_at<?",
                (now, now, cutoff),
            )
            self.db.execute(
                "UPDATE investment_lead_identity_searches SET status='expired',lease_until=NULL,"
                "last_error=COALESCE(last_error,'超过材料保留期限'),completed_at=?,updated_at=? "
                "WHERE status IN ('pending','failed','budget_paused') AND created_at<?",
                (now, now, cutoff),
            )
            self.db.execute(
                "DELETE FROM investment_lead_events WHERE created_at<? AND NOT EXISTS("
                "SELECT 1 FROM investment_lead_sources s WHERE s.material_id=investment_lead_events.material_id "
                "AND s.content_hash=investment_lead_events.content_hash)",
                (cutoff,),
            )
            self.db.execute(
                "DELETE FROM investment_lead_materials WHERE last_observed_at<? AND NOT EXISTS("
                "SELECT 1 FROM investment_lead_sources s WHERE s.material_id=investment_lead_materials.material_id "
                "AND s.content_hash=investment_lead_materials.content_hash)",
                (cutoff,),
            )
            self.db.execute("UPDATE investment_leads_meta SET last_maintenance_at=? WHERE singleton_key='global'", (now,))

    def retry_failed(self, *, stage: str = "", limit: int = 100) -> int:
        if stage and stage not in {"scan", "derive", "supplement", "identity_search"}:
            raise ValueError("不支持的投资线索任务阶段")
        safe_limit = min(500, max(1, int(limit)))
        if stage == "identity_search":
            rows = self.db.execute(
                "SELECT id,lead_id FROM investment_lead_identity_searches WHERE status='failed' "
                "ORDER BY updated_at LIMIT ?", (safe_limit,),
            ).fetchall()
            eligible_ids = [int(row["id"]) for row in rows if self._lead_has_eligible_observation(str(row["lead_id"]))]
            if not eligible_ids:
                return 0
            marks = ",".join("?" for _ in eligible_ids)
            with self.db:
                self.db.execute(
                    f"UPDATE investment_lead_identity_searches SET status='pending',attempts=0,next_attempt_at=NULL,"
                    f"lease_until=NULL,last_error=NULL,completed_at=NULL,updated_at=? WHERE id IN ({marks})",
                    [_iso(), *eligible_ids],
                )
            return len(eligible_ids)
        where = "WHERE status='failed'" + (" AND stage=?" if stage else "")
        params: list[Any] = [stage] if stage else []
        rows = self.db.execute(
            f"SELECT id,material_id,content_hash FROM investment_lead_tasks {where} ORDER BY updated_at LIMIT ?",
            [*params, safe_limit],
        ).fetchall()
        eligible_ids = [int(row["id"]) for row in rows if self._eligible(str(row["material_id"]), str(row["content_hash"]))]
        core_requeued = 0
        if eligible_ids:
            marks = ",".join("?" for _ in eligible_ids)
            with self.db:
                self.db.execute(
                    f"UPDATE investment_lead_tasks SET status='pending',attempts=0,next_attempt_at=NULL,lease_until=NULL,"
                    f"last_error=NULL,completed_at=NULL,updated_at=? WHERE id IN ({marks})",
                    [_iso(), *eligible_ids],
                )
            core_requeued = len(eligible_ids)
        if stage:
            return core_requeued
        search_rows = self.db.execute(
            "SELECT id,lead_id FROM investment_lead_identity_searches WHERE status='failed' ORDER BY updated_at LIMIT ?",
            (max(0, safe_limit - core_requeued),),
        ).fetchall()
        search_ids = [int(row["id"]) for row in search_rows if self._lead_has_eligible_observation(str(row["lead_id"]))]
        if search_ids:
            marks = ",".join("?" for _ in search_ids)
            with self.db:
                self.db.execute(
                    f"UPDATE investment_lead_identity_searches SET status='pending',attempts=0,next_attempt_at=NULL,"
                    f"lease_until=NULL,last_error=NULL,completed_at=NULL,updated_at=? WHERE id IN ({marks})",
                    [_iso(), *search_ids],
                )
        return core_requeued + len(search_ids)

    def dashboard(self) -> dict[str, Any]:
        accounts = self.db.execute(
            "SELECT COUNT(*) total,SUM(investment_leads_enabled=1) enabled "
            "FROM hottopic_accounts WHERE status='followed'"
        ).fetchone()
        task_rows = self.db.execute(
            "SELECT stage,status,COUNT(*) count,MIN(created_at) oldest FROM investment_lead_tasks GROUP BY stage,status"
        ).fetchall()
        identity_rows = self.db.execute(
            "SELECT status,COUNT(*) count,MIN(created_at) oldest FROM investment_lead_identity_searches GROUP BY status"
        ).fetchall()
        usage = self.db.execute(
            "SELECT kind,calls,input_tokens,output_tokens FROM investment_lead_usage WHERE usage_date=?",
            (_utc_date(),),
        ).fetchall()
        meta = self.db.execute(
            "SELECT capture_started_at,worker_last_seen_at,model_error FROM investment_leads_meta WHERE singleton_key='global'"
        ).fetchone()
        counts = {(str(row["stage"]), str(row["status"])): int(row["count"] or 0) for row in task_rows}
        oldest_pending = [str(row["oldest"]) for row in task_rows if row["status"] in {"pending", "processing"} and row["oldest"]]
        identity_counts = {str(row["status"]): int(row["count"] or 0) for row in identity_rows}
        oldest_pending.extend(
            str(row["oldest"]) for row in identity_rows
            if row["status"] in {"pending", "processing"} and row["oldest"]
        )
        usage_by_kind = {str(row["kind"]): row for row in usage}
        input_tokens = sum(int(row["input_tokens"] or 0) for row in usage)
        return {
            "generatedAt": _iso(),
            "captureStartedAt": meta["capture_started_at"],
            "workerLastSeenAt": meta["worker_last_seen_at"],
            "modelError": meta["model_error"],
            "accounts": {"total": int(accounts["total"] or 0), "investmentLeadsEnabled": int(accounts["enabled"] or 0)},
            "tasks": {
                "pendingScan": counts.get(("scan", "pending"), 0),
                "processingScan": counts.get(("scan", "processing"), 0),
                "pendingDerive": counts.get(("derive", "pending"), 0),
                "processingDerive": counts.get(("derive", "processing"), 0),
                "pendingSupplement": counts.get(("supplement", "pending"), 0),
                "processingSupplement": counts.get(("supplement", "processing"), 0),
                "pendingIdentitySearch": identity_counts.get("pending", 0),
                "processingIdentitySearch": identity_counts.get("processing", 0),
                "failed": sum(counts.get((stage, "failed"), 0) for stage in ("scan", "derive", "supplement")) + identity_counts.get("failed", 0),
                "budgetPaused": sum(counts.get((stage, "budget_paused"), 0) for stage in ("scan", "derive", "supplement")) + identity_counts.get("budget_paused", 0),
                "identitySearchFailures": identity_counts.get("failed", 0),
                "identitySearchUnresolved": identity_counts.get("unresolved", 0),
                "incomplete": counts.get(("scan", "needs_material"), 0),
                "oldestPendingAt": min(oldest_pending) if oldest_pending else None,
            },
            "results": {
                "active": int(self.db.execute("SELECT COUNT(*) FROM investment_leads WHERE status='active'").fetchone()[0]),
                "unlocated": int(self.db.execute("SELECT COUNT(*) FROM investment_leads WHERE status='unlocated'").fetchone()[0]),
                "scannedNoCandidate": counts.get(("scan", "no_candidate"), 0),
            },
            "usage": {
                "inputTokens": input_tokens,
                "inputTokenBudget": self.daily_input_tokens,
                "scanCalls": int(usage_by_kind["scan"]["calls"] if usage_by_kind.get("scan") else 0),
                "deriveCalls": int(usage_by_kind["derive"]["calls"] if usage_by_kind.get("derive") else 0),
                "deriveCallBudget": self.daily_derive_calls,
                "supplementReads": int(usage_by_kind["supplement"]["calls"] if usage_by_kind.get("supplement") else 0),
                "supplementReadBudget": self.daily_supplement_reads,
                "searchCalls": int(usage_by_kind["search"]["calls"] if usage_by_kind.get("search") else 0),
                "searchCallBudget": self.daily_identity_searches,
                "outputTokens": sum(int(row["output_tokens"] or 0) for row in usage),
            },
        }

    def list_leads(
        self,
        *,
        window: str = "7d",
        query: str = "",
        account: str = "",
        unlocated_only: bool = False,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        durations = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
        if window not in durations:
            raise ValueError("不支持的投资线索时间窗口")
        cutoff = _iso(_now() - durations[window])
        clauses = ["l.last_substantive_at>=?"]
        params: list[Any] = [cutoff]
        if unlocated_only:
            clauses.append("l.status='unlocated'")
        if query.strip():
            clauses.append("(LOWER(l.subject || ' ' || l.driver_logic || ' ' || l.conditions || ' ' || l.assets_json) LIKE ?)")
            params.append(f"%{query.strip().lower()}%")
        if account.strip():
            clauses.append(
                "EXISTS(SELECT 1 FROM investment_lead_sources s JOIN investment_lead_observations o "
                "ON o.material_id=s.material_id AND o.content_hash=s.content_hash "
                "WHERE s.lead_id=l.id AND o.screen_name_lower=?)"
            )
            params.append(account.strip().lstrip("@").lower())
        where = " WHERE " + " AND ".join(clauses)
        total = int(self.db.execute(f"SELECT COUNT(*) FROM investment_leads l{where}", params).fetchone()[0])
        safe_limit, safe_offset = min(100, max(1, int(limit))), max(0, int(offset))
        rows = self.db.execute(
            "SELECT l.*,(SELECT group_concat(DISTINCT o.screen_name_lower) FROM investment_lead_sources s "
            "JOIN investment_lead_observations o ON o.material_id=s.material_id AND o.content_hash=s.content_hash "
            "WHERE s.lead_id=l.id) accounts "
            f"FROM investment_leads l{where} ORDER BY l.last_substantive_at DESC,l.id LIMIT ? OFFSET ?",
            [*params, safe_limit, safe_offset],
        ).fetchall()
        return {"items": [self._lead_payload(dict(row)) for row in rows], "total": total}

    def lead_detail(self, lead_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM investment_leads WHERE id=?", (lead_id,)).fetchone()
        if row is None:
            return None
        lead = self._lead_payload(dict(row))
        sources = self.db.execute(
            "SELECT s.material_id,s.content_hash,s.role,s.created_at,m.payload_json,m.posted_at,m.activity_at,"
            "m.activity_time_complete,group_concat(DISTINCT o.screen_name_lower) accounts "
            "FROM investment_lead_sources s JOIN investment_lead_materials m "
            "ON m.material_id=s.material_id AND m.content_hash=s.content_hash "
            "LEFT JOIN investment_lead_observations o ON o.material_id=s.material_id AND o.content_hash=s.content_hash "
            "WHERE s.lead_id=? GROUP BY s.material_id,s.content_hash ORDER BY s.created_at",
            (lead_id,),
        ).fetchall()
        revisions = self.db.execute(
            "SELECT revision,subject,driver_logic,conditions,assets_json,source_claims_json,actual_model,created_at "
            "FROM investment_lead_revisions WHERE lead_id=? ORDER BY revision DESC",
            (lead_id,),
        ).fetchall()
        identity_searches = self.db.execute(
            "SELECT asset_name,status,result_json,last_error,updated_at "
            "FROM investment_lead_identity_searches WHERE lead_id=? ORDER BY created_at",
            (lead_id,),
        ).fetchall()
        lead["identitySearches"] = []
        for search in identity_searches:
            result = _decoded(search["result_json"], {})
            lead["identitySearches"].append({
                "assetName": search["asset_name"], "status": search["status"],
                "identity": result.get("identity") or {}, "summaries": result.get("summaries") or [],
                "citations": result.get("citations") or [], "error": search["last_error"],
                "updatedAt": search["updated_at"],
            })
        lead["sources"] = []
        for source in sources:
            payload = _decoded(source["payload_json"], {})
            lead["sources"].append({
                "materialId": source["material_id"], "account": source["accounts"] or "",
                "author": payload.get("author_screen_name") or "", "authorName": payload.get("author_name") or "",
                "url": payload.get("url") or "", "text": payload.get("text") or "",
                "postedAt": source["posted_at"], "activityAt": source["activity_at"],
                "activityTimeComplete": bool(source["activity_time_complete"]),
                "relation": payload.get("relation") or "", "role": payload.get("role") or "",
                "relatedMaterials": payload.get("related_materials") or [],
                "supplementMaterials": payload.get("supplement_materials") or [],
            })
        lead["revisions"] = [{
            "revision": int(item["revision"]), "subject": item["subject"], "driverLogic": item["driver_logic"],
            "conditions": item["conditions"], "assets": _decoded(item["assets_json"], []),
            "sourceClaims": _decoded(item["source_claims_json"], []), "actualModel": item["actual_model"],
            "createdAt": item["created_at"],
        } for item in revisions]
        return lead

    @staticmethod
    def _lead_payload(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"], "subject": row["subject"], "driverLogic": row["driver_logic"],
            "conditions": row["conditions"], "assets": _decoded(row["assets_json"], []),
            "sourceClaims": _decoded(row["source_claims_json"], []),
            "firstSeenAt": row["first_seen_at"], "lastSubstantiveAt": row["last_substantive_at"],
            "revision": int(row["revision"]), "status": row["status"], "accounts": row.get("accounts") or "",
        }


def _terms(value: str) -> set[str]:
    import re

    stopwords = {"的", "了", "和", "与", "在", "将", "对", "为", "是", "并", "或", "开始", "表示", "可能", "公司"}
    return {part.casefold() for part in re.findall(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}", value)
            if part.casefold() not in stopwords}


def _identity_asset_key(name: str) -> str:
    normalized = " ".join(name.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def _has_readable_material(material: dict[str, Any]) -> bool:
    if str(material.get("text") or "").strip():
        return True
    for key in ("related_materials", "supplement_materials"):
        entries = material.get(key) if isinstance(material.get(key), list) else []
        if any(isinstance(item, dict) and str(item.get("text") or "").strip() for item in entries):
            return True
    return False


def run_worker(database_path: Path, *, once: bool = False) -> int:
    interval = max(1, _env_int("X_AGENT_INVESTMENT_LEADS_WORKER_INTERVAL_SECONDS", 10, 1, 60))
    service = InvestmentLeadService(database_path)
    try:
        if once:
            print(json.dumps(service.process_once(), ensure_ascii=False))
            return 0
        print("[odaily] X Agent investment leads worker started")
        while True:
            try:
                result = service.process_once()
                if result["scanned"] or result["derived"] or result["supplemented"] or result["identitySearched"]:
                    print(json.dumps(result, ensure_ascii=False))
            except Exception as exc:
                print(f"[odaily] investment leads worker error: {type(exc).__name__}: {exc}")
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        service.close()
