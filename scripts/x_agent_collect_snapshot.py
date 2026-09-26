#!/usr/bin/env python3
"""Collect a resumable, local-only X Agent post snapshot from an explicit CSV."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import threading
import time
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


FXTWITTER_BASE_URL = "https://api.fxtwitter.com"
CHECKPOINT_VERSION = 1


@dataclass(frozen=True)
class Account:
    handle: str
    display_name: str
    profile_url: str
    protected: bool


class RequestPacer:
    """Coordinate a conservative request interval across collection workers."""

    def __init__(self, minimum_interval: float) -> None:
        self.minimum_interval = max(0.0, minimum_interval)
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait_turn(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed_at - now)
            self._next_allowed_at = max(now, self._next_allowed_at) + self.minimum_interval
        if delay:
            time.sleep(delay)

    def defer(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            self._next_allowed_at = max(self._next_allowed_at, time.monotonic() + seconds)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (TypeError, ValueError, IndexError):
        return None


def compact_text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def nested_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def bool_value(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def load_accounts(path: Path) -> list[Account]:
    accounts: list[Account] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            raw_handle = str(row.get("screen_name") or row.get("username") or "").strip().lstrip("@")
            lower = raw_handle.lower()
            if not raw_handle or lower in seen:
                continue
            seen.add(lower)
            accounts.append(
                Account(
                    handle=raw_handle,
                    display_name=str(row.get("name") or row.get("display_name") or "").strip(),
                    profile_url=str(row.get("url") or f"https://x.com/{raw_handle}").strip(),
                    protected=bool_value(row.get("protected")),
                )
            )
    if not accounts:
        raise ValueError("account CSV contains no usable screen_name or username values")
    return accounts


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"checkpoint is invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"checkpoint is not an object: {path}")
    return payload


def username(payload: dict[str, Any]) -> str:
    return str(nested_dict(payload.get("author")).get("screen_name") or "").strip().lstrip("@")


def name(payload: dict[str, Any]) -> str:
    author = nested_dict(payload.get("author"))
    return str(author.get("name") or "").strip() or username(payload)


def is_pure_repost(account: Account, payload: dict[str, Any]) -> bool:
    reposted_by = nested_dict(payload.get("reposted_by"))
    reposting_handle = str(reposted_by.get("screen_name") or "").strip().lstrip("@")
    author_handle = username(payload)
    return bool(
        reposting_handle
        and reposting_handle.lower() == account.handle.lower()
        and author_handle
        and author_handle.lower() != account.handle.lower()
    )


def quote_from(payload: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("quote", "quoted_tweet", "quote_tweet", "quoted_status"):
        candidate = payload.get(key)
        if isinstance(candidate, dict) and candidate:
            return candidate
    return None


def reply_target(payload: dict[str, Any]) -> str | None:
    target = payload.get("replying_to")
    if isinstance(target, str) and target.strip():
        return target.strip()
    if isinstance(target, list):
        joined = ",".join(str(value).strip() for value in target if str(value).strip())
        return joined or None
    status = nested_dict(payload.get("replying_to_status"))
    value = str(status.get("id") or "").strip()
    return value or None


def to_content_item(account: Account, payload: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    if is_pure_repost(account, payload):
        return None
    created_raw = str(payload.get("created_at") or "").strip()
    created_at = parse_time(created_raw)
    if created_at is None or created_at < cutoff:
        return None
    tweet_id = str(payload.get("id") or "").strip()
    text = compact_text(payload.get("text") or payload.get("raw_text"))
    quote = quote_from(payload)
    quote_id = quote_author = quote_text = None
    expanded_parts = [text] if text else []
    if quote:
        quote_id = str(quote.get("id") or "").strip() or None
        quote_author = username(quote) or None
        quote_text = compact_text(quote.get("text") or quote.get("raw_text")) or None
        if quote_text:
            expanded_parts.append(f"Quoted @{quote_author or 'unknown'}: {quote_text}")
    expanded_text = "\n".join(expanded_parts).strip()
    if not tweet_id or not expanded_text:
        return None
    replying_to = reply_target(payload)
    activity_type = "quote" if quote else "reply" if replying_to else "original"
    return {
        "account_screen_name": account.handle,
        "account_name": account.display_name,
        "tweet_id": tweet_id,
        "activity_type": activity_type,
        "author_screen_name": username(payload),
        "author_name": name(payload),
        "created_at": created_raw,
        "created_at_iso": iso(created_at),
        "url": str(payload.get("url") or f"https://x.com/{username(payload) or account.handle}/status/{tweet_id}"),
        "text": text,
        "expanded_text": expanded_text,
        "quote_id": quote_id,
        "quote_author_screen_name": quote_author,
        "quote_text": quote_text,
        "reply_to": replying_to,
        "replies": int_value(payload.get("replies")),
        "reposts": int_value(payload.get("reposts") or payload.get("retweets")),
        "quotes": int_value(payload.get("quotes")),
        "likes": int_value(payload.get("likes")),
        "views": int_value(payload.get("views")),
    }


def int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def retry_after_seconds(error: HTTPError) -> float:
    value = error.headers.get("Retry-After") if error.headers else None
    try:
        return max(1.0, float(value)) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def fetch_account(
    account: Account,
    *,
    cutoff: datetime,
    timeline_count: int,
    retries: int,
    timeout: float,
    pacer: RequestPacer,
) -> dict[str, Any]:
    if account.protected:
        return {
            "status": "protected",
            "attempts": 0,
            "completed_at": iso(utc_now()),
            "items": [],
            "error": {"kind": "protected", "message": "protected account skipped"},
        }
    url = f"{FXTWITTER_BASE_URL}/2/profile/{urllib.parse.quote(account.handle, safe='')}/statuses?{urllib.parse.urlencode({'count': timeline_count})}"
    last_error: dict[str, Any] | None = None
    for attempt in range(1, retries + 2):
        pacer.wait_turn()
        try:
            request = Request(url, headers={"User-Agent": "odaily-x-agent-local-snapshot/1.0", "Accept": "application/json"})
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list):
                raise ValueError("response has no results list")
            raw_posts = [item for item in results if isinstance(item, dict)]
            dated_posts = [(parsed, item) for item in raw_posts if (parsed := parse_time(item.get("created_at"))) is not None]
            oldest = min((item[0] for item in dated_posts), default=None)
            latest = max((item[0] for item in dated_posts), default=None)
            items = [item for raw in raw_posts if (item := to_content_item(account, raw, cutoff)) is not None]
            return {
                "status": "success",
                "attempts": attempt,
                "completed_at": iso(utc_now()),
                "items": items,
                "returned_posts": len(raw_posts),
                "latest_returned_at": iso(latest) if latest else None,
                "oldest_returned_at": iso(oldest) if oldest else None,
                "timeline_capped": bool(len(raw_posts) >= timeline_count and oldest and oldest > cutoff),
                "error": None,
            }
        except HTTPError as error:
            cooldown = retry_after_seconds(error)
            if error.code == 429:
                cooldown = max(cooldown, min(60.0, 2.0 ** (attempt - 1)))
            elif cooldown <= 0:
                cooldown = min(30.0, 2.0 ** (attempt - 1))
            pacer.defer(cooldown)
            last_error = {"kind": "http", "code": error.code, "message": str(error), "retry_after_seconds": cooldown}
        except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
            cooldown = min(30.0, 2.0 ** (attempt - 1))
            pacer.defer(cooldown)
            last_error = {"kind": type(error).__name__, "message": str(error), "retry_after_seconds": cooldown}
    return {
        "status": "failed",
        "attempts": retries + 1,
        "completed_at": iso(utc_now()),
        "items": [],
        "error": last_error or {"kind": "unknown", "message": "collection failed"},
    }


def account_record(account: Account, result: dict[str, Any]) -> dict[str, Any]:
    return {"account": asdict(account), **result}


def counts(records: dict[str, Any]) -> dict[str, int]:
    output: dict[str, int] = {}
    for value in records.values():
        status = str(value.get("status") or "unknown") if isinstance(value, dict) else "invalid"
        output[status] = output.get(status, 0) + 1
    return output


def write_snapshot_outputs(output_dir: Path, checkpoint: dict[str, Any], accounts: list[Account]) -> dict[str, Any]:
    records = checkpoint.get("accounts") if isinstance(checkpoint.get("accounts"), dict) else {}
    content_by_id: dict[str, dict[str, Any]] = {}
    activity: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    capped = 0
    for account in accounts:
        record = records.get(account.handle.lower())
        if not isinstance(record, dict):
            activity.append({**asdict(account), "status": "pending", "post_count": 0, "timeline_capped": False})
            continue
        for item in record.get("items") or []:
            if isinstance(item, dict) and item.get("tweet_id"):
                content_by_id[str(item["tweet_id"])] = item
        was_capped = bool(record.get("timeline_capped"))
        capped += int(was_capped)
        activity.append(
            {
                **asdict(account),
                "status": record.get("status"),
                "post_count": len(record.get("items") or []),
                "returned_posts": record.get("returned_posts"),
                "latest_returned_at": record.get("latest_returned_at"),
                "oldest_returned_at": record.get("oldest_returned_at"),
                "timeline_capped": was_capped,
                "completed_at": record.get("completed_at"),
                "error": record.get("error"),
            }
        )
        if record.get("status") == "failed":
            errors.append({"handle": account.handle, "error": record.get("error"), "completed_at": record.get("completed_at")})
    items = sorted(content_by_id.values(), key=lambda item: str(item.get("created_at_iso") or ""), reverse=True)
    configuration = checkpoint.get("configuration") or {}
    as_of = parse_time(configuration.get("as_of"))
    cutoff = parse_time(configuration.get("cutoff"))
    status_counts = counts(records)
    summary = {
        "source": "local FXTwitter snapshot from explicit account CSV",
        "accounts_file": configuration.get("accounts_file"),
        "accounts_sha256": configuration.get("accounts_sha256"),
        "sample_window": {"cutoff": iso(cutoff) if cutoff else None, "as_of": iso(as_of) if as_of else None},
        "timeline_count": configuration.get("timeline_count"),
        "input_account_count": len(accounts),
        "status_counts": status_counts,
        "pending_accounts": len(accounts) - sum(status_counts.values()),
        "content_item_count": len(items),
        "timeline_capped_accounts": capped,
        "failed_accounts": len(errors),
        "snapshot_complete": len(accounts) == sum(status_counts.values()) and not errors,
        "updated_at": iso(utc_now()),
        "checkpoint": str((output_dir / "checkpoint.json").resolve()),
    }
    atomic_write_json(output_dir / "content_items.json", items)
    atomic_write_json(output_dir / "account_activity.json", activity)
    atomic_write_json(output_dir / "errors.json", errors)
    atomic_write_json(output_dir / "summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect a resumable local X Agent snapshot from an explicit account CSV.")
    parser.add_argument("--accounts", required=True, type=Path, help="Existing X Agent account CSV; no source database is read.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Ignored local directory for checkpoint and snapshot artifacts.")
    parser.add_argument("--as-of", help="UTC ISO timestamp. A resumed checkpoint keeps its original window automatically.")
    parser.add_argument("--days", type=int, default=30, help="Snapshot window length, default 30 days.")
    parser.add_argument("--timeline-count", type=int, default=100, help="FXTwitter statuses count per account, maximum 100.")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent requests. Start with one to avoid public API rate limiting.")
    parser.add_argument("--request-interval", type=float, default=1.0, help="Minimum global seconds between requests.")
    parser.add_argument("--retries", type=int, default=2, help="Retries per account after the first attempt.")
    parser.add_argument("--timeout", type=float, default=25.0, help="Per-request timeout in seconds.")
    parser.add_argument("--limit", type=int, default=0, help="Only process the first N accounts, useful for a probe.")
    parser.add_argument("--stop-after", type=int, default=0, help="Process at most N new accounts this invocation, then leave a resumable checkpoint.")
    parser.add_argument("--retry-failed", action="store_true", help="Retry failed accounts already present in the checkpoint.")
    return parser.parse_args()


def validate_or_create_checkpoint(
    checkpoint: dict[str, Any],
    *,
    accounts_file: Path,
    accounts_hash: str,
    as_of: datetime,
    cutoff: datetime,
    timeline_count: int,
) -> dict[str, Any]:
    configuration = checkpoint.get("configuration") if isinstance(checkpoint.get("configuration"), dict) else None
    if configuration:
        expected = {
            "accounts_file": str(accounts_file),
            "accounts_sha256": accounts_hash,
            "timeline_count": timeline_count,
        }
        incompatible = [key for key, value in expected.items() if configuration.get(key) != value]
        if incompatible:
            raise ValueError("checkpoint input changed for " + ", ".join(incompatible) + "; use a new output directory")
        stored_as_of = parse_time(configuration.get("as_of"))
        stored_cutoff = parse_time(configuration.get("cutoff"))
        if stored_as_of is None or stored_cutoff is None:
            raise ValueError("checkpoint has no valid sample window")
        if as_of != stored_as_of or cutoff != stored_cutoff:
            raise ValueError("checkpoint window differs; omit --as-of when resuming or use a new output directory")
        checkpoint.setdefault("accounts", {})
        return checkpoint
    return {
        "version": CHECKPOINT_VERSION,
        "configuration": {
            "accounts_file": str(accounts_file),
            "accounts_sha256": accounts_hash,
            "as_of": iso(as_of),
            "cutoff": iso(cutoff),
            "timeline_count": timeline_count,
        },
        "accounts": {},
        "created_at": iso(utc_now()),
    }


def main() -> int:
    args = parse_args()
    if args.days <= 0 or args.timeline_count <= 0 or args.workers <= 0:
        raise SystemExit("--days, --timeline-count, and --workers must be positive")
    accounts_file = args.accounts.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    accounts = load_accounts(accounts_file)
    if args.limit:
        accounts = accounts[: max(0, args.limit)]
    checkpoint_path = output_dir / "checkpoint.json"
    checkpoint = load_checkpoint(checkpoint_path)
    configuration = checkpoint.get("configuration") if isinstance(checkpoint.get("configuration"), dict) else {}
    stored_as_of = parse_time(configuration.get("as_of"))
    as_of = parse_time(args.as_of) if args.as_of else stored_as_of or utc_now()
    if as_of is None:
        raise SystemExit("--as-of must be a valid ISO timestamp")
    cutoff = as_of - timedelta(days=args.days)
    checkpoint = validate_or_create_checkpoint(
        checkpoint,
        accounts_file=accounts_file,
        accounts_hash=file_hash(accounts_file),
        as_of=as_of,
        cutoff=cutoff,
        timeline_count=min(100, args.timeline_count),
    )
    records = checkpoint.setdefault("accounts", {})
    pending = [
        account
        for account in accounts
        if not isinstance(records.get(account.handle.lower()), dict)
        or (args.retry_failed and records[account.handle.lower()].get("status") == "failed")
    ]
    if args.stop_after:
        pending = pending[: max(0, args.stop_after)]
    pacer = RequestPacer(args.request_interval)
    completed = 0

    def persist() -> None:
        checkpoint["updated_at"] = iso(utc_now())
        atomic_write_json(checkpoint_path, checkpoint)

    persist()
    futures: dict[Future[dict[str, Any]], Account] = {}
    iterator = iter(pending)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while True:
            while len(futures) < args.workers:
                try:
                    account = next(iterator)
                except StopIteration:
                    break
                future = pool.submit(
                    fetch_account,
                    account,
                    cutoff=cutoff,
                    timeline_count=min(100, args.timeline_count),
                    retries=max(0, args.retries),
                    timeout=max(1.0, args.timeout),
                    pacer=pacer,
                )
                futures[future] = account
            if not futures:
                break
            done, _ = wait(futures, return_when="FIRST_COMPLETED")
            for future in done:
                account = futures.pop(future)
                try:
                    result = future.result()
                except Exception as exc:  # Keep one bad account from losing the full run.
                    result = {
                        "status": "failed",
                        "attempts": 1,
                        "completed_at": iso(utc_now()),
                        "items": [],
                        "error": {"kind": type(exc).__name__, "message": str(exc)},
                    }
                records[account.handle.lower()] = account_record(account, result)
                completed += 1
                persist()
                print(f"completed={completed}/{len(pending)} handle=@{account.handle} status={result['status']}", file=sys.stderr)
    summary = write_snapshot_outputs(output_dir, checkpoint, accounts)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
