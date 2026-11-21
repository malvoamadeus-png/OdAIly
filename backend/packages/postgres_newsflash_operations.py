"""Native PostgreSQL persistence for console newsflash operations."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres
from packages.newsflash_operations import (
    DEFAULT_PEOPLE, PUBLISHER_KINDS, CONTRIBUTION_TYPES, QUALITY_OVERRIDE_VALUES, SHIFT_DEFINITIONS,
    SOURCE_LABELS, _as_source_item_id, _is_odaily_operator, _json, _normalize_alias,
    _month_dates, _week_start,
)
from packages.newsflash_metadata import NEWSFLASH_METADATA_SCHEMA_SQL


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS newsflash_roster (
 person_key text PRIMARY KEY, display_name text NOT NULL,
 duty_enabled boolean NOT NULL DEFAULT false, contributor_enabled boolean NOT NULL DEFAULT false,
 active boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS newsflash_roster_aliases (
 alias_normalized text PRIMARY KEY, alias_display text NOT NULL,
 person_key text NOT NULL REFERENCES newsflash_roster(person_key) ON DELETE CASCADE,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS newsflash_operation_facts (
 source_item_id text PRIMARY KEY,
 operator_raw text, publisher_kind text, publisher_person_key text REFERENCES newsflash_roster(person_key) ON DELETE SET NULL,
 publisher_locked boolean NOT NULL DEFAULT false, view_count bigint, is_pushed boolean, pushed_at timestamptz,
 is_contribution boolean NOT NULL DEFAULT false, contributor_person_key text REFERENCES newsflash_roster(person_key) ON DELETE SET NULL,
 contribution_type text NOT NULL DEFAULT 'regular' CHECK (contribution_type IN ('regular','night','ppp','late_different_info')),
 first_source_override text, quality_override text NOT NULL DEFAULT 'none', source_snapshot_at timestamptz,
 attribution_checked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK (publisher_kind IS NULL OR publisher_kind IN ('human','human_unmapped','odaily_ai','other_ai','pending_ai'))
);
CREATE INDEX IF NOT EXISTS idx_newsflash_operation_operator ON newsflash_operation_facts(publisher_person_key,publisher_kind,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_newsflash_operation_contribution ON newsflash_operation_facts(is_contribution,contributor_person_key,updated_at DESC);
CREATE TABLE IF NOT EXISTS newsflash_daily_modes (
 duty_date date PRIMARY KEY, mode text NOT NULL CHECK (mode IN ('two','three')), updated_by text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS newsflash_shift_assignments (
 duty_date date NOT NULL, shift_key text NOT NULL CHECK (shift_key IN ('morning','middle','late')),
 person_key text REFERENCES newsflash_roster(person_key) ON DELETE SET NULL, updated_by text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(duty_date,shift_key)
);
CREATE INDEX IF NOT EXISTS idx_newsflash_shift_person_date ON newsflash_shift_assignments(person_key,duty_date);
CREATE TABLE IF NOT EXISTS newsflash_reporting_weeks (
 week_start date PRIMARY KEY, report_month text NOT NULL, updated_by text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS newsflash_operation_audit (
 id bigserial PRIMARY KEY, source_item_id text, entity_type text NOT NULL, entity_key text NOT NULL,
 action text NOT NULL, before_json jsonb NOT NULL DEFAULT '{}'::jsonb, after_json jsonb NOT NULL DEFAULT '{}'::jsonb,
 actor_email text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_newsflash_operation_audit_entity ON newsflash_operation_audit(entity_type,entity_key,created_at DESC);
CREATE TABLE IF NOT EXISTS newsflash_quality_week_rules (
 week_start date PRIMARY KEY, rules_json jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
""" + (NEWSFLASH_METADATA_SCHEMA_SQL
       .replace("text NOT NULL DEFAULT CURRENT_TIMESTAMP", "timestamptz NOT NULL DEFAULT now()")
       .replace("pushed_at text NOT NULL", "pushed_at timestamptz NOT NULL")
       .replace("created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP", "created_at timestamptz NOT NULL DEFAULT now()")
       .replace("updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP", "updated_at timestamptz NOT NULL DEFAULT now()"))
CHECKSUM = hashlib.sha256(SCHEMA_SQL.encode()).hexdigest()


