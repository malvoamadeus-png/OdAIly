from __future__ import annotations

import threading
import time
import unicodedata
import json
from dataclasses import dataclass
from typing import Iterable, Protocol

from packages.common.pipeline_schema import COMPETITOR_FILTER_SCHEMA_SQL
from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres
from packages.common.storage import connect_sqlite, load_storage_settings


SOURCE_EXCLUSION_SCOPES = (
    "x",
    "competitor",
    "crypto_source",
    "ai_source",
    "mixed_source",
    "jin10",
    "fact_auditor",
)

SOURCE_EXCLUSION_MATCH_TARGETS = ("title", "all")


def media_source_exclusion_scopes(
    source_group: str,
    *,
    classified_target: str | None = None,
) -> tuple[str, ...]:
    if source_group == "mixed_source":
        scopes = ["mixed_source"]
        if classified_target == "crypto":
            scopes.append("crypto_source")
        elif classified_target == "ai":
            scopes.append("ai_source")
        return tuple(scopes)
    if source_group == "ai_source":
        return ("ai_source",)
    return ("crypto_source",)


@dataclass(frozen=True, slots=True)
class SourceExclusionRuleGroup:
    rule_key: str
    name: str
    description: str
    scopes: tuple[str, ...]
    terms: tuple[str, ...]
    match_target: str = "title"
    enabled: bool = True


class SourceExclusionRepository(Protocol):
    def list_enabled_rule_groups(self) -> list[SourceExclusionRuleGroup]: ...


def normalize_exclusion_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(normalized.split())


def normalize_exclusion_match_target(value: str | None) -> str:
    normalized = str(value or "").strip().casefold()
    return normalized if normalized in SOURCE_EXCLUSION_MATCH_TARGETS else "title"


def is_source_excluded(
    groups: Iterable[SourceExclusionRuleGroup],
    *,
    scopes: Iterable[str],
    title_texts: Iterable[str | None] | None = None,
    body_texts: Iterable[str | None] | None = None,
    texts: Iterable[str | None] | None = None,
) -> bool:
    active_scopes = {scope for scope in scopes if scope in SOURCE_EXCLUSION_SCOPES}
    if not active_scopes:
        return False
    if title_texts is None and body_texts is None and texts is not None:
        title_texts = texts
    title_haystack = normalize_exclusion_text("\n".join(text or "" for text in (title_texts or [])))
    body_haystack = normalize_exclusion_text("\n".join(text or "" for text in (body_texts or [])))
    if not title_haystack and not body_haystack:
        return False
    for group in groups:
        if not group.enabled or not active_scopes.intersection(group.scopes):
            continue
        match_target = normalize_exclusion_match_target(getattr(group, "match_target", "title"))
        haystack = title_haystack if match_target == "title" else normalize_exclusion_text(f"{title_haystack}\n{body_haystack}")
        if not haystack:
            continue
        for term in group.terms:
            normalized_term = normalize_exclusion_text(term)
            if normalized_term and normalized_term in haystack:
                return True
    return False


