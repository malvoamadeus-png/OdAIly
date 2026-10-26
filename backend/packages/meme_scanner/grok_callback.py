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
MAX_CALLBACK_BYTES = 2 * 1024 * 1024
MAX_REGISTRATION_BYTES = 8 * 1024
MAX_ACTIVE_REQUESTS = 1000
PENDING_RETENTION = timedelta(hours=1)
RESULT_RETENTION = timedelta(days=7)
_STORE_LOCK = threading.Lock()


def valid_request_id(value: str) -> bool:
    return bool(REQUEST_ID_PATTERN.fullmatch(value)) and value not in {".", ".."}


def callback_result_path(root: Path, request_id: str) -> Path:
    if not valid_request_id(request_id):
        raise ValueError("Invalid request_id.")
    return root / f"{request_id}.json"


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


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


def create_callback_registration(root: Path, request_id: str, ca: str) -> str | None:
    if not valid_request_id(request_id) or not ca or len(ca) > 256:
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
        record = {
            "state": "pending",
            "request_id": request_id,
            "ca": ca,
            "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "created_at": datetime.now(UTC).isoformat(),
        }
        return token if _write_record(root, path, record, exclusive=True) else None


def store_callback_result(root: Path, request_id: str, token: str, raw_body: bytes) -> bool:
    if not valid_request_id(request_id) or not token or len(token) > 256:
        return False
    if len(raw_body) < 1 or len(raw_body) > MAX_CALLBACK_BYTES:
        return False
    try:
        result = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(result, dict):
        return False
    if result.get("type") != "meme_narrative" or result.get("request_id") != request_id:
        return False
    if not isinstance(result.get("ca"), str) or not result["ca"].strip():
        return False

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _STORE_LOCK:
        path = callback_result_path(root, request_id)
        record = _read_record(path)
        if record is None or record.get("state") != "pending":
            return False
        supplied_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(str(record.get("token_sha256", "")), supplied_hash):
            return False
        if result["ca"] != record.get("ca"):
            return False
        created_at = datetime.fromisoformat(str(record.get("created_at", "")))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if created_at + PENDING_RETENTION < datetime.now(UTC):
            path.unlink(missing_ok=True)
            return False
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
    return True


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
        try:
            created = datetime.fromisoformat(str(record.get("received_at") or record.get("created_at") or ""))
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        retention = RESULT_RETENTION if record.get("state") == "complete" else PENDING_RETENTION
        if created + retention < current:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
