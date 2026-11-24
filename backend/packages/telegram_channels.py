from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events, utils

from packages.common.paths import get_paths
from packages.common.storage import connect_sqlite, load_storage_settings
from packages.editor_plugin_feed_writer import LocalEditorPluginFeedWriter
from packages.local_pipeline.client import LocalPipelineClient
from packages.meme_scanner.telegram_helpers import parse_config, resolve_proxy


CHANNELS = {
    "apewirenewsCN": ("aggregate", "apewirenewsCN", ""),
    "trad_fin_cn": ("aggregate", "trad_fin_cn", ""),
    "vida": ("personal", -1002631914757, "方程式新闻创始人Vida"),
    "durov": ("personal", "durov", "Telegram创始人Durov"),
}


class TelegramChannelStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.postgres = path is None and load_storage_settings().backend == "postgres"

    def connect(self):
        if self.postgres:
            from packages.common.pg_native import connect_postgres
            return connect_postgres(schema="main", application_name="odaily-telegram-channels")
        return connect_sqlite(self.path or load_storage_settings().sqlite_path)

    @property
    def marker(self) -> str:
        return "%s" if self.postgres else "?"

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS telegram_channel_names (channel_key text PRIMARY KEY, write_name text NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS telegram_channel_cursors (channel_key text PRIMARY KEY, message_id bigint NOT NULL)")
            for key, (kind, _target, name) in CHANNELS.items():
                if kind == "personal":
                    conn.execute(f"INSERT INTO telegram_channel_names(channel_key,write_name) VALUES ({self.marker},{self.marker}) ON CONFLICT(channel_key) DO NOTHING", (key, name))
            if not self.postgres:
                conn.commit()

    def cursor(self, key: str) -> int | None:
        with self.connect() as conn:
            row = conn.execute(f"SELECT message_id FROM telegram_channel_cursors WHERE channel_key={self.marker}", (key,)).fetchone()
        return int(row["message_id"]) if row else None

    def save_cursor(self, key: str, message_id: int) -> None:
        with self.connect() as conn:
            conn.execute(f"INSERT INTO telegram_channel_cursors(channel_key,message_id) VALUES({self.marker},{self.marker}) ON CONFLICT(channel_key) DO UPDATE SET message_id=excluded.message_id WHERE telegram_channel_cursors.message_id < excluded.message_id", (key, message_id))
            if not self.postgres:
                conn.commit()

    def save_personal_task(self, key: str, message, url: str) -> tuple[int, str]:
        marker = self.marker
        item_id = f"{key}:{message.id}"
        with self.connect() as conn:
            row = conn.execute(f"SELECT write_name FROM telegram_channel_names WHERE channel_key={marker}", (key,)).fetchone()
            if not row or not str(row["write_name"]).strip():
                raise ValueError(f"missing Telegram writing name: {key}")
            name = str(row["write_name"]).strip()
            metadata = json.dumps({"platform": "telegram", "channel_key": key, "effective_author_name": name, "author_display_name": name}, ensure_ascii=False)
            raw = json.dumps({"message_id": message.id, "channel_key": key, "text": message.raw_text}, ensure_ascii=False)
            params = (item_id, url, f"{name}：{message.raw_text[:80]}", message.raw_text, message.date, raw, metadata)
            if self.postgres:
                row = conn.execute("INSERT INTO tasks(source,source_item_id,source_url,title,content,published_at,raw_payload,metadata,status) VALUES('telegram_channel',%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,'pending') ON CONFLICT(source,source_item_id) DO UPDATE SET source_item_id=excluded.source_item_id RETURNING id", params).fetchone()
            else:
                conn.execute("INSERT INTO tasks(source,source_item_id,source_url,title,content,published_at,raw_payload,metadata,status) VALUES('telegram_channel',?,?,?,?,?,?,?,'pending') ON CONFLICT(source,source_item_id) DO NOTHING", params)
                row = conn.execute("SELECT id FROM tasks WHERE source='telegram_channel' AND source_item_id=?", (item_id,)).fetchone()
                conn.commit()
        return int(row["id"]), item_id


async def deliver_message(key, entity, message, *, store, feed, pipeline) -> None:
    if not message.raw_text:
        return
    configured_target = CHANNELS[key][1]
    username = configured_target if isinstance(configured_target, str) else getattr(entity, "username", None)
    peer_id = utils.get_peer_id(entity)
    url = f"https://t.me/{username}/{message.id}" if username else f"https://t.me/c/{str(peer_id)[4:]}/{message.id}"
    if CHANNELS[key][0] == "aggregate":
        await asyncio.to_thread(feed.upsert_telegram_aggregate, channel=key, message_id=message.id, content=message.raw_text, source_url=url, occurred_at=message.date)
    else:
        task_id, item_id = await asyncio.to_thread(store.save_personal_task, key, message, url)
        await asyncio.to_thread(pipeline.submit_job, job_type="write_flow", task_id=task_id, source="telegram_channel", source_item_id=item_id)


async def run_telegram_channels() -> None:
    load_dotenv()
    paths = get_paths()
    session = os.getenv("TELEGRAM_CHANNEL_SESSION_PATH")
    if not session:
        raise RuntimeError("TELEGRAM_CHANNEL_SESSION_PATH must name a dedicated authorized session")
    api_id, api_hash = parse_config(paths.config_dir / "meme_telegram.txt")
    client = TelegramClient(session, api_id, api_hash, proxy=resolve_proxy(os.getenv("MEME_TELEGRAM_PROXY") or "auto"))
    store = TelegramChannelStore()
    store.init_schema()
    feed = LocalEditorPluginFeedWriter()
    pipeline = LocalPipelineClient()
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram channel session is not authorized")
    try:
        entities = {key: await client.get_entity(target) for key, (_, target, _) in CHANNELS.items()}
        for key, entity in entities.items():
            if store.cursor(key) is None:
                latest = await client.get_messages(entity, limit=1)
                store.save_cursor(key, int(latest[0].id) if latest else 0)
        wake = {key: asyncio.Event() for key in entities}
        peer_keys = {utils.get_peer_id(entity): key for key, entity in entities.items()}

        @client.on(events.NewMessage(chats=list(entities.values())))
        async def on_message(event):
            key = peer_keys.get(event.chat_id)
            if key:
                wake[key].set()

        async def watch_channel(key, entity):
            while client.is_connected():
                wake[key].clear()
                cursor = await asyncio.to_thread(store.cursor, key) or 0
                async for message in client.iter_messages(entity, min_id=cursor, reverse=True):
                    await deliver_message(key, entity, message, store=store, feed=feed, pipeline=pipeline)
                    await asyncio.to_thread(store.save_cursor, key, int(message.id))
                    print(f"[odaily] telegram channels received channel={key} message_id={message.id}", flush=True)
                try:
                    await asyncio.wait_for(wake[key].wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass

        print(f"[odaily] telegram channels listening: {', '.join(entities)}", flush=True)
        await asyncio.gather(*(watch_channel(key, entity) for key, entity in entities.items()))
    finally:
        await client.disconnect()
