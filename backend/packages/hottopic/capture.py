from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


FXTWITTER_BASE_URL = "https://api.fxtwitter.com"
DEFAULT_OUTPUT_ROOT = Path("data/runtime/hottopic")


@dataclass
class AccountRow:
    screen_name: str
    name: str
    description: str
    followers_count: int
    statuses_count: int
    protected: bool
    url: str


@dataclass
class ContentItem:
    account_screen_name: str
    account_name: str
    tweet_id: str
    activity_type: str
    author_screen_name: str
    author_name: str
    created_at: str
    created_at_iso: str
    url: str
    text: str
    expanded_text: str
    quote_id: str | None
    quote_author_screen_name: str | None
    quote_text: str | None
    reply_to: str | None
    replies: int
    reposts: int
    quotes: int
    likes: int
    views: int


def int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def bool_value(value: Any) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes"}


def compact_text(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    return ""


def nested_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def author_username(payload: dict[str, Any]) -> str:
    return str(nested_dict(payload.get("author")).get("screen_name") or "").strip().lstrip("@")


def author_name(payload: dict[str, Any]) -> str:
    author = nested_dict(payload.get("author"))
    return str(author.get("name") or "").strip() or author_username(payload)


def find_quote(payload: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("quote", "quoted_tweet", "quote_tweet", "quoted_status"):
        value = payload.get(key)
        if isinstance(value, dict) and value:
            return value
    return None


def parse_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.strptime(raw, fmt).astimezone(UTC)
        except ValueError:
            continue
    return None


def load_accounts(path: Path) -> list[AccountRow]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    accounts: list[AccountRow] = []
    seen: set[str] = set()
    for row in rows:
        screen_name = str(row.get("screen_name") or row.get("username") or "").strip().lstrip("@")
        if not screen_name or screen_name.lower() in seen:
            continue
        seen.add(screen_name.lower())
        accounts.append(
            AccountRow(
                screen_name=screen_name,
                name=str(row.get("name") or "").strip(),
                description=str(row.get("description") or "").strip(),
                followers_count=int_value(row.get("followers_count")),
                statuses_count=int_value(row.get("statuses_count") or row.get("tweet_count")),
                protected=bool_value(row.get("protected")),
                url=str(row.get("url") or f"https://twitter.com/{screen_name}").strip(),
            )
        )
    return accounts


def fetch_json(url: str, params: dict[str, Any] | None = None, *, timeout: float = 25.0) -> dict[str, Any]:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "hottopic-recent-activity-filter/0.1",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def is_pure_repost(account: str, item: dict[str, Any]) -> bool:
    reposted_by = nested_dict(item.get("reposted_by"))
    reposted_by_username = str(reposted_by.get("screen_name") or "").strip().lstrip("@")
    original_author = author_username(item)
    return bool(
        reposted_by_username
        and reposted_by_username.lower() == account.lower()
        and original_author
        and original_author.lower() != account.lower()
    )


def reply_target(item: dict[str, Any]) -> str | None:
    replying_to = item.get("replying_to")
    if isinstance(replying_to, str) and replying_to.strip():
        return replying_to.strip()
    if isinstance(replying_to, list) and replying_to:
        return ",".join(str(value).strip() for value in replying_to if str(value).strip()) or None
    status = nested_dict(item.get("replying_to_status"))
    status_id = str(status.get("id") or "").strip()
    return status_id or None


def item_to_content(account: AccountRow, item: dict[str, Any], *, cutoff: datetime) -> ContentItem | None:
    if is_pure_repost(account.screen_name, item):
        return None
    created = parse_created_at(str(item.get("created_at") or ""))
    if created is None or created < cutoff:
        return None

    tweet_id = str(item.get("id") or "").strip()
    text = compact_text(item.get("text") or item.get("raw_text"))
    quote = find_quote(item)
    quote_id = None
    quote_author = None
    quote_text = None
    expanded_parts: list[str] = []
    if text:
        expanded_parts.append(text)
    if quote:
        quote_id = str(quote.get("id") or "").strip() or None
        quote_author = author_username(quote) or None
        quote_text = compact_text(quote.get("text") or quote.get("raw_text")) or None
        if quote_text:
            expanded_parts.append(f"引用 @{quote_author or 'unknown'}: {quote_text}")
    expanded_text = "\n".join(expanded_parts).strip()
    if not tweet_id or not expanded_text:
        return None

    reply_to = reply_target(item)
    activity_type = "quote" if quote else "reply" if reply_to else "original"
    return ContentItem(
        account_screen_name=account.screen_name,
        account_name=account.name,
        tweet_id=tweet_id,
        activity_type=activity_type,
        author_screen_name=author_username(item),
        author_name=author_name(item),
        created_at=str(item.get("created_at") or "").strip(),
        created_at_iso=created.isoformat(),
        url=str(item.get("url") or f"https://x.com/{author_username(item)}/status/{tweet_id}"),
        text=text,
        expanded_text=expanded_text,
        quote_id=quote_id,
        quote_author_screen_name=quote_author,
        quote_text=quote_text,
        reply_to=reply_to,
        replies=int_value(item.get("replies")),
        reposts=int_value(item.get("reposts") or item.get("retweets")),
        quotes=int_value(item.get("quotes")),
        likes=int_value(item.get("likes")),
        views=int_value(item.get("views")),
    )


def latest_post_snapshot(account: AccountRow, results: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for raw in results:
        created = parse_created_at(str(raw.get("created_at") or ""))
        if created is not None:
            candidates.append((created, raw))
    if not candidates:
        return None
    created, raw = max(candidates, key=lambda pair: pair[0])
    tweet_id = str(raw.get("id") or "").strip()
    author = author_username(raw) or account.screen_name
    return {
        "latest_post_id": tweet_id,
        "latest_post_created_at": str(raw.get("created_at") or "").strip(),
        "latest_post_created_at_iso": created.isoformat(),
        "latest_post_url": str(raw.get("url") or f"https://x.com/{author}/status/{tweet_id}"),
        "latest_post_activity_type": "repost" if is_pure_repost(account.screen_name, raw) else "post",
        "latest_post_text": compact_text(raw.get("text") or raw.get("raw_text")),
    }


def retry_after_seconds(error: urllib.error.HTTPError, *, now: datetime | None = None) -> float:
    """Parse either permitted Retry-After form into a non-negative delay."""
    raw = error.headers.get("Retry-After") if error.headers is not None else None
    if raw is None:
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return 0.0
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return max(0.0, (retry_at.astimezone(UTC) - reference.astimezone(UTC)).total_seconds())


def scan_account(
    account: AccountRow,
    *,
    cutoff: datetime,
    timeline_count: int,
    retries: int,
    before_request: Callable[[], None] | None = None,
    on_rate_limited: Callable[[float], None] | None = None,
) -> tuple[list[ContentItem], dict[str, Any] | None, dict[str, Any] | None]:
    if account.protected:
        return [], None, {"account": account.screen_name, "kind": "protected", "error": "protected account skipped"}
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            if before_request is not None:
                before_request()
            payload = fetch_json(
                f"{FXTWITTER_BASE_URL}/2/profile/{account.screen_name}/statuses",
                {"count": max(1, min(timeline_count, 100))},
            )
            results = payload.get("results")
            if not isinstance(results, list):
                return [], None, {"account": account.screen_name, "kind": "parse_empty", "error": "missing results"}
            raw_results = [raw for raw in results if isinstance(raw, dict)]
            latest = latest_post_snapshot(account, raw_results)
            items = [
                content
                for raw in raw_results
                for content in [item_to_content(account, raw, cutoff=cutoff)]
                if content is not None
            ]
            return items, latest, None
        except Exception as exc:
            last_error = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                # Do not spend the account retry budget inside an upstream
                # cooldown. Let the worker pause every account immediately
                # and reschedule this one with its persisted backoff instead.
                if on_rate_limited is not None:
                    on_rate_limited(retry_after_seconds(exc))
                break
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
    rate_limited = isinstance(last_error, urllib.error.HTTPError) and last_error.code == 429
    error: dict[str, Any] = {
        "account": account.screen_name,
        "kind": "rate_limited" if rate_limited else "fetch_failed",
        "error": str(last_error),
    }
    if rate_limited:
        retry_after = retry_after_seconds(last_error)
        if retry_after > 0:
            error["retry_after_seconds"] = retry_after
    return [], None, error


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def find_default_csv() -> Path:
    matches = sorted(Path.cwd().glob("twitter-*.csv"))
    if not matches:
        raise FileNotFoundError("No twitter-*.csv file found in current workspace")
    return matches[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Filter followed X accounts with activity in the last N hours.")
    parser.add_argument("--csv", default="", help="Path to following CSV. Defaults to twitter-*.csv in cwd.")
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--timeline-count", type=int, default=50)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--stale-days", type=int, default=90)
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.csv) if args.csv else find_default_csv()
    accounts = load_accounts(input_path)
    if args.limit:
        accounts = accounts[: args.limit]

    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=args.hours)
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_root) / run_id

    all_items: list[ContentItem] = []
    errors: list[dict[str, Any]] = []
    latest_posts: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                scan_account,
                account,
                cutoff=cutoff,
                timeline_count=args.timeline_count,
                retries=args.retries,
            ): account
            for account in accounts
        }
        for index, future in enumerate(as_completed(futures), 1):
            account = futures[future]
            try:
                items, latest, error = future.result()
            except Exception as exc:
                items, latest, error = [], None, {"account": account.screen_name, "kind": "unexpected", "error": str(exc)}
            all_items.extend(items)
            if latest:
                latest_posts[account.screen_name] = latest
            if error:
                errors.append(error)
            if index % 50 == 0:
                print(f"scanned={index}/{len(accounts)} active_accounts={len({item.account_screen_name for item in all_items})}", file=sys.stderr)

    all_items.sort(key=lambda item: item.created_at_iso, reverse=True)
    items_by_account: dict[str, list[ContentItem]] = {}
    for item in all_items:
        items_by_account.setdefault(item.account_screen_name, []).append(item)

    account_lookup = {account.screen_name: account for account in accounts}
    stale_cutoff = now - timedelta(days=args.stale_days)
    account_activity: list[dict[str, Any]] = []
    for account in accounts:
        latest = latest_posts.get(account.screen_name, {})
        latest_iso = str(latest.get("latest_post_created_at_iso") or "")
        account_activity.append(
            {
                "screen_name": account.screen_name,
                "name": account.name,
                "followers_count": account.followers_count,
                "url": account.url,
                **latest,
                "active_in_window": account.screen_name in items_by_account,
                "stale_90d": bool(latest_iso and datetime.fromisoformat(latest_iso) < stale_cutoff),
                "scan_status": "ok" if account.screen_name in latest_posts else "error_or_protected",
            }
        )
    stale_accounts = [row for row in account_activity if row["stale_90d"]]
    active_accounts = []
    for screen_name, items in sorted(items_by_account.items(), key=lambda pair: (len(pair[1]), pair[1][0].created_at_iso), reverse=True):
        account = account_lookup[screen_name]
        active_accounts.append(
            {
                "screen_name": screen_name,
                "name": account.name,
                "followers_count": account.followers_count,
                "recent_item_count": len(items),
                "latest_created_at": items[0].created_at,
                "latest_created_at_iso": items[0].created_at_iso,
                "original_count": sum(1 for item in items if item.activity_type == "original"),
                "quote_count": sum(1 for item in items if item.activity_type == "quote"),
                "reply_count": sum(1 for item in items if item.activity_type == "reply"),
                "url": account.url,
                **latest_posts.get(screen_name, {}),
            }
        )
    inactive_accounts = [
        {
            "screen_name": account.screen_name,
            "name": account.name,
            "followers_count": account.followers_count,
            "protected": account.protected,
            "url": account.url,
            **latest_posts.get(account.screen_name, {}),
        }
        for account in accounts
        if account.screen_name not in items_by_account
    ]

    summary = {
        "run_id": run_id,
        "input_csv": str(input_path),
        "hours": args.hours,
        "timeline_count": args.timeline_count,
        "workers": args.workers,
        "started_reference_utc": now.isoformat(),
        "cutoff_utc": cutoff.isoformat(),
        "input_account_count": len(accounts),
        "active_account_count": len(active_accounts),
        "inactive_account_count": len(inactive_accounts),
        "content_item_count": len(all_items),
        "error_count": len(errors),
        "stale_days": args.stale_days,
        "stale_account_count": len(stale_accounts),
        "unresolved_account_count": sum(1 for row in account_activity if row["scan_status"] != "ok"),
        "activity_type_counts": {
            "original": sum(1 for item in all_items if item.activity_type == "original"),
            "quote": sum(1 for item in all_items if item.activity_type == "quote"),
            "reply": sum(1 for item in all_items if item.activity_type == "reply"),
        },
        "top_active_accounts": active_accounts[:30],
    }

    write_json(output_dir / "summary.json", summary)
    write_json(output_dir / "content_items.json", [asdict(item) for item in all_items])
    write_json(output_dir / "active_accounts.json", active_accounts)
    write_json(output_dir / "inactive_accounts.json", inactive_accounts)
    write_json(output_dir / "account_activity.json", account_activity)
    write_json(output_dir / "stale_accounts_90d.json", stale_accounts)
    write_json(output_dir / "errors.json", errors)
    write_csv(output_dir / "active_accounts.csv", active_accounts, list(active_accounts[0].keys()) if active_accounts else ["screen_name"])
    content_rows = [asdict(item) for item in all_items]
    write_csv(output_dir / "content_items.csv", content_rows, list(content_rows[0].keys()) if content_rows else ["tweet_id"])
    write_csv(output_dir / "inactive_accounts.csv", inactive_accounts, list(inactive_accounts[0].keys()) if inactive_accounts else ["screen_name"])
    write_csv(output_dir / "account_activity.csv", account_activity, list(account_activity[0].keys()) if account_activity else ["screen_name"])
    write_csv(output_dir / "stale_accounts_90d.csv", stale_accounts, list(stale_accounts[0].keys()) if stale_accounts else ["screen_name"])

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
