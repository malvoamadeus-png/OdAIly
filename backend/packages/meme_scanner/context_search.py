from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telethon import TelegramClient

from packages.common.paths import get_paths
from packages.meme_scanner.telegram_helpers import (
    display_name,
    load_name_list,
    parse_config,
    resolve_proxy,
)

PATHS = get_paths()
CONFIG_DIR = PATHS.config_dir
EXPORTS_DATA_DIR = PATHS.exports_dir
DEFAULT_CONFIG = CONFIG_DIR / "meme_telegram.txt"
DEFAULT_SESSION = PATHS.processed_dir / "meme_telegram_narrative"
load_chat_name_list = load_name_list


@dataclass
class MessageRow:
    chat_title: str
    chat_username: str | None
    message_id: int
    sent_at: datetime
    sender_name: str
    sender_username: str | None
    text: str


@dataclass
class SearchHit:
    entity: Any
    row: MessageRow
    matched_terms: list[str]


def setup_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass


def hit_reason(text: str, terms: list[str]) -> list[str]:
    lowered = text.casefold()
    return [term for term in terms if term.casefold() in lowered]


def one_line(text: str, max_len: int = 260) -> str:
    value = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if len(value) > max_len:
        return value[: max_len - 3] + "..."
    return value


def is_automated_sender(sender: Any) -> bool:
    """Telegram marks bot accounts explicitly; never persist their messages."""
    username = str(getattr(sender, "username", "") or "").casefold()
    return bool(getattr(sender, "bot", False)) or username.endswith("_bot") or username.endswith("bot")


async def row_from_message(
    message: Any,
    chat_title: str,
    chat_username: str | None,
    excluded_sender_ids: set[int] | None = None,
) -> MessageRow | None:
    if not message or not message.date:
        return None
    text = message.message or ""
    if not text.strip():
        return None
    sender_id = getattr(message, "sender_id", None)
    if sender_id is not None and sender_id in (excluded_sender_ids or set()):
        return None
    sender = await message.get_sender()
    if is_automated_sender(sender):
        return None
    return MessageRow(
        chat_title=chat_title,
        chat_username=chat_username,
        message_id=message.id,
        sent_at=message.date.astimezone(timezone.utc),
        sender_name=display_name(sender),
        sender_username=getattr(sender, "username", None),
        text=text,
    )


async def collect_chat_messages(
    client: TelegramClient,
    entity: Any,
    chat_title: str,
    chat_username: str | None,
    limit: int,
    since: datetime | None,
    excluded_sender_ids: set[int] | None = None,
) -> list[MessageRow]:
    rows: list[MessageRow] = []
    async for message in client.iter_messages(entity, limit=limit):
        if not message.date or (since is not None and message.date < since):
            continue
        text = message.message or ""
        if not text.strip():
            continue
        sender_id = getattr(message, "sender_id", None)
        if sender_id is not None and sender_id in (excluded_sender_ids or set()):
            continue
        sender = await message.get_sender()
        if is_automated_sender(sender):
            continue
        rows.append(
            MessageRow(
                chat_title=chat_title,
                chat_username=chat_username,
                message_id=message.id,
                sent_at=message.date.astimezone(timezone.utc),
                sender_name=display_name(sender),
                sender_username=getattr(sender, "username", None),
                text=text,
            )
        )
    rows.sort(key=lambda item: item.sent_at)
    return rows


