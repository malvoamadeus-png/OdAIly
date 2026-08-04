from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.tl.types import Channel, Chat

from packages.common.paths import get_paths

from .telegram_helpers import (
    display_name,
    is_automated_sender,
    load_name_list,
    parse_config,
    resolve_proxy,
)
from .tg_watcher import resolve_allowed_entities


PATHS = get_paths()
HEX_MARKER_RE = re.compile(r"\b0x[0-9a-fA-F]{6,}\b")


def _chat_type(entity: Any) -> str:
    if isinstance(entity, Channel) and getattr(entity, "megagroup", False):
        return "supergroup"
    if isinstance(entity, Channel):
        return "channel"
    return "group"


def _compact_text(value: str, limit: int = 240) -> str:
    text = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _default_output_dir() -> Path:
    return PATHS.exports_dir / "meme_non_whitelist_0x"


def _entity_keys(entity: Any) -> set[str]:
    username = str(getattr(entity, "username", "") or "")
    return {
        display_name(entity).casefold(),
        username.casefold(),
        f"@{username}".casefold() if username else "",
    }


async def run(args: argparse.Namespace) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    allowed = load_name_list(Path(args.allowed_chats))
    api_id, api_hash = parse_config(Path(args.config))
    client = TelegramClient(
        str(Path(args.session)),
        api_id,
        api_hash,
        proxy=resolve_proxy(args.proxy),
        timeout=args.timeout,
        connection_retries=args.connection_retries,
        retry_delay=1,
    )
    await client.start()
    try:
        whitelist_entities, _matched = await resolve_allowed_entities(
            client, allowed, args.dialogs_limit
        )
        whitelist_keys = {
            key for entity in whitelist_entities for key in _entity_keys(entity) if key
        }
        groups: dict[int, dict[str, Any]] = {}
        scanned = 0
        async for message in client.iter_messages(None, search=args.search, limit=args.limit):
            scanned += 1
            entity = await message.get_chat()
            if not isinstance(entity, (Channel, Chat)):
                continue
            chat_id = int(getattr(message, "chat_id", 0) or getattr(entity, "id", 0) or 0)
            if _entity_keys(entity).intersection(whitelist_keys):
                continue
            text = str(message.message or "")
            references = sorted(set(HEX_MARKER_RE.findall(text)))
            if not references:
                continue
            sender = await message.get_sender()
            entry = groups.setdefault(
                chat_id,
                {
                    "chat_id": chat_id,
                    "chat": display_name(entity),
                    "username": getattr(entity, "username", None),
                    "type": _chat_type(entity),
                    "matched_messages": 0,
                    "bot_messages": 0,
                    "channel_messages": 0,
                    "human_messages": 0,
                    "terms": set(),
                    "samples": [],
                },
            )
            entry["matched_messages"] += 1
            entry["terms"].update(references)
            automated = is_automated_sender(sender)
            broadcast_channel = isinstance(entity, Channel) and not getattr(entity, "megagroup", False)
            if automated:
                entry["bot_messages"] += 1
            elif broadcast_channel:
                entry["channel_messages"] += 1
            else:
                entry["human_messages"] += 1
            if len(entry["samples"]) < args.samples_per_chat:
                entry["samples"].append(
                    {
                        "date": message.date.astimezone(UTC).isoformat() if message.date else None,
                        "sender": display_name(sender),
                        "automated": automated,
                        "channel": broadcast_channel,
                        "matches": references,
                        "text": _compact_text(text) or "[non-text message]",
                    }
                )

        ordered = sorted(
            groups.values(), key=lambda item: (-item["matched_messages"], item["chat"].casefold())
        )
        for item in ordered:
            item["terms"] = sorted(item["terms"])
        result = {
            "generated_at": datetime.now(UTC).isoformat(),
            "search": args.search,
            "scanned_global_results": scanned,
            "whitelist_entity_count": len(whitelist_entities),
            "groups": ordered,
        }
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        json_path = output_dir / f"report-{stamp}.json"
        markdown_path = output_dir / f"report-{stamp}.md"
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [
            "# Meme速递：白名单外 Telegram 0x 命中群组",
            "",
            f"检索时间：{result['generated_at']}",
            f"检索方式：Telegram 全局搜索 {args.search}，返回上限 {args.limit} 条，实际读取 {scanned} 条；白名单实体 {len(whitelist_ids)} 个。",
            "",
            "| 群组/频道 | 用户名 | 类型 | 命中消息 | 真人 | 机器人 | 频道帖子 | 不同 0x 字样 | 示例 |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
        for item in ordered:
            samples = " / ".join(
                f"{sample['sender']}：{sample['text']}" for sample in item["samples"]
            )
            username = f"@{item['username']}" if item["username"] else ""
            lines.append(
                "| "
                + " | ".join(
                    [
                        _markdown_cell(str(item["chat"])),
                        _markdown_cell(username),
                        item["type"],
                        str(item["matched_messages"]),
                        str(item["human_messages"]),
                        str(item["bot_messages"]),
                        str(item["channel_messages"]),
                        str(len(item["terms"])),
                        _markdown_cell(samples),
                    ]
                )
                + " |"
            )
        if not ordered:
            lines.append("| （没有命中） |  |  | 0 | 0 | 0 | 0 | 0 |  |")
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {"json": str(json_path), "markdown": str(markdown_path), **result},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        await client.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search non-whitelist Telegram groups/channels for 0x-style references."
    )
    parser.add_argument("--config", default=str(PATHS.config_dir / "meme_telegram.txt"))
    parser.add_argument(
        "--session",
        default=str(PATHS.processed_dir / "meme_telegram_discovery"),
    )
    parser.add_argument("--allowed-chats", default=str(PATHS.config_dir / "meme_whitelist.txt"))
    parser.add_argument("--dialogs-limit", type=int, default=300)
    parser.add_argument("--search", default="0x")
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--samples-per-chat", type=int, default=5)
    parser.add_argument("--output-dir", default=str(_default_output_dir()))
    parser.add_argument("--proxy", default="auto")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--connection-retries", type=int, default=3)
    return parser


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))