class SQLiteSourceExclusionRepository:
    def __init__(self, path) -> None:
        self.path = path
        with connect_sqlite(path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_exclusion_rule_groups (
                    id integer PRIMARY KEY AUTOINCREMENT,
                    rule_key text NOT NULL UNIQUE,
                    name text NOT NULL UNIQUE,
                    description text NOT NULL DEFAULT '',
                    scopes text NOT NULL,
                    terms text NOT NULL,
                    match_target text NOT NULL DEFAULT 'title',
                    enabled integer NOT NULL DEFAULT 1,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Seed a separate fact-auditor rule set once from the existing
            # competitor groups. Later edits to either scope stay independent.
            has_fact_rules = conn.execute(
                "SELECT 1 FROM source_exclusion_rule_groups WHERE scopes LIKE '%fact_auditor%' LIMIT 1"
            ).fetchone()
            if not has_fact_rules:
                rows = conn.execute(
                    "SELECT rule_key, name, description, terms, match_target, enabled FROM source_exclusion_rule_groups WHERE scopes LIKE '%competitor%' ORDER BY name, rule_key"
                ).fetchall()
                for row in rows:
                    conn.execute(
                        "INSERT OR IGNORE INTO source_exclusion_rule_groups (rule_key, name, description, scopes, terms, match_target, enabled) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            f"fact-auditor-copy-{row['rule_key']}",
                            f"事实审核者 · {row['name']}",
                            f"从竞品规则复制的初始词表。{row['description'] or ''}".strip(),
                            json.dumps(["fact_auditor"], ensure_ascii=False),
                            row["terms"],
                            row["match_target"],
                            row["enabled"],
                        ),
                    )
            conn.commit()

    def list_enabled_rule_groups(self) -> list[SourceExclusionRuleGroup]:
        with connect_sqlite(self.path) as conn:
            rows = conn.execute(
                "SELECT rule_key, name, description, scopes, terms, match_target, enabled FROM source_exclusion_rule_groups WHERE enabled=1 ORDER BY name, rule_key"
            ).fetchall()
        return [
            SourceExclusionRuleGroup(
                rule_key=str(row["rule_key"]),
                name=str(row["name"]),
                description=str(row["description"] or ""),
                scopes=tuple(str(value) for value in json.loads(row["scopes"])),
                terms=tuple(str(value) for value in json.loads(row["terms"])),
                match_target=normalize_exclusion_match_target(row["match_target"]),
                enabled=bool(row["enabled"]),
            )
            for row in rows
        ]


class PostgresSourceExclusionRepository:
    def __init__(self) -> None:
        self.application_name = "odaily-source-exclusions"

    def init_schema(self) -> None:
        import hashlib

        with connect_postgres(schema="main", application_name=self.application_name) as connection:
            apply_schema_migrations(
                connection,
                schema="main",
                component="source_exclusions",
                migrations=(PostgresMigration(
                    1, "source_exclusions_initial", hashlib.sha256(COMPETITOR_FILTER_SCHEMA_SQL.encode()).hexdigest(),
                    lambda conn: conn.execute(COMPETITOR_FILTER_SCHEMA_SQL),
                ),),
            )
            with connection.transaction():
                connection.execute(
                    """INSERT INTO source_exclusion_rule_groups(
                        rule_key,name,description,scopes,terms,match_target,enabled
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(rule_key) DO NOTHING""",
                    ("ripple-xrp-commercial-content", "Ripple / XRP 商务稿",
                     "从 Crypto 信源入口屏蔽 Ripple、XRP、RLUSD 商务稿。",
                     ["crypto_source"], ["Ripple", "XRP", "RLUSD"], "title", True),
                )
                connection.execute(
                    """INSERT INTO source_exclusion_rule_groups(
                        rule_key,name,description,scopes,terms,match_target,enabled
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(rule_key) DO NOTHING""",
                    ("competitor-magne-ai", "MAGNE.AI 竞品屏蔽",
                     "从竞品入口屏蔽标题或正文出现 MAGNE.AI 的内容。",
                     ["competitor"], ["MAGNE.AI"], "all", True),
                )
                with connection.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO competitor_filter_keywords(term,term_normalized,enabled) VALUES(%s,%s,true) ON CONFLICT(term_normalized) DO NOTHING",
                        ((term, term.casefold()) for term in ("跌破", "突破", "爆仓", "Bitget")),
                    )
                has_fact_rules = connection.execute(
                    "SELECT 1 FROM source_exclusion_rule_groups WHERE 'fact_auditor'=ANY(scopes) LIMIT 1"
                ).fetchone()
                if not has_fact_rules:
                    rules = connection.execute(
                        "SELECT rule_key,name,description,terms,match_target,enabled FROM source_exclusion_rule_groups "
                        "WHERE 'competitor'=ANY(scopes) ORDER BY name,rule_key"
                    ).fetchall()
                    for row in rules:
                        connection.execute(
                            """INSERT INTO source_exclusion_rule_groups(
                                rule_key,name,description,scopes,terms,match_target,enabled
                            ) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(rule_key) DO NOTHING""",
                            (f"fact-auditor-copy-{row['rule_key']}", f"事实审核者 · {row['name']}",
                             f"从竞品规则复制的初始词表。{row['description'] or ''}".strip(),
                             ["fact_auditor"], row["terms"], row["match_target"], row["enabled"]),
                        )

    def list_enabled_rule_groups(self) -> list[SourceExclusionRuleGroup]:
        self.init_schema()
        with connect_postgres(schema="main", application_name=self.application_name) as connection:
            rows = connection.execute(
                "SELECT rule_key,name,description,scopes,terms,match_target,enabled "
                "FROM source_exclusion_rule_groups WHERE enabled=true ORDER BY name,rule_key"
            ).fetchall()
        return [SourceExclusionRuleGroup(
            rule_key=str(row["rule_key"]), name=str(row["name"]), description=str(row["description"] or ""),
            scopes=tuple(str(value) for value in row["scopes"]), terms=tuple(str(value) for value in row["terms"]),
            match_target=normalize_exclusion_match_target(row["match_target"]), enabled=bool(row["enabled"]),
        ) for row in rows]


def create_source_exclusion_repository(database_url: str | None = None, *, path=None) -> SourceExclusionRepository:
    if database_url is not None:
        raise ValueError("Configure ODAILY_POSTGRES_DSN instead of passing a database URL")
    if load_storage_settings().backend == "postgres":
        return PostgresSourceExclusionRepository()
    return SQLiteSourceExclusionRepository(path or load_storage_settings().sqlite_path)


class SourceExclusionMatcher:
    def __init__(
        self,
        repository: SourceExclusionRepository,
        *,
        cache_seconds: float = 60.0,
    ) -> None:
        self.repository = repository
        self.cache_seconds = max(5.0, float(cache_seconds))
        self._groups: list[SourceExclusionRuleGroup] = []
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def is_excluded(
        self,
        *,
        scopes: Iterable[str],
        title_texts: Iterable[str | None] | None = None,
        body_texts: Iterable[str | None] | None = None,
        texts: Iterable[str | None] | None = None,
    ) -> bool:
        return is_source_excluded(
            self._load_groups(),
            scopes=scopes,
            title_texts=title_texts,
            body_texts=body_texts,
            texts=texts,
        )

    def _load_groups(self) -> list[SourceExclusionRuleGroup]:
        now = time.monotonic()
        if now - self._loaded_at < self.cache_seconds:
            return self._groups
        with self._lock:
            now = time.monotonic()
            if now - self._loaded_at < self.cache_seconds:
                return self._groups
            try:
                groups = self.repository.list_enabled_rule_groups()
            except Exception as exc:
                print(f"[odaily] source exclusion rules load failed error={exc}")
                self._loaded_at = now
                return self._groups
            self._groups = groups
            self._loaded_at = now
            return self._groups