async def collect_context_window(
    client: TelegramClient,
    entity: Any,
    hit_message_id: int,
    chat_title: str,
    chat_username: str | None,
    before: int,
    after: int,
    excluded_sender_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    ids = [
        message_id
        for message_id in range(hit_message_id - before, hit_message_id + after + 1)
        if message_id > 0
    ]
    messages = await client.get_messages(entity, ids=ids)
    by_id = {
        message.id: message
        for message in messages
        if message is not None and getattr(message, "id", None) is not None
    }
    context: list[dict[str, Any]] = []
    for message_id in ids:
        message = by_id.get(message_id)
        row = await row_from_message(
            message,
            chat_title,
            chat_username,
            excluded_sender_ids,
        )
        if row is None:
            continue
        context.append(
            {
                "offset": row.message_id - hit_message_id,
                "message_id": row.message_id,
                "sent_at": row.sent_at.isoformat(),
                "sender_name": row.sender_name,
                "sender_username": row.sender_username,
                "text": row.text,
            }
        )
    return context


async def search_chat_messages(
    client: TelegramClient,
    entity: Any,
    chat_title: str,
    chat_username: str | None,
    terms: list[str],
    *,
    since: datetime | None,
    search_limit_per_term: int | None,
    excluded_sender_ids: set[int] | None = None,
) -> tuple[list[SearchHit], int, int]:
    hits: list[SearchHit] = []
    seen_message_ids: set[int] = set()
    searched_messages = 0
    hit_count = 0

    for term in terms:
        async for message in client.iter_messages(
            entity,
            search=term,
            limit=search_limit_per_term,
        ):
            searched_messages += 1
            if not message.date:
                continue
            sent_at = message.date.astimezone(timezone.utc)
            if since is not None and sent_at < since:
                break
            if message.id in seen_message_ids:
                continue
            row = await row_from_message(
                message,
                chat_title,
                chat_username,
                excluded_sender_ids,
            )
            if row is None:
                continue
            seen_message_ids.add(message.id)
            hit_count += 1
            reasons = hit_reason(row.text, terms) or [term]
            hits.append(SearchHit(entity=entity, row=row, matched_terms=reasons))
    return hits, searched_messages, hit_count


def select_newest_hits(hits: list[SearchHit], max_contexts: int) -> list[SearchHit]:
    """Apply the cap after all chats have been searched, never per chat."""
    selected = sorted(
        hits,
        key=lambda hit: (hit.row.sent_at, hit.row.chat_title, hit.row.message_id),
        reverse=True,
    )
    return selected if max_contexts < 0 else selected[:max_contexts]


def select_edge_hits(
    hits: list[SearchHit],
    *,
    newest_contexts: int,
    oldest_contexts: int,
) -> list[SearchHit]:
    """Select newest and oldest hits globally, deduplicating an overlap."""
    newest = select_newest_hits(hits, newest_contexts)
    oldest = sorted(
        hits,
        key=lambda hit: (hit.row.sent_at, hit.row.chat_title, hit.row.message_id),
    )
    if oldest_contexts >= 0:
        oldest = oldest[:oldest_contexts]
    selected: dict[tuple[str, int], SearchHit] = {}
    for hit in newest + oldest:
        selected[(hit.row.chat_title, hit.row.message_id)] = hit
    return sorted(
        selected.values(),
        key=lambda hit: (hit.row.sent_at, hit.row.chat_title, hit.row.message_id),
        reverse=True,
    )


async def materialize_contexts(
    client: TelegramClient,
    hits: list[SearchHit],
    *,
    before: int,
    after: int,
    excluded_sender_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    for hit in hits:
        row = hit.row
        contexts.append(
            {
                "chat_title": row.chat_title,
                "chat_username": row.chat_username,
                "message_id": row.message_id,
                "sent_at": row.sent_at.isoformat(),
                "matched_terms": hit.matched_terms,
                "context": await collect_context_window(
                    client,
                    hit.entity,
                    row.message_id,
                    row.chat_title,
                    row.chat_username,
                    before,
                    after,
                    excluded_sender_ids,
                ),
            }
        )
    return contexts


async def run(args: argparse.Namespace) -> None:
    api_id, api_hash = parse_config(Path(args.config))
    proxy = resolve_proxy(args.proxy)
    allowed_chats = load_chat_name_list(Path(args.allowed_chats))
    if not allowed_chats:
        raise RuntimeError(f"No allowed chats configured in {args.allowed_chats}")

    terms = [term for term in args.term if term.strip()]
    if not terms:
        raise RuntimeError("At least one --term is required.")
    excluded_sender_ids = set(args.exclude_sender_id)

    client = TelegramClient(
        str(Path(args.session)),
        api_id,
        api_hash,
        proxy=proxy,
        timeout=args.timeout,
        connection_retries=args.connection_retries,
        retry_delay=1,
    )
    since = (
        datetime.now(timezone.utc) - timedelta(hours=args.lookback_hours)
        if args.lookback_hours is not None
        else None
    )
    hits: list[SearchHit] = []
    errors: list[str] = []
    scanned_chats = 0
    scanned_messages = 0
    total_hits = 0

    await client.start()
    try:
        async for dialog in client.iter_dialogs(limit=args.dialogs_limit):
            if not (dialog.is_group or dialog.is_channel):
                continue
            entity = dialog.entity
            chat_title = display_name(entity)
            chat_username = getattr(entity, "username", None)
            candidates = {
                dialog.name.casefold() if dialog.name else "",
                chat_title.casefold(),
                chat_username.casefold() if chat_username else "",
                f"@{chat_username}".casefold() if chat_username else "",
            }
            if not allowed_chats.intersection(candidates):
                continue

            scanned_chats += 1
            if args.search_backend == "telegram":
                try:
                    found_hits, searched_count, chat_hits = await search_chat_messages(
                        client,
                        entity,
                        chat_title,
                        chat_username,
                        terms,
                        since=since,
                        search_limit_per_term=args.search_limit_per_term,
                        excluded_sender_ids=excluded_sender_ids,
                    )
                except Exception as exc:
                    errors.append(f"{chat_title}: {type(exc).__name__}: {exc}")
                    continue
                scanned_messages += searched_count
                total_hits += chat_hits
                hits.extend(found_hits)
                continue

            try:
                rows = await collect_chat_messages(
                    client,
                    entity,
                    chat_title,
                    chat_username,
                    args.per_chat_limit,
                    since,
                    excluded_sender_ids,
                )
            except Exception as exc:
                errors.append(f"{chat_title}: {type(exc).__name__}: {exc}")
                continue
            scanned_messages += len(rows)
            for row in rows:
                reasons = hit_reason(row.text, terms)
                if not reasons:
                    continue
                total_hits += 1
                hits.append(SearchHit(entity=entity, row=row, matched_terms=reasons))

        selected_hits = select_edge_hits(
            hits,
            newest_contexts=args.max_contexts,
            oldest_contexts=args.oldest_contexts,
        )
        contexts = await materialize_contexts(
            client,
            selected_hits,
            before=args.before,
            after=args.after,
            excluded_sender_ids=excluded_sender_ids,
        )
    finally:
        await client.disconnect()

    contexts.sort(key=lambda item: item["sent_at"], reverse=True)
    output = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "search_backend": args.search_backend,
        "terms": terms,
        "lookback_hours": args.lookback_hours,
        "before": args.before,
        "after": args.after,
        "scanned_chats": scanned_chats,
        "scanned_messages": scanned_messages,
        "errors": errors,
        "hit_count": total_hits,
        "context_count": len(contexts),
        "contexts": contexts,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Scanned chats: {scanned_chats}")
    print(
        f"{'Searched' if args.search_backend == 'telegram' else 'Scanned'} "
        f"messages: {scanned_messages}"
    )
    print(f"Skipped chats with errors: {len(errors)}")
    print(f"Hits: {total_hits}")
    print(f"Contexts saved: {len(contexts)}")
    print(f"Saved: {output_path}")
    for ctx_index, ctx in enumerate(contexts[: args.print_hits], 1):
        print()
        print(
            f"## Hit {ctx_index}: {ctx['chat_title']} msg={ctx['message_id']} "
            f"terms={', '.join(ctx['matched_terms'])}"
        )
        for item in ctx["context"]:
            marker = ">>" if item["offset"] == 0 else "  "
            print(
                f"{marker} {item['offset']:>3} {item['sent_at']} "
                f"{item['sender_name']}: {one_line(item['text'], args.print_text_len)}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search Telegram whitelist chats and print message context windows.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--session", default=str(DEFAULT_SESSION.with_suffix("")))
    parser.add_argument("--allowed-chats", default=str(CONFIG_DIR / "whitelist.txt"))
    parser.add_argument("--term", action="append", required=True)
    parser.add_argument("--lookback-hours", type=int, default=48)
    parser.add_argument("--dialogs-limit", type=int, default=250)
    parser.add_argument(
        "--search-backend",
        choices=["telegram", "recent"],
        default="telegram",
        help=(
            "telegram uses Telegram server-side search and then fetches context; "
            "recent scans the most recent --per-chat-limit messages locally."
        ),
    )
    parser.add_argument(
        "--search-limit-per-term",
        type=int,
        default=300,
        help="Max Telegram server-side hits to inspect per chat per term.",
    )
    parser.add_argument(
        "--max-contexts",
        type=int,
        default=50,
        help="Max matched messages for which context windows are fetched/saved. Use -1 for no cap.",
    )
    parser.add_argument(
        "--oldest-contexts",
        type=int,
        default=0,
        help="Also fetch/save this many globally oldest matched messages. Use -1 for every hit.",
    )
    parser.add_argument("--per-chat-limit", type=int, default=1200)
    parser.add_argument("--before", type=int, default=5)
    parser.add_argument("--after", type=int, default=20)
    parser.add_argument(
        "--exclude-sender-id",
        action="append",
        type=int,
        default=[],
        help="Telegram numeric sender ID to exclude before hit selection and context export. Repeatable.",
    )
    parser.add_argument("--output", default=str(EXPORTS_DATA_DIR / "search" / "telegram_context_search.json"))
    parser.add_argument("--print-hits", type=int, default=8)
    parser.add_argument("--print-text-len", type=int, default=260)
    parser.add_argument("--proxy", default="auto")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--connection-retries", type=int, default=3)
    return parser


def main() -> None:
    setup_stdout()
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
