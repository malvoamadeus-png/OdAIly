from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from packages.common.storage import connect_sqlite


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


class BusinessNewsRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.initialize()

    def initialize(self) -> None:
        with connect_sqlite(self.database_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS business_telegram_groups (
                    chat_id TEXT PRIMARY KEY,
                    chat_title TEXT NOT NULL DEFAULT '',
                    chat_username TEXT,
                    customer_name TEXT NOT NULL DEFAULT '',
                    is_enabled INTEGER NOT NULL DEFAULT 0 CHECK (is_enabled IN (0, 1)),
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_message_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_business_telegram_groups_enabled
                    ON business_telegram_groups(is_enabled, discovered_at DESC);

                CREATE TABLE IF NOT EXISTS business_message_inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    update_id INTEGER NOT NULL UNIQUE,
                    chat_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    sender_id TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    received_at TEXT NOT NULL,
                    message_date TEXT,
                    is_text INTEGER NOT NULL DEFAULT 0,
                    raw_text TEXT,
                    classification TEXT,
                    classification_reason TEXT,
                    writing_style TEXT,
                    writer4_title TEXT,
                    writer4_content TEXT,
                    writer2_title TEXT,
                    writer2_content TEXT,
                    source_url TEXT,
                    status TEXT NOT NULL,
                    status_reason TEXT,
                    push_result TEXT,
                    plugin_notice_result TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_id, message_id),
                    FOREIGN KEY (chat_id) REFERENCES business_telegram_groups(chat_id)
                );
                CREATE INDEX IF NOT EXISTS idx_business_message_inbox_status
                    ON business_message_inbox(status, received_at, id);
                CREATE INDEX IF NOT EXISTS idx_business_message_inbox_chat_time
                    ON business_message_inbox(chat_id, received_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_business_message_inbox_classification
                    ON business_message_inbox(classification, status, received_at DESC);

                CREATE TABLE IF NOT EXISTS business_telegram_recent_messages (
                    chat_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    sender_id TEXT NOT NULL DEFAULT '',
                    received_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    text_content TEXT,
                    urls_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(chat_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_business_recent_messages_expiry
                    ON business_telegram_recent_messages(expires_at);

                CREATE TABLE IF NOT EXISTS business_telegram_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def last_update_id(self) -> int | None:
        with connect_sqlite(self.database_path) as conn:
            row = conn.execute(
                "SELECT state_value FROM business_telegram_state WHERE state_key='last_update_id'"
            ).fetchone()
        return int(row[0]) if row else None

    def ingest_update(
        self,
        update: dict[str, Any],
        *,
        source_urls: list[str],
        own_bot_id: str | None,
        startup_backfill: bool = False,
    ) -> dict[str, Any] | None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return None
        message = update.get("message")
        member_update = update.get("my_chat_member")
        payload = message if isinstance(message, dict) else member_update if isinstance(member_update, dict) else None
        chat = payload.get("chat") if isinstance(payload, dict) else None
        if not isinstance(chat, dict) or chat.get("type") not in {"group", "supergroup"}:
            self._save_cursor(update_id)
            return None

        chat_id = str(chat.get("id") or "").strip()
        if not chat_id:
            self._save_cursor(update_id)
            return None
        title = str(chat.get("title") or chat.get("username") or "").strip()
        username = str(chat.get("username") or "").strip() or None
        now = _now()
        message_id = message.get("message_id") if isinstance(message, dict) else None
        sender = message.get("from") if isinstance(message, dict) else None
        sender = sender if isinstance(sender, dict) else {}
        sender_chat = message.get("sender_chat") if isinstance(message, dict) else None
        sender_id = str(sender.get("id") or (sender_chat or {}).get("id") or "")
        sender_name = " ".join(
            str(sender.get(key) or "").strip()
            for key in ("first_name", "last_name")
            if str(sender.get(key) or "").strip()
        ) or str(sender.get("username") or (sender_chat or {}).get("title") or "").strip()
        text_content = message.get("text") if isinstance(message, dict) else None
        is_text = isinstance(text_content, str)

        with connect_sqlite(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            group_existed = conn.execute(
                "SELECT 1 FROM business_telegram_groups WHERE chat_id=?", (chat_id,)
            ).fetchone() is not None
            conn.execute(
                """INSERT INTO business_telegram_groups
                   (chat_id, chat_title, chat_username, discovered_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     chat_title=CASE WHEN excluded.chat_title!='' THEN excluded.chat_title ELSE business_telegram_groups.chat_title END,
                     chat_username=COALESCE(excluded.chat_username, business_telegram_groups.chat_username),
                     updated_at=excluded.updated_at""",
                (chat_id, title, username, _iso(now), _iso(now)),
            )
            existing = conn.execute(
                "SELECT is_enabled FROM business_telegram_groups WHERE chat_id=?", (chat_id,)
            ).fetchone()
            enabled = bool(existing and existing[0])
            if isinstance(message, dict):
                conn.execute(
                    "UPDATE business_telegram_groups SET last_message_at=?,updated_at=? WHERE chat_id=?",
                    (_iso(now), _iso(now), chat_id),
                )
            if (
                isinstance(message, dict)
                and isinstance(message_id, int)
                and enabled
                and not startup_backfill
                and str(sender.get("id") or "") != str(own_bot_id or "")
            ):
                raw_text = text_content if is_text else None
                compact_length = sum(
                    1 for char in _without_urls(raw_text or "") if not char.isspace()
                )
                if not is_text:
                    status, reason, stored_text = "skipped_non_text", "非纯文字消息", None
                elif compact_length < 15:
                    status, reason, stored_text = "filtered_short", "去掉空白和 URL 后不足 15 字", None
                else:
                    status, reason, stored_text = "pending", None, raw_text
                message_date = message.get("date")
                try:
                    message_date_text = datetime.fromtimestamp(int(message_date), UTC).isoformat()
                except (TypeError, ValueError, OverflowError):
                    message_date_text = None
                conn.execute(
                    """INSERT OR IGNORE INTO business_message_inbox
                       (update_id,chat_id,message_id,sender_id,sender_name,received_at,message_date,
                        is_text,raw_text,source_url,status,status_reason,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        update_id,
                        chat_id,
                        message_id,
                        sender_id,
                        sender_name,
                        _iso(now),
                        message_date_text,
                        int(is_text),
                        stored_text,
                        source_urls[0] if source_urls else None,
                        status,
                        reason,
                        _iso(now),
                        _iso(now),
                    ),
                )
                expiry = _iso(now + timedelta(minutes=10))
                conn.execute(
                    """INSERT OR REPLACE INTO business_telegram_recent_messages
                       (chat_id,message_id,sender_id,received_at,expires_at,text_content,urls_json)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        chat_id,
                        message_id,
                        sender_id,
                        _iso(now),
                        expiry,
                        raw_text,
                        json.dumps(source_urls if is_text else [], ensure_ascii=False),
                    ),
                )
            conn.execute("DELETE FROM business_telegram_recent_messages WHERE expires_at<?", (_iso(now),))
            conn.execute(
                """INSERT INTO business_telegram_state(state_key,state_value,updated_at)
                   VALUES('last_update_id',?,?)
                   ON CONFLICT(state_key) DO UPDATE SET
                     state_value=MAX(CAST(business_telegram_state.state_value AS INTEGER),CAST(excluded.state_value AS INTEGER)),
                     updated_at=excluded.updated_at""",
                (str(update_id), _iso(now)),
            )
        return {
            "chat_id": chat_id,
            "chat_title": title,
            "update_id": update_id,
            "message_id": message_id,
            "newly_discovered": not group_existed,
        }

    def _save_cursor(self, update_id: int) -> None:
        with connect_sqlite(self.database_path) as conn:
            conn.execute(
                """INSERT INTO business_telegram_state(state_key,state_value,updated_at)
                   VALUES('last_update_id',?,?)
                   ON CONFLICT(state_key) DO UPDATE SET
                     state_value=MAX(CAST(business_telegram_state.state_value AS INTEGER),CAST(excluded.state_value AS INTEGER)),
                     updated_at=excluded.updated_at""",
                (str(update_id), _iso()),
            )

    def claim_pending(self, limit: int = 4) -> list[dict[str, Any]]:
        now = _iso()
        with connect_sqlite(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT m.*,g.customer_name FROM business_message_inbox m
                   JOIN business_telegram_groups g USING(chat_id)
                   WHERE m.status='pending' ORDER BY m.id LIMIT ?""",
                (max(1, min(32, int(limit))),),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                conn.executemany(
                    "UPDATE business_message_inbox SET status='processing',updated_at=? WHERE id=? AND status='pending'",
                    [(now, row_id) for row_id in ids],
                )
        return [dict(row) for row in rows]

    def discard_interrupted(self) -> int:
        with connect_sqlite(self.database_path) as conn:
            cursor = conn.execute(
                """UPDATE business_message_inbox
                   SET status='discarded_failure',status_reason='服务中断，按不重试规则丢弃',raw_text=NULL,updated_at=?
                   WHERE status IN ('processing','formatting','publishing')""",
                (_iso(),),
            )
        return cursor.rowcount

    def adjacent_urls(self, item: dict[str, Any]) -> tuple[list[str], list[str]]:
        with connect_sqlite(self.database_path) as conn:
            previous = conn.execute(
                """SELECT sender_id,urls_json FROM business_telegram_recent_messages
                   WHERE chat_id=? AND message_id<? AND expires_at>=?
                   ORDER BY message_id DESC LIMIT 1""",
                (item["chat_id"], item["message_id"], _iso()),
            ).fetchone()
            following = conn.execute(
                """SELECT sender_id,urls_json FROM business_telegram_recent_messages
                   WHERE chat_id=? AND message_id>? AND expires_at>=?
                   ORDER BY message_id LIMIT 1""",
                (item["chat_id"], item["message_id"], _iso()),
            ).fetchone()
        prev_urls = json.loads(previous["urls_json"] or "[]") if previous and previous["sender_id"] == item["sender_id"] else []
        next_urls = json.loads(following["urls_json"] or "[]") if following and following["sender_id"] == item["sender_id"] else []
        return prev_urls, next_urls

    def update_message(self, inbox_id: int, **fields: Any) -> None:
        allowed = {
            "classification",
            "classification_reason",
            "writing_style",
            "writer4_title",
            "writer4_content",
            "writer2_title",
            "writer2_content",
            "source_url",
            "status",
            "status_reason",
            "push_result",
            "plugin_notice_result",
            "raw_text",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = _iso()
        assignments = ",".join(f"{key}=?" for key in updates)
        with connect_sqlite(self.database_path) as conn:
            conn.execute(
                f"UPDATE business_message_inbox SET {assignments} WHERE id=?",
                (*updates.values(), inbox_id),
            )

    def list_dashboard(self, payload: dict[str, Any]) -> dict[str, Any]:
        page = max(0, int(payload.get("page") or 0))
        page_size = max(1, min(100, int(payload.get("page_size") or 50)))
        filters: list[str] = []
        values: list[Any] = []
        for key, column in (("chat_id", "m.chat_id"), ("classification", "m.classification"), ("status", "m.status")):
            value = str(payload.get(key) or "").strip()
            if value:
                filters.append(f"{column}=?")
                values.append(value)
        date_from = str(payload.get("date_from") or "").strip()
        date_to = str(payload.get("date_to") or "").strip()
        if date_from:
            filters.append("m.received_at>=?")
            values.append(_date_boundary(date_from, end=False))
        if date_to:
            filters.append("m.received_at<?")
            values.append(_date_boundary(date_to, end=True))
        query = str(payload.get("query") or "").strip()[:120]
        if query:
            filters.append("(m.raw_text LIKE ? OR m.writer4_title LIKE ? OR m.writer4_content LIKE ? OR m.classification_reason LIKE ? OR g.chat_title LIKE ?)")
            values.extend([f"%{query}%"] * 5)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with connect_sqlite(self.database_path) as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM business_message_inbox m JOIN business_telegram_groups g USING(chat_id){where}",
                    values,
                ).fetchone()[0]
            )
            items = [
                dict(row)
                for row in conn.execute(
                    f"""SELECT m.*,g.chat_title,g.customer_name
                        FROM business_message_inbox m JOIN business_telegram_groups g USING(chat_id)
                        {where} ORDER BY m.received_at DESC,m.id DESC LIMIT ? OFFSET ?""",
                    (*values, page_size, page * page_size),
                ).fetchall()
            ]
            groups = [
                dict(row)
                for row in conn.execute(
                    """SELECT chat_id,chat_title,chat_username,customer_name,is_enabled,discovered_at,
                              updated_at,last_message_at
                       FROM business_telegram_groups ORDER BY discovered_at DESC,chat_title"""
                ).fetchall()
            ]
        return {"items": items, "groups": groups, "total": total, "page": page, "page_size": page_size}

    def save_group(self, *, chat_id: str, customer_name: str, enabled: bool) -> dict[str, Any]:
        clean_chat_id = str(chat_id or "").strip()
        clean_customer_name = str(customer_name or "").strip()[:80]
        if not clean_chat_id or not clean_customer_name:
            raise ValueError("chat_id 和客户名不能为空")
        with connect_sqlite(self.database_path) as conn:
            conn.execute(
                "UPDATE business_telegram_groups SET customer_name=?,is_enabled=?,updated_at=? WHERE chat_id=?",
                (clean_customer_name, int(bool(enabled)), _iso(), clean_chat_id),
            )
            row = conn.execute(
                """SELECT chat_id,chat_title,chat_username,customer_name,is_enabled,discovered_at,
                          updated_at,last_message_at
                   FROM business_telegram_groups WHERE chat_id=?""",
                (clean_chat_id,),
            ).fetchone()
        if row is None:
            raise ValueError("群尚未由 Telegram 自动发现")
        return dict(row)


def _without_urls(text: str) -> str:
    return re.sub(r"https?://\S+", "", text, flags=re.IGNORECASE)


def _date_boundary(value: str, *, end: bool) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        local_date = date.fromisoformat(value)
        if end:
            local_date += timedelta(days=1)
        boundary = datetime.combine(local_date, time.min, ZoneInfo("Asia/Shanghai"))
        return boundary.astimezone(UTC).isoformat()
    return value
