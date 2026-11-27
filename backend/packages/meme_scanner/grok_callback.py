from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
EVM_CA_PATTERN = re.compile(r"0x[0-9a-fA-F]{40}", re.IGNORECASE)
MAX_CALLBACK_BYTES = 2 * 1024 * 1024
MAX_REGISTRATION_BYTES = 8 * 1024
MAX_ACTIVE_REQUESTS = 1000
PENDING_RETENTION = timedelta(hours=1)
RESULT_RETENTION = timedelta(days=7)
_STORE_LOCK = threading.Lock()

CALLBACK_FAILURE_MESSAGES = {
    "invalid_request_id": "Callback request ID is invalid.",
    "invalid_token": "Callback token is invalid.",
    "invalid_body_size": "Callback body size is invalid.",
    "invalid_json": "Callback body is not valid JSON.",
    "invalid_result": "Callback result must be a JSON object.",
    "invalid_type": "Callback result type must be meme_narrative.",
    "request_id_mismatch": "Result request_id does not match the callback URL.",
    "missing_ca": "Result CA is missing.",
    "registration_not_found": "Callback registration was not found.",
    "registration_not_pending": "Callback registration is not pending.",
    "token_mismatch": "Callback token does not match this request.",
    "ca_mismatch": "Result CA does not match the registered CA.",
    "registration_expired": "Callback registration has expired.",
    "registration_invalid": "Callback registration data is invalid.",
    "duplicate_result": "A different result was already accepted for this request.",
}


def valid_request_id(value: str) -> bool:
    return isinstance(value, str) and bool(REQUEST_ID_PATTERN.fullmatch(value)) and value not in {".", ".."}


def callback_result_path(root: Path, request_id: str) -> Path:
    if not valid_request_id(request_id):
        raise ValueError("Invalid request_id.")
    return root / f"{request_id}.json"


def callback_ca_matches(candidate: Any, registered: Any) -> bool:
    if not isinstance(candidate, str) or not isinstance(registered, str):
        return False
    if candidate == registered:
        return True
    return bool(
        EVM_CA_PATTERN.fullmatch(candidate)
        and EVM_CA_PATTERN.fullmatch(registered)
        and candidate.lower() == registered.lower()
    )


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _pending_expiry(record: dict[str, Any]) -> datetime | None:
    expires_at = record.get("pending_expires_at")
    if expires_at is not None:
        return _parse_utc(expires_at)
    created_at = _parse_utc(record.get("created_at"))
    return created_at + PENDING_RETENTION if created_at else None


def _write_record(root: Path, path: Path, record: dict[str, Any], *, exclusive: bool) -> bool:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    mode = "x" if exclusive else "w"
    try:
        with path.open(mode, encoding="utf-8") as output:
            path.chmod(0o600)
            json.dump(record, output, ensure_ascii=False)
            output.write("\n")
    except FileExistsError:
        return False
    return True


def create_callback_registration(
    root: Path,
    request_id: str,
    ca: str,
    *,
    pending_retention: timedelta = PENDING_RETENTION,
) -> str | None:
    if not valid_request_id(request_id) or not ca or len(ca) > 256 or pending_retention <= timedelta(0):
        return None
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _STORE_LOCK:
        prune_old_results(root)
        active = 0
        for path in root.glob("*.json"):
            record = _read_record(path)
            if record is not None and record.get("state") == "pending":
                active += 1
        if active >= MAX_ACTIVE_REQUESTS:
            return None
        path = callback_result_path(root, request_id)
        token = secrets.token_urlsafe(32)
        created_at = datetime.now(UTC)
        record = {
            "state": "pending",
            "request_id": request_id,
            "ca": ca,
            "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "created_at": created_at.isoformat(),
            "pending_expires_at": (created_at + pending_retention).isoformat(),
        }
        return token if _write_record(root, path, record, exclusive=True) else None


def store_callback_result_with_reason(
    root: Path, request_id: str, token: str, raw_body: bytes
) -> str | None:
    if not valid_request_id(request_id):
        return "invalid_request_id"
    if not isinstance(token, str) or not token or len(token) > 256:
        return "invalid_token"
    if len(raw_body) < 1 or len(raw_body) > MAX_CALLBACK_BYTES:
        return "invalid_body_size"
    try:
        result = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid_json"
    if not isinstance(result, dict):
        return "invalid_result"
    if result.get("type") != "meme_narrative":
        return "invalid_type"
    if result.get("request_id") != request_id:
        return "request_id_mismatch"
    if not isinstance(result.get("ca"), str) or not result["ca"].strip():
        return "missing_ca"

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _STORE_LOCK:
        path = callback_result_path(root, request_id)
        record = _read_record(path)
        if record is None:
            return "registration_not_found"
        supplied_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(str(record.get("token_sha256", "")), supplied_hash):
            return "token_mismatch"
        if not callback_ca_matches(result["ca"], record.get("ca")):
            return "ca_mismatch"
        state = record.get("state")
        if state == "complete":
            return None if record.get("result") == result else "duplicate_result"
        if state != "pending":
            return "registration_not_pending"
        expires_at = _pending_expiry(record)
        if expires_at is None:
            return "registration_invalid"
        if expires_at <= datetime.now(UTC):
            path.unlink(missing_ok=True)
            return "registration_expired"
        completed = {
            **record,
            "state": "complete",
            "received_at": datetime.now(UTC).isoformat(),
            "result": result,
        }
        temporary_path = path.with_suffix(".tmp")
        _write_record(root, temporary_path, completed, exclusive=False)
        temporary_path.replace(path)
        path.chmod(0o600)
        prune_old_results(root)
    return None


def store_callback_result(root: Path, request_id: str, token: str, raw_body: bytes) -> bool:
    return store_callback_result_with_reason(root, request_id, token, raw_body) is None


def load_callback_result(root: Path, request_id: str, token: str) -> dict[str, Any] | None:
    if not valid_request_id(request_id) or not token or len(token) > 256:
        return None
    with _STORE_LOCK:
        record = _read_record(callback_result_path(root, request_id))
        if record is None or record.get("state") != "complete":
            return None
        supplied_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(str(record.get("token_sha256", "")), supplied_hash):
            return None
        result = record.get("result")
        return result if isinstance(result, dict) else None


def prune_old_results(root: Path, *, now: datetime | None = None) -> int:
    current = now or datetime.now(UTC)
    removed = 0
    for path in root.glob("*.json"):
        record = _read_record(path)
        if record is None:
            continue
        if record.get("state") == "complete":
            created = _parse_utc(record.get("received_at") or record.get("created_at"))
            expires_at = created + RESULT_RETENTION if created else None
        else:
            expires_at = _pending_expiry(record)
        if expires_at is not None and expires_at <= current:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
