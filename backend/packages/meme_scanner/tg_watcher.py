from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telethon import TelegramClient, events

from packages.common.paths import get_paths

from .telegram_helpers import (
    display_name,
    extract_ca_references,
    is_automated_sender,
    load_name_list,
    parse_config,
    resolve_proxy,
)


PATHS = get_paths()
PROCESSED_DATA_DIR = PATHS.processed_dir
DEFAULT_DB = PATHS.processed_dir / "meme_scanner.sqlite3"
DEFAULT_CONFIG = PATHS.config_dir / "meme_telegram.txt"
DEFAULT_ALLOWED_CHATS = PATHS.config_dir / "meme_whitelist.txt"
DEFAULT_BLOCKED_SENDERS = PATHS.config_dir / "meme_blocked_senders.txt"
WINDOW_MINUTES = 20
COOLDOWN_HOURS = 6
RETENTION_DAYS = 90
MIN_MENTIONS = 5
MIN_SENDERS = 3


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def sender_is_blocked(sender: Any, blocked: set[str]) -> bool:
    if not blocked:
        return False
    sender_id = getattr(sender, "id", None)
    username = str(getattr(sender, "username", "") or "")
    candidates = {
        display_name(sender).casefold(),
        username.casefold(),
        f"@{username}".casefold() if username else "",
        str(sender_id) if sender_id is not None else "",
    }
    return bool(candidates.intersection(blocked))


def forward_key(message: Any, address: str) -> str:
    forwarded = getattr(message, "fwd_from", None)
    if forwarded is None:
        return f"message:{getattr(message, 'chat_id', '')}:{message.id}:{address}"
    origin = getattr(forwarded, "from_id", None) or getattr(forwarded, "saved_from_peer", None) or "unknown"
    origin_id = getattr(origin, "channel_id", None) or getattr(origin, "user_id", None) or str(origin)
    origin_message = getattr(forwarded, "channel_post", None) or getattr(forwarded, "saved_from_msg_id", None) or "unknown"
    raw = f"forward:{origin_id}:{origin_message}:{address}".encode("utf-8", errors="replace")
    return "forward:" + hashlib.sha256(raw).hexdigest()


class MentionStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ca_mentions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL,
              chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
              sender_key TEXT NOT NULL, sent_at TEXT NOT NULL,
              text_excerpt TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ca_mentions_window ON ca_mentions(address, sent_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ca_mentions_message
              ON ca_mentions(address, chat_id, message_id);
            CREATE TABLE IF NOT EXISTS tg_candidates (
              id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL,
              detected_at TEXT NOT NULL, window_start TEXT NOT NULL,
              mention_count INTEGER NOT NULL, chat_count INTEGER NOT NULL,
              sender_count INTEGER NOT NULL, evidence_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', reason TEXT,
              market_cap REAL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tg_candidates_status ON tg_candidates(status, id);
            CREATE INDEX IF NOT EXISTS idx_tg_candidates_address_time ON tg_candidates(address, detected_at);
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def record(
        self,
        *,
        address: str,
        chat_id: int,
        message_id: int,
        sender_key: str,
        sent_at: datetime,
        text: str,
        dedupe_key: str,
        window_minutes: int,
        cooldown_hours: int,
    ) -> bool:
        cursor = self.conn.execute(
            """INSERT OR IGNORE INTO ca_mentions(
              address, chat_id, message_id, sender_key, sent_at, text_excerpt, dedupe_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (address, chat_id, message_id, sender_key, sent_at.isoformat(), text[:1000], dedupe_key, now_iso()),
        )
        self.conn.commit()
        if cursor.rowcount == 0:
            return False
        return self._maybe_create_candidate(address, window_minutes, cooldown_hours)

    def _maybe_create_candidate(self, address: str, window_minutes: int, cooldown_hours: int) -> bool:
        detected_at = datetime.now(UTC)
        window_start = detected_at - timedelta(minutes=window_minutes)
        stats = self.conn.execute(
            """SELECT COUNT(*) AS mentions, COUNT(DISTINCT chat_id) AS chats,
              COUNT(DISTINCT sender_key) AS senders
            FROM ca_mentions WHERE address=? AND sent_at>=?""",
            (address, window_start.isoformat()),
        ).fetchone()
        mentions, chats, senders = int(stats["mentions"]), int(stats["chats"]), int(stats["senders"])
        qualifies = mentions >= MIN_MENTIONS and senders >= MIN_SENDERS
        if not qualifies:
            return False
        cooldown_start = detected_at - timedelta(hours=cooldown_hours)
        recent = self.conn.execute(
            "SELECT 1 FROM tg_candidates WHERE address=? AND detected_at>=? LIMIT 1",
            (address, cooldown_start.isoformat()),
        ).fetchone()
        if recent:
            return False
        rows = self.conn.execute(
            """SELECT chat_id, message_id, sender_key, sent_at, text_excerpt
            FROM ca_mentions WHERE address=? AND sent_at>=? ORDER BY sent_at DESC LIMIT 20""",
            (address, window_start.isoformat()),
        ).fetchall()
        evidence = {"messages": [dict(row) for row in reversed(rows)]}
        self.conn.execute(
            """INSERT INTO tg_candidates(
              address, detected_at, window_start, mention_count, chat_count,
              sender_count, evidence_json, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                address,
                detected_at.isoformat(),
                window_start.isoformat(),
                mentions,
                chats,
                senders,
                json.dumps(evidence, ensure_ascii=False),
                detected_at.isoformat(),
            ),
        )
        self.conn.commit()
        return True

    def prune(self, retention_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        cursor = self.conn.execute("DELETE FROM ca_mentions WHERE sent_at<?", (cutoff,))
        self.conn.commit()
        return cursor.rowcount


async def resolve_allowed_entities(client: TelegramClient, allowed: set[str], dialogs_limit: int) -> tuple[list[Any], set[str]]:
    entities: list[Any] = []
    matched_names: set[str] = set()
    async for dialog in client.iter_dialogs(limit=dialogs_limit):
        if not (dialog.is_group or dialog.is_channel):
            continue
        entity = dialog.entity
        username = str(getattr(entity, "username", "") or "")
        candidates = {
            str(dialog.name or "").casefold(),
            display_name(entity).casefold(),
            username.casefold(),
            f"@{username}".casefold() if username else "",
        }
        matched = candidates.intersection(allowed)
        if matched:
            entities.append(entity)
            matched_names.update(matched)
    return entities, matched_names


async def run(args: argparse.Namespace) -> int:
    load_dotenv()
    allowed = load_name_list(Path(args.allowed_chats))
    if not allowed:
        raise RuntimeError(f"No Telegram chats configured in {args.allowed_chats}")
    blocked = load_name_list(Path(args.blocked_senders))
    api_id, api_hash = parse_config(Path(args.config))
    proxy = resolve_proxy(args.proxy)
    store = MentionStore(Path(args.db))
    last_prune = datetime.min.replace(tzinfo=UTC)
    client = TelegramClient(
        str(Path(args.session)),
        api_id,
        api_hash,
        proxy=proxy,
        timeout=args.timeout,
        connection_retries=args.connection_retries,
        retry_delay=1,
    )

    async def handle(message: Any) -> None:
        nonlocal last_prune
        current_time = datetime.now(UTC)
        if (current_time - last_prune).total_seconds() >= 24 * 3600:
            pruned = store.prune(args.retention_days)
            last_prune = current_time
            if pruned:
                print(f"[meme-tg-watch] pruned={pruned}")
        text = str(message.message or "")
        references = [address for chain, address in extract_ca_references(text) if chain == "evm"]
        if not references or not message.date:
            return
        sender = await message.get_sender()
        if is_automated_sender(sender) or sender_is_blocked(sender, blocked):
            return
        chat_id = int(getattr(message, "chat_id", 0) or 0)
        sender_id = getattr(sender, "id", None)
        sender_key = str(sender_id) if sender_id is not None else f"name:{display_name(sender).casefold()}"
        for address in references:
            created = store.record(
                address=address,
                chat_id=chat_id,
                message_id=int(message.id),
                sender_key=sender_key,
                sent_at=message.date.astimezone(UTC),
                text=text,
                dedupe_key=forward_key(message, address),
                window_minutes=args.window_minutes,
                cooldown_hours=args.cooldown_hours,
            )
            if created:
                print(f"[meme-tg-watch] candidate={address} window={args.window_minutes}m")

    await client.start()
    try:
        entities, matched_names = await resolve_allowed_entities(client, allowed, args.dialogs_limit)
        if not entities:
            raise RuntimeError("None of the configured Telegram whitelist chats are visible to this account")
        print(f"[meme-tg-watch] listening chats={len(entities)} configured={len(allowed)}")
        if args.check:
            missing = sorted(allowed - matched_names)
            if missing:
                print(f"[meme-tg-watch] unmatched={json.dumps(missing, ensure_ascii=False)}")
            return 0
        since = datetime.now(UTC) - timedelta(minutes=args.backfill_minutes)
        for entity in entities:
            async for message in client.iter_messages(entity, limit=args.backfill_limit):
                if not message.date or message.date.astimezone(UTC) < since:
                    break
                await handle(message)

        @client.on(events.NewMessage(chats=entities))
        async def new_message_handler(event: events.NewMessage.Event) -> None:
            await handle(event.message)

        print("[meme-tg-watch] backfill_complete")
        await client.run_until_disconnected()
        return 0
    finally:
        await client.disconnect()
        store.close()


def build_parser() -> argparse.ArgumentParser:
    default_session = os.environ.get("MEME_TELEGRAM_WATCH_SESSION") or str(PROCESSED_DATA_DIR / "meme_telegram_watch")
    parser = argparse.ArgumentParser(description="Listen to Telegram whitelist chats and create CA burst candidates.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--session", default=default_session)
    parser.add_argument("--allowed-chats", default=str(DEFAULT_ALLOWED_CHATS))
    parser.add_argument("--blocked-senders", default=str(DEFAULT_BLOCKED_SENDERS))
    parser.add_argument("--window-minutes", type=int, default=WINDOW_MINUTES)
    parser.add_argument("--cooldown-hours", type=int, default=COOLDOWN_HOURS)
    parser.add_argument("--retention-days", type=int, default=RETENTION_DAYS)
    parser.add_argument("--backfill-minutes", type=int, default=WINDOW_MINUTES)
    parser.add_argument("--backfill-limit", type=int, default=200)
    parser.add_argument("--check", action="store_true", help="Connect, resolve whitelist chats, and exit without listening.")
    parser.add_argument("--dialogs-limit", type=int, default=300)
    parser.add_argument("--proxy", default="auto")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--connection-retries", type=int, default=3)
    return parser


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))
