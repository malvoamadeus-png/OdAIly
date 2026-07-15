from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv


DEFAULT_POSTGRES_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_POSTGRES_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS = 60_000


def load_database_url(database_url: str | None = None) -> str:
    if database_url:
        return database_url
    load_dotenv()
    value = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("Missing SUPABASE_DB_URL or DATABASE_URL")
    return value


def get_postgres_connect_timeout_seconds(default: int = DEFAULT_POSTGRES_CONNECT_TIMEOUT_SECONDS) -> int:
    value = str(os.getenv("POSTGRES_CONNECT_TIMEOUT_SECONDS") or default).strip()
    try:
        return max(1, int(value))
    except ValueError:
        return default


def get_postgres_idle_in_transaction_session_timeout_ms(
    default: int = DEFAULT_POSTGRES_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS,
) -> int:
    value = str(os.getenv("POSTGRES_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS") or default).strip()
    try:
        return max(0, int(value))
    except ValueError:
        return default


def build_psycopg_connect_kwargs(
    *,
    row_factory: Any,
    autocommit: bool = False,
    application_name: str | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "row_factory": row_factory,
        "autocommit": autocommit,
        "connect_timeout": get_postgres_connect_timeout_seconds(),
    }
    if application_name:
        kwargs["application_name"] = application_name
    idle_timeout_ms = get_postgres_idle_in_transaction_session_timeout_ms()
    if idle_timeout_ms > 0:
        kwargs["options"] = f"-c idle_in_transaction_session_timeout={idle_timeout_ms}"
    return kwargs
