from __future__ import annotations

import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Protocol

from packages.common.postgres import build_psycopg_connect_kwargs, load_database_url


SOURCE_EXCLUSION_SCOPES = (
    "x",
    "competitor",
    "crypto_source",
    "ai_source",
    "mixed_source",
    "jin10",
)


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
    enabled: bool = True


class SourceExclusionRepository(Protocol):
    def list_enabled_rule_groups(self) -> list[SourceExclusionRuleGroup]: ...


def normalize_exclusion_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(normalized.split())


def is_source_excluded(
    groups: Iterable[SourceExclusionRuleGroup],
    *,
    scopes: Iterable[str],
    texts: Iterable[str | None],
) -> bool:
    active_scopes = {scope for scope in scopes if scope in SOURCE_EXCLUSION_SCOPES}
    if not active_scopes:
        return False
    haystack = normalize_exclusion_text("\n".join(text or "" for text in texts))
    if not haystack:
        return False
    for group in groups:
        if not group.enabled or not active_scopes.intersection(group.scopes):
            continue
        for term in group.terms:
            normalized_term = normalize_exclusion_text(term)
            if normalized_term and normalized_term in haystack:
                return True
    return False


class PostgresSourceExclusionRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or load_database_url()
        self.application_name = "odaily-source-exclusions"

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except Exception as exc:  # pragma: no cover - dependency guard.
            raise RuntimeError("psycopg is required for source exclusion rules") from exc
        return psycopg.connect(
            self.database_url,
            **build_psycopg_connect_kwargs(
                row_factory=dict_row,
                autocommit=True,
                application_name=self.application_name,
            ),
        )

    def list_enabled_rule_groups(self) -> list[SourceExclusionRuleGroup]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT rule_key, name, description, scopes, terms, enabled
                FROM source_exclusion_rule_groups
                WHERE enabled = true
                ORDER BY name ASC, rule_key ASC
                """
            ).fetchall()
        return [
            SourceExclusionRuleGroup(
                rule_key=str(row["rule_key"]),
                name=str(row["name"]),
                description=str(row.get("description") or ""),
                scopes=tuple(str(value) for value in (row.get("scopes") or [])),
                terms=tuple(str(value) for value in (row.get("terms") or [])),
                enabled=bool(row.get("enabled", True)),
            )
            for row in rows
        ]


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

    def is_excluded(self, *, scopes: Iterable[str], texts: Iterable[str | None]) -> bool:
        return is_source_excluded(self._load_groups(), scopes=scopes, texts=texts)

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