class PostgresNewsflashOperationsRepository:
    """PostgreSQL implementation for mutable newsflash operations data."""

    def __init__(self, path: Path | None = None, *, seed_defaults: bool = True) -> None:
        self.path = path
        self.application_name = "odaily-newsflash-operations"
        self.init_schema(seed_defaults=seed_defaults)

    def _connect(self):
        return connect_postgres(schema="main", application_name=self.application_name)

    def import_xlsx(
        self,
        path: Path,
        *,
        start_date: date,
        end_date: date,
        preserve_existing: bool = False,
    ) -> dict[str, int]:
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise RuntimeError("openpyxl is required for the one-time XLSX import") from exc
        from packages.newsflash_operations import _parse_datetime

        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = workbook.active
            rows = sheet.iter_rows(values_only=True)
            headers = [str(cell or "").strip() for cell in next(rows)]
            required = {"ID", "标题", "操作人", "链接", "发布时间", "阅读量", "是否推送", "推送时间"}
            if not required.issubset(headers):
                raise ValueError("XLSX headers do not match the confirmed Odaily export")
            index = {header: headers.index(header) for header in required}
            facts: list[dict[str, Any]] = []
            for row in rows:
                published = _parse_datetime(row[index["发布时间"]])
                if published is None or not (start_date <= published.date() < end_date):
                    continue
                pushed_at = _parse_datetime(row[index["推送时间"]])
                facts.append({
                    "source_item_id": _as_source_item_id(row[index["ID"]]),
                    "title": str(row[index["标题"]] or "").strip() or None,
                    "operator_raw": str(row[index["操作人"]] or "").strip() or None,
                    "source_url": str(row[index["链接"]] or "").strip() or None,
                    "published_at": published.isoformat(),
                    "view_count": int(row[index["阅读量"]]) if row[index["阅读量"]] is not None else None,
                    "is_pushed": 1 if str(row[index["是否推送"]] or "").strip() == "是" else 0,
                    "pushed_at": pushed_at.isoformat() if pushed_at else None,
                })
        finally:
            workbook.close()
        return self.upsert_source_facts(
            facts,
            snapshot_at=datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            preserve_existing=preserve_existing,
        )

    def seed_confirmed_week(self, *, actor_email: str = "codex@local") -> None:
        week_start = date(2026, 7, 20)
        schedule = {
            0: ("zoey", "leo", "shark"),
            1: ("harbour", "zoey", "shark"),
            2: ("zoey", "leo", "harbour"),
            3: ("zoey", "harbour", "shark"),
            4: ("zoey", "shark", "harbour"),
            5: ("harbour", "leo"),
            6: ("zoey", "shark"),
        }
        for offset, people in schedule.items():
            duty_date = week_start + timedelta(days=offset)
            mode = "three" if offset < 5 else "two"
            self.save_day_mode({"date": duty_date.isoformat(), "mode": mode}, actor_email=actor_email)
            shift_keys = ("morning", "middle", "late") if mode == "three" else ("morning", "late")
            for shift_key, person_key in zip(shift_keys, people, strict=True):
                self.save_assignment(
                    {"date": duty_date.isoformat(), "shift_key": shift_key, "person_key": person_key},
                    actor_email=actor_email,
                )
        self.save_week_month(
            {"week_start": week_start.isoformat(), "report_month": "2026-07"}, actor_email=actor_email,
        )

    def init_schema(self, *, seed_defaults: bool = True) -> None:
        with self._connect() as conn:
            apply_schema_migrations(conn, schema="main", component="newsflash_operations", migrations=(
                PostgresMigration(1, "newsflash operations native schema", CHECKSUM, lambda c: c.execute(SCHEMA_SQL)),
            ))
            if seed_defaults:
              with conn.transaction():
                for person_key, display_name, duty_enabled, contributor_enabled, aliases in DEFAULT_PEOPLE:
                    conn.execute(
                        "INSERT INTO newsflash_roster(person_key,display_name,duty_enabled,contributor_enabled,active) VALUES(%s,%s,%s,%s,true) ON CONFLICT(person_key) DO NOTHING",
                        (person_key, display_name, bool(duty_enabled), bool(contributor_enabled)),
                    )
                    for alias in aliases:
                        conn.execute(
                            "INSERT INTO newsflash_roster_aliases(alias_normalized,alias_display,person_key) VALUES(%s,%s,%s) ON CONFLICT(alias_normalized) DO NOTHING",
                            (_normalize_alias(alias), alias, person_key),
                        )
                conn.execute("INSERT INTO newsflash_metadata_state(singleton_key,enabled_at) VALUES('global',now()) ON CONFLICT(singleton_key) DO NOTHING")

    def ensure_metadata_schema(self) -> None:
        self.init_schema(seed_defaults=False)
        with self._connect() as conn:
            with conn.transaction():
                conn.execute("INSERT INTO newsflash_metadata_state(singleton_key,enabled_at) VALUES('global',now()) ON CONFLICT(singleton_key) DO NOTHING")

    def initialize_metadata_schema_only(self) -> None:
        """Install the metadata tables without setting their runtime epoch."""
        self.init_schema(seed_defaults=False)

    def record_newsflash_delivery(
        self, *, delivery_key: str | None, origin_key: str, outlet_category: str | None,
        title: str, response_text: str | None, pushed_at: str | None = None,
        priority_status: str = "pending", priority_level: str | None = None,
        priority_reason: str | None = None, duplicate_result: dict[str,Any] | None = None,
    ) -> str:
        import uuid
        from packages.newsflash_metadata import (
            OUTLET_CATEGORY_CODES, PRIORITY_LEVELS, PRIORITY_STATUSES, _response_ids,
            _try_link_metadata,
        )
        if outlet_category is None:
            outlet_category={"manual":"manual","business_news":"business","hyperliquid_monitor":"market","gate_market":"market","stock_market":"market","meme":"meme_digest","hot_topic_auto":"hot_topic_auto","scheduled_brief":"scheduled_brief","x":"ai_regular","binance_square":"ai_regular","competitor":"ai_regular","external_media":"ai_regular"}.get(origin_key)
            if delivery_key and str(delivery_key).startswith("okx-stock-brief:"):outlet_category="market"
            elif delivery_key and str(delivery_key).startswith("yesterday-hot-discussion:"):outlet_category="ai_regular"
        if outlet_category not in OUTLET_CATEGORY_CODES:raise ValueError(f"invalid newsflash outlet category: {outlet_category}")
        key=str(delivery_key or uuid.uuid4().hex);stamp=pushed_at or datetime.now(UTC).isoformat()
        ids=_response_ids(response_text);odaily_id=next(iter(ids)) if len(ids)==1 else None
        id_match_status="matched" if odaily_id is not None else "ambiguous" if ids else "missing"
        if priority_status not in PRIORITY_STATUSES:priority_status="failed"
        if priority_level not in PRIORITY_LEVELS:priority_level=None
        if priority_status!="evaluated":priority_level=priority_reason=None
        duplicate_status="not_checked";duplicate_target_type=duplicate_target_id=duplicate_target_title=duplicate_reason=None
        if isinstance(duplicate_result,dict) and isinstance(duplicate_result.get("is_duplicate"),bool):
            duplicate_status="duplicate" if duplicate_result["is_duplicate"] else "clear"
            target=duplicate_result.get("duplicate_target") if isinstance(duplicate_result.get("duplicate_target"),dict) else {}
            duplicate_target_type=str(duplicate_result.get("duplicate_target_type") or target.get("target_type") or "") or None
            duplicate_target_id=str(duplicate_result.get("duplicate_target_id") or target.get("target_id") or "") or None
            duplicate_target_title=str(target.get("title") or "")[:500] or None
            duplicate_reason=str(duplicate_result.get("reason") or "")[:500] or None
        with self._connect() as conn:
            with conn.transaction():
                conn.execute("""INSERT INTO newsflash_production_metadata(delivery_key,origin_key,outlet_category,odaily_id,id_match_status,title,pushed_at,priority_status,priority_level,priority_reason,duplicate_status,duplicate_target_type,duplicate_target_id,duplicate_target_title,duplicate_reason)
                VALUES(%s,%s,%s,%s,%s,%s,%s::timestamptz,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(delivery_key) DO UPDATE SET origin_key=excluded.origin_key,outlet_category=excluded.outlet_category,
                odaily_id=COALESCE(excluded.odaily_id,newsflash_production_metadata.odaily_id),
                id_match_status=CASE WHEN COALESCE(excluded.odaily_id,newsflash_production_metadata.odaily_id) IS NOT NULL THEN 'matched' WHEN excluded.id_match_status='ambiguous' THEN 'ambiguous' ELSE newsflash_production_metadata.id_match_status END,
                title=excluded.title,pushed_at=excluded.pushed_at,priority_status=excluded.priority_status,priority_level=excluded.priority_level,priority_reason=excluded.priority_reason,
                duplicate_status=excluded.duplicate_status,duplicate_target_type=excluded.duplicate_target_type,duplicate_target_id=excluded.duplicate_target_id,
                duplicate_target_title=excluded.duplicate_target_title,duplicate_reason=excluded.duplicate_reason,updated_at=now()""",
                (key,origin_key,outlet_category,odaily_id,id_match_status,title,stamp,priority_status,priority_level,priority_reason,duplicate_status,duplicate_target_type,duplicate_target_id,duplicate_target_title,duplicate_reason))
                if id_match_status=="missing":
                    candidates=conn.execute("SELECT * FROM newsflash_production_metadata WHERE title=%s AND odaily_id IS NULL AND id_match_status='missing' AND abs(extract(epoch from (pushed_at-%s::timestamptz)))<=300",(title,stamp)).fetchall()
                    references=conn.execute("SELECT source_item_id FROM odaily_reference_items WHERE title=%s AND abs(extract(epoch from (published_at-%s::timestamptz)))<=300",(title,stamp)).fetchall()
                    if len(candidates)==1 and len(references)==1:
                        _try_link_metadata(conn,candidates[0],str(references[0]["source_item_id"]))
        return key

    def update_newsflash_priority(self, *, delivery_key: str, status: str, level: str | None = None, reason: str | None = None) -> bool:
        from packages.newsflash_metadata import PRIORITY_LEVELS,PRIORITY_STATUSES
        if status not in PRIORITY_STATUSES:status="failed"
        if level not in PRIORITY_LEVELS:level=None
        if status!="evaluated":level=reason=None
        with self._connect() as conn:
            with conn.transaction():
                result=conn.execute("UPDATE newsflash_production_metadata SET priority_status=%s,priority_level=%s,priority_reason=%s,updated_at=now() WHERE delivery_key=%s",(status,level,reason,delivery_key))
                return result.rowcount>0

    def execute(self, action: str, payload: dict[str, Any], *, actor_email: str) -> Any:
        handlers = {
            "list": self.list_newsflashes,
            "update": lambda d: self.update_newsflash(d, actor_email=actor_email),
            "roster": lambda _d: self.list_roster(),
            "save_person": lambda d: self.save_person(d, actor_email=actor_email),
            "export": lambda d: self.export_newsflashes(limit=int(d.get("limit",100)),offset=int(d.get("offset",0))),
            "fact_review": lambda d: self._fact_review(d),
            "schedule": self.get_schedule,
            "save_day_mode": lambda d: self.save_day_mode(d, actor_email=actor_email),
            "save_assignment": lambda d: self.save_assignment(d, actor_email=actor_email),
            "save_week_month": lambda d: self.save_week_month(d, actor_email=actor_email),
            "events": self.list_events,
            "summary": self.get_summary,
            "quality": self.get_quality,
            "quality_month": self.list_quality_monthly,
            "contributions": self.list_contributions,
            "contributions_month": self.list_contributions_monthly,
        }
        handler = handlers.get(action)
        if handler is None:
            raise NotImplementedError(f"PostgreSQL newsflash action not yet implemented: {action}")
        return handler(payload)

    def _audit(self, conn: Any, *, source_item_id: str | None, entity_type: str, entity_key: str,
               action: str, before: Any, after: Any, actor_email: str) -> None:
        from psycopg.types.json import Jsonb
        conn.execute(
            "INSERT INTO newsflash_operation_audit(source_item_id,entity_type,entity_key,action,before_json,after_json,actor_email) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (source_item_id, entity_type, entity_key, action, Jsonb(json.loads(_json(before or {}))), Jsonb(json.loads(_json(after or {}))), actor_email),
        )

    def list_roster(self) -> dict[str, Any]:
        with self._connect() as conn:
            people = [dict(r) for r in conn.execute("SELECT * FROM newsflash_roster ORDER BY lower(display_name),person_key").fetchall()]
            aliases = conn.execute("SELECT alias_display,person_key FROM newsflash_roster_aliases ORDER BY lower(alias_display),alias_display").fetchall()
        by_person: dict[str, list[str]] = {}
        for row in aliases:
            by_person.setdefault(row["person_key"], []).append(row["alias_display"])
        for person in people:
            person["aliases"] = by_person.get(person["person_key"], [])
        return {"people": people}

    def save_person(self, payload: dict[str, Any], *, actor_email: str) -> dict[str, Any]:
        person_key = str(payload.get("person_key") or "").strip().casefold()
        display_name = str(payload.get("display_name") or "").strip()
        if not person_key or not display_name:
            raise ValueError("person_key and display_name are required")
        aliases = [str(v).strip() for v in payload.get("aliases") or [] if str(v).strip()]
        with self._connect() as conn:
            with conn.transaction():
                old = conn.execute("SELECT * FROM newsflash_roster WHERE person_key=%s FOR UPDATE", (person_key,)).fetchone()
                before = dict(old) if old else None
                conn.execute(
                    "INSERT INTO newsflash_roster(person_key,display_name,duty_enabled,contributor_enabled,active) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(person_key) DO UPDATE SET display_name=excluded.display_name,duty_enabled=excluded.duty_enabled,contributor_enabled=excluded.contributor_enabled,active=excluded.active,updated_at=now()",
                    (person_key, display_name, bool(payload.get("duty_enabled")), bool(payload.get("contributor_enabled")), payload.get("active", True) is not False),
                )
                conn.execute("DELETE FROM newsflash_roster_aliases WHERE person_key=%s", (person_key,))
                for alias in {display_name, *aliases}:
                    conn.execute(
                        "INSERT INTO newsflash_roster_aliases(alias_normalized,alias_display,person_key) VALUES(%s,%s,%s) ON CONFLICT(alias_normalized) DO UPDATE SET alias_display=excluded.alias_display,person_key=excluded.person_key",
                        (_normalize_alias(alias), alias, person_key),
                    )
                after = dict(conn.execute("SELECT * FROM newsflash_roster WHERE person_key=%s", (person_key,)).fetchone())
                self._audit(conn, source_item_id=None, entity_type="person", entity_key=person_key, action="save", before=before, after={**after,"aliases":aliases}, actor_email=actor_email)
        return self.list_roster()

    def _ensure_month(self, month_key: str) -> list[Any]:
        from packages.newsflash_operations import date
        dates = _month_dates(month_key)
        with self._connect() as conn:
            with conn.transaction():
                for day in dates:
                    mode = "three" if day.weekday() < 5 else "two"
                    conn.execute("INSERT INTO newsflash_daily_modes(duty_date,mode) VALUES(%s,%s) ON CONFLICT(duty_date) DO NOTHING", (day, mode))
        return dates

    def get_schedule(self, payload: dict[str, Any]) -> dict[str, Any]:
        from packages.newsflash_operations import datetime, SHANGHAI_TZ
        from datetime import timedelta
        month = str(payload.get("month") or datetime.now(SHANGHAI_TZ).strftime("%Y-%m"))
        dates = self._ensure_month(month)
        first, last = dates[0], dates[-1]
        first_week, last_week = _week_start(first), _week_start(last)
        with self._connect() as conn:
            modes = {r["duty_date"].isoformat(): r["mode"] for r in conn.execute("SELECT duty_date,mode FROM newsflash_daily_modes WHERE duty_date BETWEEN %s AND %s",(first,last)).fetchall()}
            assignments = [dict(r) for r in conn.execute("SELECT a.duty_date,a.shift_key,a.person_key,a.updated_by,a.created_at,a.updated_at,p.display_name FROM newsflash_shift_assignments a LEFT JOIN newsflash_roster p USING(person_key) WHERE a.duty_date BETWEEN %s AND %s ORDER BY a.duty_date,a.shift_key",(first,last)).fetchall()]
            reporting = {r["week_start"].isoformat():r["report_month"] for r in conn.execute("SELECT week_start,report_month FROM newsflash_reporting_weeks WHERE week_start BETWEEN %s AND %s",(first_week,last_week)).fetchall()}
        weeks=[];cursor=first_week
        while cursor<=last_week:
            week_days=[cursor+timedelta(days=i) for i in range(7)];months={d.strftime("%Y-%m") for d in week_days}
            weeks.append({"week_start":cursor.isoformat(),"week_end":week_days[-1].isoformat(),"report_month":reporting.get(cursor.isoformat()) or (next(iter(months)) if len(months)==1 else None),"report_month_manual":cursor.isoformat() in reporting})
            cursor+=timedelta(days=7)
        return {"month":month,"days":[{"date":d.isoformat(),"mode":modes[d.isoformat()]} for d in dates],"assignments":assignments,"weeks":weeks,**self.list_roster()}

    def save_day_mode(self, payload: dict[str,Any], *, actor_email: str) -> dict[str,Any]:
        from datetime import date
        day=date.fromisoformat(str(payload.get("date")));mode=str(payload.get("mode") or "")
        if mode not in SHIFT_DEFINITIONS:raise ValueError("invalid day mode")
        with self._connect() as conn:
            with conn.transaction():
                before=conn.execute("SELECT * FROM newsflash_daily_modes WHERE duty_date=%s FOR UPDATE",(day,)).fetchone()
                conn.execute("INSERT INTO newsflash_daily_modes(duty_date,mode,updated_by) VALUES(%s,%s,%s) ON CONFLICT(duty_date) DO UPDATE SET mode=excluded.mode,updated_by=excluded.updated_by,updated_at=now()",(day,mode,actor_email))
                if not before or before["mode"]!=mode:conn.execute("DELETE FROM newsflash_shift_assignments WHERE duty_date=%s",(day,))
                after=dict(conn.execute("SELECT * FROM newsflash_daily_modes WHERE duty_date=%s",(day,)).fetchone())
                self._audit(conn,source_item_id=None,entity_type="day_mode",entity_key=day.isoformat(),action="save",before=dict(before) if before else None,after=after,actor_email=actor_email)
        return self.get_schedule({"month":day.strftime("%Y-%m")})

    def save_assignment(self, payload: dict[str,Any], *, actor_email: str) -> dict[str,Any]:
        from datetime import date
        day=date.fromisoformat(str(payload.get("date")));shift=str(payload.get("shift_key") or "");person=str(payload.get("person_key") or "").strip() or None
        with self._connect() as conn:
            with conn.transaction():
                mode_row=conn.execute("SELECT mode FROM newsflash_daily_modes WHERE duty_date=%s",(day,)).fetchone();mode=mode_row["mode"] if mode_row else ("three" if day.weekday()<5 else "two")
                if shift not in {d[0] for d in SHIFT_DEFINITIONS[mode]}:raise ValueError("shift is not available for this day mode")
                if person and not conn.execute("SELECT 1 FROM newsflash_roster WHERE person_key=%s AND active AND duty_enabled",(person,)).fetchone():raise ValueError("duty person is not active")
                before=conn.execute("SELECT * FROM newsflash_shift_assignments WHERE duty_date=%s AND shift_key=%s FOR UPDATE",(day,shift)).fetchone()
                conn.execute("INSERT INTO newsflash_shift_assignments(duty_date,shift_key,person_key,updated_by) VALUES(%s,%s,%s,%s) ON CONFLICT(duty_date,shift_key) DO UPDATE SET person_key=excluded.person_key,updated_by=excluded.updated_by,updated_at=now()",(day,shift,person,actor_email))
                after=dict(conn.execute("SELECT * FROM newsflash_shift_assignments WHERE duty_date=%s AND shift_key=%s",(day,shift)).fetchone())
                self._audit(conn,source_item_id=None,entity_type="assignment",entity_key=f"{day.isoformat()}:{shift}",action="save",before=dict(before) if before else None,after=after,actor_email=actor_email)
        return self.get_schedule({"month":day.strftime("%Y-%m")})

    def save_week_month(self, payload: dict[str,Any], *, actor_email: str) -> dict[str,Any]:
        from datetime import date, timedelta
        week=_week_start(date.fromisoformat(str(payload.get("week_start"))));month=str(payload.get("report_month") or "")
        if month not in {(week+timedelta(days=i)).strftime("%Y-%m") for i in range(7)}:raise ValueError("report month must be covered by the natural week")
        with self._connect() as conn:
            with conn.transaction():
                before=conn.execute("SELECT * FROM newsflash_reporting_weeks WHERE week_start=%s FOR UPDATE",(week,)).fetchone()
                conn.execute("INSERT INTO newsflash_reporting_weeks(week_start,report_month,updated_by) VALUES(%s,%s,%s) ON CONFLICT(week_start) DO UPDATE SET report_month=excluded.report_month,updated_by=excluded.updated_by,updated_at=now()",(week,month,actor_email))
                after=dict(conn.execute("SELECT * FROM newsflash_reporting_weeks WHERE week_start=%s",(week,)).fetchone())
                self._audit(conn,source_item_id=None,entity_type="reporting_week",entity_key=week.isoformat(),action="save",before=dict(before) if before else None,after=after,actor_email=actor_email)
        return {"week_start":week.isoformat(),"report_month":month}

    def _person_for_alias(self, conn: Any, operator_raw: str | None) -> str | None:
        normalized = _normalize_alias(operator_raw)
        row = conn.execute("SELECT person_key FROM newsflash_roster_aliases WHERE alias_normalized=%s", (normalized,)).fetchone() if normalized else None
        return str(row["person_key"]) if row else None

    def _classify_operator(self, conn: Any, operator_raw: str | None) -> tuple[str, str | None]:
        if _is_odaily_operator(operator_raw):
            return "odaily_ai", None
        if str(operator_raw or "").strip():
            person = self._person_for_alias(conn, operator_raw)
            return ("human" if person else "human_unmapped"), person
        return "pending_ai", None

    def upsert_source_facts(self, facts: list[dict[str, Any]], *, snapshot_at: str | None = None,
                            preserve_existing: bool = False) -> dict[str, int]:
        from psycopg.types.json import Jsonb
        matched = skipped = skipped_existing = 0
        ids: list[str] = []
        with self._connect() as conn:
            with conn.transaction():
                for fact in facts:
                    source_id = _as_source_item_id(fact.get("source_item_id"))
                    if not source_id or not conn.execute("SELECT 1 FROM odaily_reference_items WHERE source_item_id=%s", (source_id,)).fetchone():
                        skipped += 1
                        continue
                    old = conn.execute("SELECT publisher_locked FROM newsflash_operation_facts WHERE source_item_id=%s", (source_id,)).fetchone()
                    if preserve_existing and old:
                        skipped_existing += 1
                        continue
                    kind, person = self._classify_operator(conn, fact.get("operator_raw"))
                    if old and old["publisher_locked"]:
                        kind = person = None
                    conn.execute(
                        "INSERT INTO newsflash_operation_facts(source_item_id,operator_raw,publisher_kind,publisher_person_key,view_count,is_pushed,pushed_at,source_snapshot_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source_item_id) DO UPDATE SET operator_raw=excluded.operator_raw,publisher_kind=CASE WHEN newsflash_operation_facts.publisher_locked THEN newsflash_operation_facts.publisher_kind ELSE excluded.publisher_kind END,publisher_person_key=CASE WHEN newsflash_operation_facts.publisher_locked THEN newsflash_operation_facts.publisher_person_key ELSE excluded.publisher_person_key END,view_count=excluded.view_count,is_pushed=excluded.is_pushed,pushed_at=excluded.pushed_at,source_snapshot_at=excluded.source_snapshot_at,updated_at=now()",
                        (source_id, str(fact.get("operator_raw") or "").strip() or None, kind, person, fact.get("view_count"), None if fact.get("is_pushed") is None else bool(fact.get("is_pushed")), fact.get("pushed_at"), snapshot_at or datetime.now(UTC)),
                    )
                    if not preserve_existing:
                        conn.execute("UPDATE odaily_reference_items SET source_url=COALESCE(%s,source_url),title=COALESCE(%s,title),published_at=COALESCE(%s::timestamptz,published_at),updated_at=now() WHERE source_item_id=%s", (fact.get("source_url"), fact.get("title"), fact.get("published_at"), source_id))
                    matched += 1
                    ids.append(source_id)
        reconciled = self.reconcile_ai_publishers(source_item_ids=ids)
        return {"read": len(facts), "matched": matched, "skipped": skipped, "skipped_existing": skipped_existing, "reconciled_odaily": reconciled}

    def reconcile_ai_publishers(self, *, source_item_ids: Iterable[str] | None = None) -> int:
        from packages.newsflash_operations import _parse_datetime, SHANGHAI_TZ
        now = datetime.now(SHANGHAI_TZ)
        ids = [_as_source_item_id(v) for v in source_item_ids or () if _as_source_item_id(v)]
        if source_item_ids is not None and not ids:
            return 0
        count = 0
        with self._connect() as conn:
            with conn.transaction():
                sql = "SELECT f.source_item_id,r.title,r.published_at FROM newsflash_operation_facts f JOIN odaily_reference_items r USING(source_item_id) WHERE NOT f.publisher_locked AND (f.operator_raw IS NULL OR btrim(f.operator_raw)='') AND f.publisher_kind IN ('pending_ai','other_ai')"
                params: tuple[Any,...] = ()
                if ids:
                    sql += " AND f.source_item_id=ANY(%s)"; params = (ids,)
                for row in conn.execute(sql, params).fetchall():
                    published = _parse_datetime(row["published_at"])
                    if not published: continue
                    matches = conn.execute("SELECT p.final_title,p.publish_completed_at,p.push_result FROM x_task_pipeline p WHERE p.publish_completed_at IS NOT NULL AND p.final_title=%s", (row["title"],)).fetchall()
                    match = any(row["source_item_id"] in self._remote_ids(c["push_result"]) or (lambda t: t is not None and abs((t-published).total_seconds())<=300)(_parse_datetime(c["publish_completed_at"])) for c in matches)
                    kind = "odaily_ai" if match else "other_ai" if now-published>=__import__("datetime").timedelta(minutes=10) else "pending_ai"
                    conn.execute("UPDATE newsflash_operation_facts SET publisher_kind=%s,attribution_checked_at=now(),updated_at=now() WHERE source_item_id=%s AND NOT publisher_locked", (kind,row["source_item_id"]))
                    count += int(match)
        return count

    @staticmethod
    def _remote_ids(raw: Any) -> set[str]:
        try: payload = raw if isinstance(raw, (dict,list)) else json.loads(raw or "{}")
        except (TypeError,ValueError): return set()
        found: set[str] = set()
        def walk(x: Any, key: str = "") -> None:
            if isinstance(x,dict):
                for k,v in x.items(): walk(v,str(k))
            elif isinstance(x,list):
                for v in x: walk(v,key)
            elif key.casefold() in {"id","newsflashid","newsflash_id","source_item_id"} and x is not None: found.add(_as_source_item_id(x))
        walk(payload); return found

    def update_newsflash(self, payload: dict[str, Any], *, actor_email: str) -> dict[str, Any]:
        source_id = _as_source_item_id(payload.get("source_item_id")); patch = payload.get("patch") or {}
        allowed = {"is_contribution","contributor_person_key","contribution_type","publisher_kind","publisher_person_key","operator_raw","first_source_override","quality_override"}
        if not source_id or not isinstance(patch,dict): raise ValueError("source_item_id and patch are required")
        if set(patch)-allowed: raise ValueError("unsupported newsflash field")
        data=dict(patch)
        if "contribution_type" in data and data["contribution_type"] not in CONTRIBUTION_TYPES: raise ValueError("invalid contribution type")
        if "quality_override" in data:
            data["quality_override"]=str(data["quality_override"] or "none").strip().casefold()
            if data["quality_override"] not in QUALITY_OVERRIDE_VALUES: raise ValueError("invalid quality override")
        if "first_source_override" in data:
            data["first_source_override"]=str(data["first_source_override"] or "").strip().casefold() or None
            if data["first_source_override"] and data["first_source_override"] not in SOURCE_LABELS: raise ValueError("invalid first source override")
        if "is_contribution" in data:
            data["is_contribution"]=bool(data["is_contribution"])
            if not data["is_contribution"]: data.update(contributor_person_key=None,contribution_type="regular")
        if "publisher_kind" in data:
            if data["publisher_kind"] not in PUBLISHER_KINDS-{"pending_ai"}: raise ValueError("invalid publisher kind")
            data["publisher_locked"]=True
            if data["publisher_kind"]!="human": data["publisher_person_key"]=None
            elif not data.get("publisher_person_key"): raise ValueError("human publisher requires a person")
        with self._connect() as conn:
            with conn.transaction():
                if not conn.execute("SELECT 1 FROM odaily_reference_items WHERE source_item_id=%s",(source_id,)).fetchone(): raise ValueError("newsflash not found")
                conn.execute("INSERT INTO newsflash_operation_facts(source_item_id) VALUES(%s) ON CONFLICT DO NOTHING",(source_id,))
                before=dict(conn.execute("SELECT * FROM newsflash_operation_facts WHERE source_item_id=%s FOR UPDATE",(source_id,)).fetchone())
                if data:
                    columns=list(data)
                    conn.execute("UPDATE newsflash_operation_facts SET "+",".join(f"{c}=%s" for c in columns)+",updated_at=now() WHERE source_item_id=%s",[*data.values(),source_id])
                after=dict(conn.execute("SELECT * FROM newsflash_operation_facts WHERE source_item_id=%s",(source_id,)).fetchone())
                self._audit(conn,source_item_id=source_id,entity_type="newsflash",entity_key=source_id,action="update",before=before,after=after,actor_email=actor_email)
        return self.list_newsflashes({"search":source_id})["items"][0]

    def _fact_review(self, payload: dict[str,Any]) -> dict[str,Any]:
        from packages.fact_auditor.postgres_repository import PostgresFactReviewRepository
        return PostgresFactReviewRepository(self.path).detail(_as_source_item_id(payload.get("source_item_id")),int(payload["check_id"]) if payload.get("check_id") is not None else None)

    def list_newsflashes(self, payload: dict[str,Any]) -> dict[str,Any]:
        """List current reference rows with native joins and stable pagination."""
        page=max(1,int(payload.get("page") or 1)); size=50; clauses=["true"]; params:list[Any]=[]
        search=str(payload.get("search") or "").strip()
        if search:
            if search.isdigit(): clauses.append("r.source_item_id=%s");params.append(search)
            else: clauses.append("(lower(r.title) LIKE %s OR r.source_item_id=%s)");params.extend([f"%{search.casefold()}%",search])
        for key,op in (("date_from",">="),("date_to","<")):
            value=str(payload.get(key) or "").strip()
            if value:
                from datetime import date as dt_date, timedelta
                clauses.append(f"r.published_at {op} %s::date");params.append(value if key=="date_from" else (dt_date.fromisoformat(value)+timedelta(days=1)).isoformat())
        for key,col in (("publisher_kind","f.publisher_kind"),("publisher_person_key","f.publisher_person_key"),("contributor_person_key","f.contributor_person_key"),("contribution_type","f.contribution_type")):
            value=str(payload.get(key) or "").strip()
            if value:clauses.append(f"{col}=%s");params.append(value)
        for key,col in (("is_pushed","f.is_pushed"),("is_contribution","f.is_contribution")):
            if payload.get(key) in (True,False,1,0,"1","0"):clauses.append(f"{col}=%s");params.append(str(payload[key]).casefold() in {"true","1"})
        fact_status=str(payload.get("fact_status") or "").strip()
        from packages.fact_auditor.repository import LABELS as FACT_LABELS
        if fact_status:
            if fact_status not in FACT_LABELS:raise ValueError("invalid fact review status")
            clauses.append("COALESCE(fc.status,'unreviewed')=%s");params.append(fact_status)
        first_status=str(payload.get("first_status") or "").strip()
        if first_status:
            if first_status not in {"ready","insufficient","unmatched","excluded"}:raise ValueError("invalid first publication status")
            if first_status=="excluded":clauses.append("(EXISTS(SELECT 1 FROM newsflash_event_exclusions x WHERE x.source='odaily' AND x.source_item_id=r.source_item_id) AND f.first_source_override IS NULL)")
            elif first_status=="unmatched":clauses.append("(f.first_source_override IS NULL AND NOT EXISTS(SELECT 1 FROM newsflash_event_sources es JOIN newsflash_items ni ON ni.id=es.item_id JOIN newsflash_events ne ON ne.event_id=es.event_id WHERE ni.source='odaily' AND ni.source_item_id=r.source_item_id AND ne.status='active') AND NOT EXISTS(SELECT 1 FROM newsflash_event_exclusions x WHERE x.source='odaily' AND x.source_item_id=r.source_item_id))")
            else:
                cond="ne.first_published_at IS NOT NULL" if first_status=="ready" else "ne.first_published_at IS NULL"
                override="f.first_source_override IS NOT NULL OR " if first_status=="ready" else "f.first_source_override IS NULL AND "
                clauses.append(f"({override}EXISTS(SELECT 1 FROM newsflash_event_sources es JOIN newsflash_items ni ON ni.id=es.item_id JOIN newsflash_events ne ON ne.event_id=es.event_id WHERE ni.source='odaily' AND ni.source_item_id=r.source_item_id AND ne.status='active' AND {cond}))")
        first_source=str(payload.get("first_source") or "").strip()
        if first_source:clauses.append("(f.first_source_override=%s OR EXISTS(SELECT 1 FROM newsflash_event_sources es JOIN newsflash_items ni ON ni.id=es.item_id JOIN newsflash_events ne ON ne.event_id=es.event_id WHERE ni.source='odaily' AND ni.source_item_id=r.source_item_id AND ne.status='active' AND (%s=ANY(ne.first_sources) OR ne.first_source=%s)))");params.extend([first_source,first_source,first_source])
        where=" AND ".join(clauses)
        from packages.fact_auditor.postgres_repository import _locator
        check_id=f"(SELECT c.id FROM fact_checks c WHERE c.source_item_id=r.source_item_id AND c.title IS NOT DISTINCT FROM r.title AND c.content=r.content AND c.source_locator={_locator()} ORDER BY c.id DESC LIMIT 1)"
        with self._connect() as conn:
            has_text_reviews = conn.execute("SELECT to_regclass('main.auditor_checks') IS NOT NULL AS present").fetchone()["present"]
            text_select = ("ac.status AS text_status,ac.audit_result AS audit_result,ac.updated_at AS text_updated_at"
                           if has_text_reviews else "NULL::text AS text_status,NULL::jsonb AS audit_result,NULL::timestamptz AS text_updated_at")
            text_join = ("LEFT JOIN LATERAL (SELECT status,audit_result,updated_at FROM auditor_checks WHERE source_item_id=r.source_item_id AND content_hash=md5(COALESCE(r.title,'')||E'\\n'||r.content) ORDER BY id DESC LIMIT 1) ac ON true"
                         if has_text_reviews else "")
            total=int(conn.execute(f"SELECT count(*) AS n FROM odaily_reference_items r LEFT JOIN newsflash_operation_facts f USING(source_item_id) WHERE {where}",params).fetchone()["n"])
            rows=conn.execute(f"""SELECT r.source_item_id,r.source_url,r.title,r.content,r.published_at,r.created_at,r.updated_at AS reference_updated_at,
                f.operator_raw,f.publisher_kind,f.publisher_person_key,f.publisher_locked,f.view_count,f.is_pushed,f.is_contribution,
                f.contributor_person_key,f.contribution_type,f.first_source_override,f.quality_override,f.source_snapshot_at,
                op.display_name AS publisher_person_name,cp.display_name AS contributor_name,fc.id AS fact_check_id,
                fc.status AS fact_status,fc.reason AS fact_reason,fc.summary AS fact_summary,fc.updated_at AS fact_updated_at,
                {text_select}
                FROM odaily_reference_items r LEFT JOIN newsflash_operation_facts f USING(source_item_id)
                LEFT JOIN newsflash_roster op ON op.person_key=f.publisher_person_key LEFT JOIN newsflash_roster cp ON cp.person_key=f.contributor_person_key
                LEFT JOIN fact_checks fc ON fc.id={check_id} {text_join} WHERE {where}
                ORDER BY r.published_at DESC NULLS LAST,r.source_item_id DESC LIMIT %s OFFSET %s""",[*params,size,(page-1)*size]).fetchall()
            data=[]
            for raw in rows:
                item=dict(raw);item["publisher_locked"]=bool(item["publisher_locked"]);item["is_contribution"]=bool(item["is_contribution"])
                item["is_pushed"]=None if item["is_pushed"] is None else bool(item["is_pushed"])
                item["publisher_kind"],item["publisher_person_key"]=self._effective_publisher(item)
                item["first_publication"]=self._event_info(conn,item["source_item_id"])
                from packages.newsflash_operations import TEXT_REVIEW_LABELS
                audit=item.pop("audit_result",{}) or {};audit=audit if isinstance(audit,dict) else {}
                text_status=item.pop("text_status",None) or "unreviewed"
                if text_status not in TEXT_REVIEW_LABELS:text_status="failed"
                item["text_review"]={"status":text_status,"label":TEXT_REVIEW_LABELS[text_status],"summary":str(audit.get("summary") or ""),"updated_at":item.pop("text_updated_at",None)}
                item["read_status"]="not_synced"
                item["fact_review"]={"id":item.pop("fact_check_id"),"status":item.pop("fact_status") or "unreviewed","reason":item.pop("fact_reason") or "","summary":item.pop("fact_summary") or "","updated_at":item.pop("fact_updated_at")}
                item["id"]=item["source_item_id"]
                item.update(self._metadata_for_reference(conn,item))
                item.pop("created_at",None)
                data.append(item)
            counts={r["status"]:r["count"] for r in conn.execute(f"SELECT COALESCE(fc.status,'unreviewed') status,count(*) count FROM odaily_reference_items r LEFT JOIN newsflash_operation_facts f USING(source_item_id) LEFT JOIN fact_checks fc ON fc.id={check_id} WHERE {where} GROUP BY 1",params).fetchall()}
        return {"items":data,"page":page,"page_size":size,"total":total,"pages":max(1,(total+size-1)//size),"fact_review_counts":counts}

    def _metadata_for_reference(self,conn:Any,item:dict[str,Any])->dict[str,Any]:
        from packages.newsflash_metadata import metadata_for_reference
        return metadata_for_reference(conn,source_item_id=str(item["id"]),title=item.get("title"),published_at=item.get("published_at"),publisher_kind=item.get("publisher_kind"),operation_created_at=item.get("created_at"))

    def _effective_publisher(self, item: dict[str,Any]) -> tuple[str|None,str|None]:
        from packages.newsflash_operations import _parse_datetime,SHANGHAI_TZ
        if item.get("publisher_locked"):return item.get("publisher_kind"),item.get("publisher_person_key")
        if _is_odaily_operator(item.get("operator_raw")):return "odaily_ai",None
        if item.get("publisher_kind"):return item.get("publisher_kind"),item.get("publisher_person_key")
        if str(item.get("operator_raw") or "").strip():return "human_unmapped",None
        published=_parse_datetime(item.get("published_at"))
        return ("other_ai",None) if published and datetime.now(SHANGHAI_TZ)-published>=__import__("datetime").timedelta(minutes=10) else ("pending_ai",None)

    def export_newsflashes(self, *, limit: int=100, offset: int=0) -> dict[str,Any]:
        from packages.newsflash_metadata import OUTLET_CATEGORY_LABELS
        from packages.newsflash_operations import _decode
        from packages.fact_auditor.alerts import plugin_alert_summary
        from urllib.parse import urlencode
        size=max(1,min(int(limit or 100),500));off=max(0,int(offset or 0))
        where="(m.outlet_category IS NOT NULL OR (m.delivery_key IS NULL AND f.publisher_kind='human'))"
        with self._connect() as conn:
            has_text_reviews = conn.execute("SELECT to_regclass('main.auditor_checks') IS NOT NULL AS present").fetchone()["present"]
            text_join = ("LEFT JOIN LATERAL (SELECT status,audit_result,telegram_text,updated_at FROM auditor_checks WHERE source_item_id=r.source_item_id AND content_hash=md5(COALESCE(r.title,'')||E'\\n'||r.content) ORDER BY id DESC LIMIT 1) ac ON true"
                         if has_text_reviews else "")
            text_select = ("ac.status text_status,ac.audit_result,ac.telegram_text,ac.updated_at text_updated_at"
                           if has_text_reviews else "NULL::text text_status,NULL::jsonb audit_result,NULL::text telegram_text,NULL::timestamptz text_updated_at")
            total=int(conn.execute(f"SELECT count(*) n FROM odaily_reference_items r LEFT JOIN newsflash_production_metadata m ON m.odaily_id=r.source_item_id LEFT JOIN newsflash_operation_facts f USING(source_item_id) WHERE {where}").fetchone()["n"])
            rows=conn.execute(f"SELECT r.source_item_id,r.title,r.content,r.published_at,r.updated_at reference_updated_at,m.delivery_key,m.outlet_category,m.priority_status,m.priority_level,m.updated_at metadata_updated_at,f.publisher_kind,fc.status fact_status,fc.result_json fact_result_json,fc.updated_at fact_updated_at,{text_select} FROM odaily_reference_items r LEFT JOIN newsflash_production_metadata m ON m.odaily_id=r.source_item_id LEFT JOIN newsflash_operation_facts f USING(source_item_id) LEFT JOIN LATERAL (SELECT * FROM fact_checks c WHERE c.source_item_id=r.source_item_id AND c.title IS NOT DISTINCT FROM r.title AND c.content=r.content ORDER BY c.id DESC LIMIT 1) fc ON true {text_join} WHERE {where} ORDER BY r.published_at DESC NULLS LAST,r.source_item_id DESC LIMIT %s OFFSET %s",(size,off)).fetchall()
        items=[]
        for row in rows:
            category=str(row["outlet_category"] or ("manual" if row["publisher_kind"]=="human" else ""))
            if category not in OUTLET_CATEGORY_LABELS:continue
            audit=_decode(row["audit_result"],{}) if row["audit_result"] is not None else {}
            audit=audit if isinstance(audit,dict) else {}
            text_status=str(row["text_status"] or "unreviewed")
            text_review={"status":text_status,"feedback":audit if row["text_status"] else None,"text":str(row["telegram_text"] or audit.get("summary") or ""),"updated_at":row["text_updated_at"]}
            fact_status=row["fact_status"] or "unreviewed"
            fact_result=row["fact_result_json"] if isinstance(row["fact_result_json"],dict) else _decode(row["fact_result_json"],{})
            fact_text=plugin_alert_summary(fact_result) if fact_status=="flagged" else ""
            items.append({"id":row["source_item_id"],"title":row["title"],"content":row["content"],"published_at":row["published_at"],"outlet_category":OUTLET_CATEGORY_LABELS[category],"priority_level":row["priority_level"] if row["priority_status"]=="evaluated" else None,"text_review":text_review,"fact_review":{"status":fact_status,"text":fact_text,"url":"https://odaily.info/content/newsflash/newsflash-list?"+urlencode({"view":"newsflash","fact_review":row["source_item_id"]}),"updated_at":row["fact_updated_at"]},"duplicate":{"status":"not_implemented","text":None,"url":None},"read_status":{"is_read":False,"readers":[]},"updated_at":max([x.isoformat() if isinstance(x,datetime) else str(x) for x in (row["reference_updated_at"],row["metadata_updated_at"],text_review["updated_at"],row["fact_updated_at"]) if x] or [""]) or None,"_delivery_key":row["delivery_key"]})
        return {"items":items,"total":total,"limit":size,"offset":off,"next_offset":off+size if off+size<total else None}

    def list_events(self, payload: dict[str,Any]) -> dict[str,Any]:
        from datetime import date, timedelta
        from packages.newsflash_operations import SOURCE_MEDIA_DOMAINS, SOURCE_MEDIA_URL_KEYS, SOURCE_LABELS
        page=max(1,int(payload.get("page") or 1));size=50;clauses=["e.status='active'"];params:list[Any]=[]
        search=str(payload.get("search") or "").strip()
        if search:
            pat=f"%{search.casefold()}%"
            clauses.append("(lower(COALESCE(e.representative_title,'')) LIKE %s OR EXISTS(SELECT 1 FROM newsflash_event_sources ss JOIN newsflash_items si ON si.id=ss.item_id WHERE ss.event_id=e.event_id AND (lower(COALESCE(si.title,'')) LIKE %s OR lower(si.source_item_id) LIKE %s)))")
            params.extend([pat,pat,pat])
        if payload.get("has_odaily") in (True,False,1,0,"1","0"):
            clauses.append("e.has_odaily=%s");params.append(str(payload["has_odaily"]).casefold() in {"true","1"})
        first=str(payload.get("first_source") or "").strip()
        if first:clauses.append("(%s=ANY(e.first_sources) OR e.first_source=%s)");params.extend([first,first])
        for key,op in (("date_from",">="),("date_to","<")):
            value=str(payload.get(key) or "").strip()
            if value:clauses.append(f"e.event_time {op} %s::date");params.append(value if key=="date_from" else (date.fromisoformat(value)+timedelta(days=1)).isoformat())
        where=" AND ".join(clauses)
        with self._connect() as conn:
            total=int(conn.execute(f"SELECT count(*) n FROM newsflash_events e WHERE {where}",params).fetchone()["n"])
            events=conn.execute(f"SELECT e.* FROM newsflash_events e WHERE {where} ORDER BY e.event_time DESC NULLS LAST,e.updated_at DESC LIMIT %s OFFSET %s",[*params,size,(page-1)*size]).fetchall()
            items=[]
            for event in events:
                src=conn.execute("SELECT i.source,i.source_item_id,i.source_url,i.title,i.published_at,i.raw_payload FROM newsflash_event_sources s JOIN newsflash_items i ON i.id=s.item_id WHERE s.event_id=%s ORDER BY i.published_at NULLS LAST,i.id",(event["event_id"],)).fetchall()
                sources=[]
                for raw in src:
                    source=dict(raw);payload_data=source.get("raw_payload") or {}
                    if isinstance(payload_data,str):
                        try:payload_data=json.loads(payload_data)
                        except ValueError:payload_data={}
                    media=None
                    candidates=[str(payload_data.get(k) or "").strip() for k in SOURCE_MEDIA_URL_KEYS] if isinstance(payload_data,dict) else []
                    candidates.append(str(source.get("source_url") or ""))
                    domains=SOURCE_MEDIA_DOMAINS.get(str(source["source"]).casefold(),())
                    for url in candidates:
                        try:
                            from urllib.parse import urlparse
                            parsed=urlparse(url);host=(parsed.hostname or "").casefold().removeprefix("www.")
                            if parsed.scheme in {"http","https"} and any(host==d or host.endswith("."+d) for d in domains):media=url;break
                        except ValueError:pass
                    source["media_url"]=media;source.pop("raw_payload",None);sources.append(source)
                first_sources=list(event["first_sources"] or []) or ([event["first_source"]] if event["first_source"] else [])
                labels=[SOURCE_LABELS.get(x,x) for x in first_sources]
                odaily=next((x for x in sources if x["source"]=="odaily"),None);delay=None
                if odaily and odaily["published_at"] and event["first_published_at"]:
                    from packages.newsflash_operations import _parse_datetime
                    delay=round((_parse_datetime(odaily["published_at"])-_parse_datetime(event["first_published_at"])).total_seconds()/60,1)
                items.append({**dict(event),"first_sources":labels,"first_source_label":"、".join(labels) if labels else "数据不足","odaily_published_at":odaily["published_at"] if odaily else None,"odaily_delay_minutes":delay,"sources":sources})
        return {"items":items,"page":page,"pages":max(1,(total+size-1)//size),"total":total,"page_size":size}

    def _period_dates(self, conn: Any, payload: dict[str,Any]) -> tuple[list[Any],str,list[str]]:
        from datetime import date, timedelta
        from packages.newsflash_operations import _week_start, _month_dates, SHANGHAI_TZ, datetime
        month=str(payload.get("report_month") or "").strip()
        if month:
            stored={r["week_start"].isoformat() for r in conn.execute("SELECT week_start FROM newsflash_reporting_weeks WHERE report_month=%s",(month,)).fetchall()}
            days=_month_dates(month);cursor=_week_start(days[0]);last=_week_start(days[-1])
            while cursor<=last:
                span=[cursor+timedelta(days=i) for i in range(7)]
                if all(d.strftime("%Y-%m")==month for d in span):stored.add(cursor.isoformat())
                cursor+=timedelta(days=7)
            weeks=sorted(stored);return [date.fromisoformat(w)+timedelta(days=i) for w in weeks for i in range(7)],month,weeks
        start=_week_start(date.fromisoformat(str(payload.get("week_start") or _week_start(datetime.now(SHANGHAI_TZ).date()))))
        return [start+timedelta(days=i) for i in range(7)],start.isoformat(),[start.isoformat()]

    def _shift_windows(self, conn: Any, dates: list[Any]) -> list[Any]:
        from datetime import datetime, timedelta
        from packages.newsflash_operations import SHIFT_DEFINITIONS, ShiftWindow, SHANGHAI_TZ
        if not dates:return []
        modes={r["duty_date"].isoformat():r["mode"] for r in conn.execute("SELECT duty_date,mode FROM newsflash_daily_modes WHERE duty_date BETWEEN %s AND %s",(min(dates),max(dates))).fetchall()}
        assignments=conn.execute("SELECT a.duty_date,a.shift_key,a.person_key,p.display_name FROM newsflash_shift_assignments a JOIN newsflash_roster p USING(person_key) WHERE a.duty_date BETWEEN %s AND %s AND a.person_key IS NOT NULL",(min(dates),max(dates))).fetchall()
        out=[]
        for a in assignments:
            day=a["duty_date"];key=day.isoformat();mode=modes.get(key,"three" if day.weekday()<5 else "two")
            definition=next((x for x in SHIFT_DEFINITIONS[mode] if x[0]==a["shift_key"]),None)
            if not definition:continue
            shift,label,start,end,end_offset=definition
            out.append(ShiftWindow(day,shift,label,a["person_key"],a["display_name"],datetime.combine(day,start,SHANGHAI_TZ),datetime.combine(day+timedelta(days=end_offset),end,SHANGHAI_TZ)))
        return out

    def get_summary(self, payload: dict[str,Any]) -> dict[str,Any]:
        from datetime import datetime, time, timedelta
        from packages.newsflash_operations import SHANGHAI_TZ, _parse_datetime
        with self._connect() as conn:
            dates,period,weeks=self._period_dates(conn,payload)
            if not dates:return {"period":period,"weeks":weeks,"rows":[],"people":[],"unassigned_count":0}
            windows=self._shift_windows(conn,dates)
            start=datetime.combine(min(dates),time(),SHANGHAI_TZ)-timedelta(minutes=30);end=datetime.combine(max(dates)+timedelta(days=2),time(),SHANGHAI_TZ)
            items=conn.execute("SELECT r.source_item_id,r.published_at,f.publisher_kind,f.publisher_person_key,f.operator_raw,f.publisher_locked,f.view_count,f.is_pushed,f.is_contribution FROM odaily_reference_items r LEFT JOIN newsflash_operation_facts f USING(source_item_id) WHERE r.published_at>=%s AND r.published_at<%s",(start,end)).fetchall()
            roster={r["person_key"]:r["display_name"] for r in conn.execute("SELECT person_key,display_name FROM newsflash_roster").fetchall()}
        from packages.newsflash_operations import NewsflashOperationsRepository
        # Reuse only deterministic in-memory assignment and metric functions.
        helper=object.__new__(NewsflashOperationsRepository);helper._effective_publisher=lambda _conn,**kw:self._effective_publisher(kw)
        assigned={};ai_by_day={};unassigned=0;date_set=set(dates)
        for raw in items:
            item=dict(raw);kind,person=self._effective_publisher(item);item["publisher_kind"]=kind;item["publisher_person_key"]=person
            if item.get("is_pushed") is not None:item["is_pushed"]=bool(item["is_pushed"])
            moment=_parse_datetime(item.get("published_at"))
            if not moment:continue
            if kind=="odaily_ai" and moment.date() in date_set:ai_by_day.setdefault(moment.date().isoformat(),[]).append(item);continue
            if kind=="human_unmapped":
                if moment.date() in date_set:unassigned+=1
                continue
            if kind!="human" or item.get("is_contribution"):continue
            window=helper._assign_window(moment,str(person or ""),windows)
            if not window:
                if moment.date() in date_set:unassigned+=1
                continue
            assigned.setdefault((window.duty_date.isoformat(),window.shift_key,window.person_key),[]).append(item)
        rows=[]
        for w in sorted(windows,key=lambda x:(x.duty_date,x.core_start,x.person_name)):
            rows.append({"date":w.duty_date.isoformat(),"shift_key":w.shift_key,"shift_label":w.shift_label,"person_key":w.person_key,"person_name":w.person_name,"is_ai":False,**helper._metric(assigned.get((w.duty_date.isoformat(),w.shift_key,w.person_key),[]))})
        for day in sorted(date_set):rows.append({"date":day.isoformat(),"shift_key":"all_day","shift_label":"全天","person_key":"odaily_ai","person_name":"OdAIly","is_ai":True,**helper._metric(ai_by_day.get(day.isoformat(),[]))})
        grouped={}
        for row in rows:grouped.setdefault(row["person_key"],[]).append(row)
        people=[]
        for key,group in grouped.items():
            published=sum(x["published_count"] for x in group);pushed=sum(x["pushed_count"] for x in group);known=sum(x["view_coverage"]["known"] for x in group);total=sum(x["view_coverage"]["total"] for x in group);weighted=sum((x["average_views"] or 0)*x["view_coverage"]["known"] for x in group)
            people.append({"person_key":key,"person_name":"OdAIly" if key=="odaily_ai" else roster.get(key,key),"published_count":published,"pushed_count":pushed,"average_views":round(weighted/known,1) if known else None,"pushed_views":sum(int(x["pushed_views"] or 0) for x in group) if pushed else None,"view_coverage":{"known":known,"total":total}})
        return {"period":period,"weeks":weeks,"rows":rows,"people":people,"unassigned_count":unassigned}

    def _event_info(self, conn: Any, source_id: str) -> dict[str,Any]:
        row=conn.execute("SELECT first_source_override FROM newsflash_operation_facts WHERE source_item_id=%s",(source_id,)).fetchone();override=str(row["first_source_override"] or "").casefold() if row else ""
        from packages.newsflash_operations import SOURCE_LABELS
        if override in SOURCE_LABELS:return {"status":"ready","sources":[SOURCE_LABELS[override]],"label":SOURCE_LABELS[override],"override_source":override}
        excluded=conn.execute("SELECT 1 FROM newsflash_event_exclusions WHERE source='odaily' AND source_item_id=%s",(source_id,)).fetchone()
        if excluded:return {"status":"excluded","sources":[],"label":"事件已排除（未参与事件归并）","override_source":None}
        event=conn.execute("SELECT e.* FROM newsflash_items i JOIN newsflash_event_sources s ON s.item_id=i.id JOIN newsflash_events e ON e.event_id=s.event_id WHERE i.source='odaily' AND i.source_item_id=%s AND e.status='active' LIMIT 1",(source_id,)).fetchone()
        if not event:return {"status":"unmatched","sources":[],"label":"未匹配到事件","override_source":None}
        sources=list(event["first_sources"] or []) or ([event["first_source"]] if event["first_source"] else []);labels=[SOURCE_LABELS.get(x,x) for x in sources]
        if not event["first_published_at"]:return {"status":"insufficient","sources":[],"label":"数据不足","event_id":event["event_id"],"override_source":None}
        return {"status":"ready","sources":labels,"label":"、".join(labels),"event_id":event["event_id"],"published_at":event["first_published_at"],"override_source":None}

    def _quality_rules(self, conn: Any, week: Any) -> dict[str,Any]:
        from packages.newsflash_operations import QUALITY_REGULAR_SOURCE_ACCOUNTS,QUALITY_EXTERNAL_MEDIA_URLS,QUALITY_EXCLUSION_GROUPS,QUALITY_THRESHOLD_MULTIPLIER,QUALITY_KPI_PER_ITEM
        from urllib.parse import urlparse
        xrows=[]
        if conn.execute("SELECT to_regclass('main.x_capture_accounts') AS relation").fetchone()["relation"]:
            xrows=conn.execute("SELECT username FROM x_capture_accounts WHERE enabled ORDER BY username_lower,username").fetchall()
        media=list(QUALITY_EXTERNAL_MEDIA_URLS)
        if conn.execute("SELECT to_regclass('main.non_mainstream_media_sources') AS relation").fetchone()["relation"]:
            media.extend(r["homepage_url"] for r in conn.execute("SELECT homepage_url FROM non_mainstream_media_sources WHERE enabled ORDER BY site_key").fetchall() if r["homepage_url"])
        domains=sorted({(urlparse(v).hostname or "").casefold().removeprefix("www.") for v in media if urlparse(v).hostname})
        seed={"regular_source_accounts":list(QUALITY_REGULAR_SOURCE_ACCOUNTS),"automated_x_accounts":[r["username"] for r in xrows],"automated_media_domains":domains,"keyword_groups":[{"key":g["key"],"terms":list(g["terms"]),"scope":g.get("scope","title_content"),"label":g.get("label") or " + ".join(g["terms"])} for g in QUALITY_EXCLUSION_GROUPS],"threshold_multiplier":QUALITY_THRESHOLD_MULTIPLIER,"kpi_per_item":QUALITY_KPI_PER_ITEM}
        from psycopg.types.json import Jsonb
        row=conn.execute("INSERT INTO newsflash_quality_week_rules(week_start,rules_json) VALUES(%s,%s) ON CONFLICT(week_start) DO NOTHING RETURNING rules_json,created_at",(week,Jsonb(seed))).fetchone()
        if not row:row=conn.execute("SELECT rules_json,created_at FROM newsflash_quality_week_rules WHERE week_start=%s FOR UPDATE",(week,)).fetchone()
        rules=row["rules_json"] if isinstance(row["rules_json"],dict) else json.loads(row["rules_json"])
        existing={str(g.get("key")) for g in rules.get("keyword_groups",[]) if isinstance(g,dict)}
        missing=[g for g in seed["keyword_groups"] if g["key"] not in existing]
        changed=False
        if missing:rules["keyword_groups"]=[*rules.get("keyword_groups",[]),*missing];changed=True
        if rules.get("regular_source_accounts")!=seed["regular_source_accounts"]:rules["regular_source_accounts"]=seed["regular_source_accounts"];changed=True
        if changed:conn.execute("UPDATE newsflash_quality_week_rules SET rules_json=%s WHERE week_start=%s",(Jsonb(rules),week))
        rules["snapshot_at"]=row["created_at"];return rules

    def get_quality(self, payload: dict[str,Any]) -> dict[str,Any]:
        from datetime import date, datetime, time, timedelta
        from packages.newsflash_operations import (QUALITY_FIRST_WEEK,SHANGHAI_TZ,QUALITY_KPI_PER_ITEM,
            _parse_datetime,_week_start,QUALITY_REGULAR_SOURCE_ACCOUNTS)
        raw=str(payload.get("week_start") or "").strip();start=_week_start(date.fromisoformat(raw)) if raw else None
        with self._connect() as conn:
            if start is None:
                row=conn.execute("SELECT max(r.published_at) AS latest FROM newsflash_operation_facts f JOIN odaily_reference_items r USING(source_item_id) WHERE f.is_pushed AND f.view_count IS NOT NULL AND r.published_at >= %s",(datetime.combine(QUALITY_FIRST_WEEK,time(),SHANGHAI_TZ),)).fetchone()
                latest=_parse_datetime(row["latest"]);start=_week_start(latest.date()) if latest else max(QUALITY_FIRST_WEEK,_week_start(datetime.now(SHANGHAI_TZ).date()))
            if start<QUALITY_FIRST_WEEK:raise ValueError(f"quality week must not be earlier than {QUALITY_FIRST_WEEK.isoformat()}")
            end=start+timedelta(days=7);rules=self._quality_rules(conn,start)
            rows=[dict(r) for r in conn.execute("SELECT r.source_item_id,r.source_url,r.title,r.content,r.raw_payload,r.published_at,f.publisher_kind,f.publisher_person_key,f.view_count,f.is_pushed,f.is_contribution,f.quality_override FROM odaily_reference_items r JOIN newsflash_operation_facts f USING(source_item_id) WHERE r.published_at >= %s AND r.published_at < %s ORDER BY r.published_at DESC,r.source_item_id DESC",(datetime.combine(start,time(),SHANGHAI_TZ),datetime.combine(end,time(),SHANGHAI_TZ))).fetchall()]
            pushed=[r for r in rows if r["is_pushed"]];views=[int(r["view_count"]) for r in pushed if r["view_count"] is not None]
            base={"status":"ready" if views else "insufficient","week_start":start.isoformat(),"week_end":(end-timedelta(days=1)).isoformat(),"in_progress":datetime.now(SHANGHAI_TZ).date()<end,"pushed_count":len(pushed),"pushed_view_count":len(views),"rules":rules}
            if not views:return {**base,"average_views":None,"threshold_views":None,"qualified_count":0,"excluded_count":0,"total_kpi":0,"unassigned_count":0,"groups":[]}
            avg=sum(views)/len(views);threshold=avg*float(rules["threshold_multiplier"]);windows=self._shift_windows(conn,[start+timedelta(days=i) for i in range(7)])
            people={w.person_key:w.person_name for w in windows};groups={k:{"qualified":[],"excluded":[]} for k in people};unassigned=0
            regular={str(x).casefold() for x in rules["regular_source_accounts"]};auto_x={str(x).casefold() for x in rules["automated_x_accounts"]};auto_domains={str(x).casefold() for x in rules["automated_media_domains"]}
            from urllib.parse import urlparse
            helper=object.__new__(__import__("packages.newsflash_operations",fromlist=["NewsflashOperationsRepository"]).NewsflashOperationsRepository)
            for row in rows:
                if row["publisher_kind"] not in {"human","human_unmapped"}:continue
                moment=_parse_datetime(row["published_at"])
                original=str((row["raw_payload"] or {}).get("sourceUrl") or row["source_url"] or "") if isinstance(row["raw_payload"],dict) else str(row["source_url"] or "")
                parsed=urlparse(original);host=(parsed.hostname or "").casefold().removeprefix("www.");user=(parsed.path.split("/")[1] if parsed.path.startswith("/") and len(parsed.path.split("/"))>1 else "").casefold()
                window=helper._assign_window(moment,str(row["publisher_person_key"] or ""),windows) if moment and row["publisher_kind"]=="human" else None
                if not window:unassigned+=1;continue
                override=str(row["quality_override"] or "none");reasons=[];labels=[]
                if override!="include" and (row["view_count"] is None or int(row["view_count"])<=threshold):continue
                first,first_sources=self._quality_first(conn,row["source_item_id"])
                if override!="include":
                    if row["is_contribution"]:reasons.append("contribution");labels.append("贡献快讯")
                    if first.get("status")=="ready" and first_sources and "odaily" not in first_sources:reasons.append("competitor_first");labels.append("晚于竞品")
                    if user in regular:reasons.append("regular_source");labels.append("常规信源")
                    if user in auto_x or any(host==d or host.endswith("."+d) for d in auto_domains):reasons.append("automated_coverage");labels.append("自动覆盖")
                    if "金十" in str(row["content"] or ""):reasons.append("jin10_content");labels.append("正文含金十")
                    scopes={"title":str(row["title"] or "").casefold(),"content":str(row["content"] or "").casefold(),"title_content":(str(row["title"] or "")+"\n"+str(row["content"] or "")).casefold()}
                    for g in rules.get("keyword_groups",[]):
                        terms=[str(t) for t in g.get("terms",[]) if str(t).strip()]
                        if terms and all(t.casefold() in scopes.get(g.get("scope"),scopes["title_content"]) for t in terms):reasons.append("keyword_"+str(g.get("key")));labels.append("排除词："+str(g.get("label") or " + ".join(terms)))
                    if override=="exclude":reasons.append("manual_exclude");labels.append("人工排除")
                item={"source_item_id":row["source_item_id"],"odaily_url":f"https://www.odaily.news/zh-CN/newsflash/{row['source_item_id']}","original_url":original or None,"has_original_url":bool(original),"title":row["title"],"published_at":row["published_at"],"view_count":row["view_count"],"is_pushed":row["is_pushed"],"first_publication":first,"quality_override":override,"exclusion_reasons":reasons,"exclusion_reason_labels":labels}
                groups[window.person_key]["excluded" if reasons else "qualified"].append(item)
            out=[]
            for key,name in sorted(people.items(),key=lambda x:(x[1].casefold(),x[0])):
                q=groups[key]["qualified"];ex=groups[key]["excluded"];out.append({"person_key":key,"person_name":name,"qualified_count":len(q),"excluded_count":len(ex),"kpi":round(len(q)*float(rules["kpi_per_item"]),10),"qualified":q,"excluded":ex})
        qn=sum(x["qualified_count"] for x in out);en=sum(x["excluded_count"] for x in out)
        return {**base,"average_views":avg,"threshold_views":threshold,"qualified_count":qn,"excluded_count":en,"total_kpi":round(qn*float(rules["kpi_per_item"]),10),"unassigned_count":unassigned,"groups":out}

    def list_quality_monthly(self,payload:dict[str,Any])->dict[str,Any]:
        from datetime import date
        from packages.newsflash_operations import QUALITY_FIRST_WEEK
        month=str(payload.get("report_month") or datetime.now().strftime("%Y-%m"))
        with self._connect() as conn:_,label,weeks=self._period_dates(conn,{"report_month":month})
        if any(date.fromisoformat(w)<QUALITY_FIRST_WEEK for w in weeks):raise ValueError(f"quality month must not include a week earlier than {QUALITY_FIRST_WEEK.isoformat()}")
        results=[self.get_quality({"week_start":w}) for w in weeks];names={g["person_key"]:g["person_name"] for r in results for g in r["groups"]};people=[]
        for key,name in sorted(names.items(),key=lambda x:(x[1].casefold(),x[0])):
            ws=[]
            for r in results:
                g=next((x for x in r["groups"] if x["person_key"]==key),None);ws.append({"week_start":r["week_start"],"week_end":r["week_end"],"status":r["status"],"qualified_count":g["qualified_count"] if g else 0,"excluded_count":g["excluded_count"] if g else 0,"kpi":g["kpi"] if g else 0})
            people.append({"person_key":key,"person_name":name,"qualified_count":sum(x["qualified_count"] for x in ws),"excluded_count":sum(x["excluded_count"] for x in ws),"kpi":round(sum(x["kpi"] for x in ws),10),"weeks":ws})
        return {"report_month":label,"in_progress":any(r["in_progress"] for r in results),"weeks":[{k:r[k] for k in ("week_start","week_end","status","in_progress","average_views","threshold_views","qualified_count","excluded_count","total_kpi")} for r in results],"qualified_count":sum(r["qualified_count"] for r in results),"excluded_count":sum(r["excluded_count"] for r in results),"total_kpi":round(sum(r["total_kpi"] for r in results),10),"people":people}

    def _quality_first(self,conn:Any,source_id:str)->tuple[dict[str,Any],list[str]]:
        from packages.newsflash_operations import SOURCE_LABELS
        info=self._event_info(conn,source_id);row=conn.execute("SELECT first_source_override FROM newsflash_operation_facts WHERE source_item_id=%s",(source_id,)).fetchone();override=str(row["first_source_override"] or "").casefold() if row else ""
        if override in SOURCE_LABELS:return info,[override]
        if info.get("status")!="ready":return info,[]
        event=conn.execute("SELECT e.first_source,e.first_sources FROM newsflash_items i JOIN newsflash_event_sources s ON s.item_id=i.id JOIN newsflash_events e ON e.event_id=s.event_id WHERE i.source='odaily' AND i.source_item_id=%s AND e.status='active' LIMIT 1",(source_id,)).fetchone()
        if not event:return info,[]
        values=list(event["first_sources"] or []) or ([event["first_source"]] if event["first_source"] else [])
        return info,[str(v).casefold() for v in values if str(v or "").strip()]

    def _quality_original(self,row:dict[str,Any])->str|None:
        from packages.newsflash_operations import _decode
        raw=row.get("raw_payload");payload=raw if isinstance(raw,dict) else _decode(raw,{})
        source=payload.get("sourceUrl") if isinstance(payload,dict) else None
        if str(source or "").strip():return str(source).strip()
        fallback=str(row.get("source_url") or "").strip()
        from packages.newsflash_operations import NewsflashOperationsRepository
        return fallback if fallback and not NewsflashOperationsRepository._is_odaily_url(fallback) else None

    def _list_contributions_for_week(self,conn:Any,start:Any)->dict[str,Any]:
        from datetime import datetime,time,timedelta
        from packages.newsflash_operations import (SHANGHAI_TZ,QUALITY_REGULAR_SOURCE_ACCOUNTS,CONTRIBUTION_TYPES,
            CONTRIBUTION_SCORE_CAP,_week_start,NewsflashOperationsRepository)
        begin=datetime.combine(start,time(),SHANGHAI_TZ);end=begin+timedelta(days=7)
        baseline=conn.execute("SELECT f.view_count FROM newsflash_operation_facts f JOIN odaily_reference_items r USING(source_item_id) WHERE f.is_pushed AND r.published_at >= %s AND r.published_at < %s",(begin,end)).fetchall();views=[int(r["view_count"]) for r in baseline if r["view_count"] is not None];avg=sum(views)/len(views) if views else None
        people=[dict(r) for r in conn.execute("SELECT person_key,display_name FROM newsflash_roster WHERE active AND contributor_enabled ORDER BY lower(display_name),person_key").fetchall()]
        rows=[dict(r) for r in conn.execute("SELECT r.source_item_id,r.source_url,r.raw_payload,r.title,r.published_at,f.view_count,f.contribution_type,f.contributor_person_key,p.display_name contributor_name FROM newsflash_operation_facts f JOIN odaily_reference_items r USING(source_item_id) JOIN newsflash_roster p ON p.person_key=f.contributor_person_key WHERE f.is_contribution AND r.published_at >= %s AND r.published_at < %s ORDER BY lower(p.display_name),r.published_at DESC",(begin,end)).fetchall()]
        for row in rows:
            source=row["source_item_id"];url=self._quality_original(row);user=NewsflashOperationsRepository._x_username(url);reasons=[];labels=[]
            if user and user.casefold() in {x.casefold() for x in QUALITY_REGULAR_SOURCE_ACCOUNTS}:reasons.append("regular_source");labels.append("常规信源")
            first,sources=self._quality_first(conn,source)
            if row["contribution_type"]!="late_different_info" and first.get("status")=="ready" and sources and "odaily" not in sources:reasons.append("competitor_first");labels.append("竞品先发")
            score=NewsflashOperationsRepository._contribution_score(row["view_count"],row["contribution_type"],avg,excluded=bool(reasons))
            row.update({"first_publication":first,"original_url":url,"score_exclusion_reasons":reasons,"score_exclusion_reason_labels":labels,"score_zeroed_by_rules":bool(reasons and score["score_before_exclusion"]>0),**score})
        groups=[]
        for person in people:
            items=[r for r in rows if r["contributor_person_key"]==person["person_key"]];known=[int(r["view_count"]) for r in items if r["view_count"] is not None];base=round(sum(float(r["base_score"]) for r in items),10);bonus_count,bonus=NewsflashOperationsRepository._apply_contribution_bonus_cap(items,base);capped=min(base,CONTRIBUTION_SCORE_CAP)
            groups.append({**person,"count":len(items),"total_views":sum(known),"average_views":round(sum(known)/len(known),1) if known else None,"view_coverage":{"known":len(known),"total":len(items)},"base_score_total":base,"base_score_capped":capped,"high_view_bonus_count":bonus_count,"high_view_bonus":bonus,"total_score":round(min(base,capped+bonus),10),"items":items})
        return {"week_start":start.isoformat(),"week_end":(end-timedelta(days=1)).date().isoformat(),"in_progress":datetime.now(SHANGHAI_TZ).date()<end.date(),"status":"ready" if avg is not None else "insufficient","baseline":{"pushed_count":len(baseline),"known_view_count":len(views),"average_views":avg},"groups":groups}

    def list_contributions(self,payload:dict[str,Any])->dict[str,Any]:
        from datetime import date,datetime
        from packages.newsflash_operations import SHANGHAI_TZ,_week_start
        start=_week_start(date.fromisoformat(str(payload.get("week_start") or _week_start(datetime.now(SHANGHAI_TZ).date()))))
        with self._connect() as conn:return self._list_contributions_for_week(conn,start)

    def list_contributions_monthly(self,payload:dict[str,Any])->dict[str,Any]:
        report_month=str(payload.get("report_month") or datetime.now().strftime("%Y-%m"))
        with self._connect() as conn:
            _,label,weeks=self._period_dates(conn,{"report_month":report_month});results=[self._list_contributions_for_week(conn,__import__("datetime").date.fromisoformat(w)) for w in weeks]
            people=[dict(r) for r in conn.execute("SELECT person_key,display_name FROM newsflash_roster WHERE active AND contributor_enabled ORDER BY lower(display_name),person_key").fetchall()]
        out=[]
        for person in people:
            weekly=[{"week_start":r["week_start"],"week_end":r["week_end"],"status":r["status"],"count":g["count"],"total_views":g["total_views"],"average_views":g["average_views"],"view_coverage":g["view_coverage"],"total_score":g["total_score"]} for r in results for g in r["groups"] if g["person_key"]==person["person_key"]]
            known=sum(x["view_coverage"]["known"] for x in weekly);total=sum(x["view_coverage"]["total"] for x in weekly);views=sum(x["total_views"] for x in weekly)
            out.append({**person,"count":sum(x["count"] for x in weekly),"total_views":views,"average_views":round(views/known,1) if known else None,"view_coverage":{"known":known,"total":total},"total_score":round(sum(x["total_score"] for x in weekly),10),"weeks":weekly})
        return {"report_month":label,"in_progress":any(r["in_progress"] for r in results),"insufficient_week_count":sum(r["status"]=="insufficient" for r in results),"weeks":[{"week_start":r["week_start"],"week_end":r["week_end"],"status":r["status"],"in_progress":r["in_progress"],"baseline":r["baseline"]} for r in results],"people":out}
