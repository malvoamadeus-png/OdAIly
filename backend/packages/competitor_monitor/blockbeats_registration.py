from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import string
import time
from dataclasses import dataclass
from typing import Any

import requests


MAIL_API = "https://api.mail.tm"
BLOCKBEATS_API = "https://api.blockbeats.cn"
BLOCKBEATS_PRO_API = "https://api-pro.theblockbeats.info"
APP_KEY = "bb_demo_app"
APP_SECRET = "bb_demo_secret_2026_01"
ALPHANUMERIC = string.ascii_letters + string.digits


class BlockbeatsRegistrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BlockbeatsRegistrationResult:
    api_key: str
    api_quota: Any = None
    verified_items: int = 0


@dataclass(frozen=True, slots=True)
class DisposableMailbox:
    address: str
    password: str
    token: str


def register_blockbeats_key(
    *,
    verification_timeout_seconds: int = 120,
    request_timeout_seconds: float = 30.0,
    session: requests.Session | None = None,
) -> BlockbeatsRegistrationResult:
    """Create one disposable-mail BlockBeats account and validate its free key."""
    client = session or requests.Session()
    mailbox = _create_disposable_mailbox(client, request_timeout_seconds)
    _blockbeats_request(
        client,
        "POST",
        "/user/email",
        body={"email": mailbox.address},
        timeout_seconds=request_timeout_seconds,
    )
    code = _wait_for_verification_code(
        client,
        mailbox,
        timeout_seconds=verification_timeout_seconds,
        request_timeout_seconds=request_timeout_seconds,
    )

    password = _random_password()
    registration = _blockbeats_request(
        client,
        "POST",
        "/user/register",
        body={
            "email": mailbox.address,
            "code": code,
            "password": password,
            "repassword": password,
        },
        timeout_seconds=request_timeout_seconds,
    )
    if registration.get("code") != 0:
        raise BlockbeatsRegistrationError(f"BlockBeats registration failed: {registration}")

    user_info = (registration.get("data") or {}).get("userInfo") or {}
    user_token = user_info.get("token")
    if not user_token:
        raise BlockbeatsRegistrationError("Registration response did not contain a login token")

    key_response = _blockbeats_request(
        client,
        "GET",
        "/apiDoc/free-api-key",
        token=user_token,
        timeout_seconds=request_timeout_seconds,
    )
    key_data = key_response.get("data") or {}
    api_key = str(key_data.get("api_key") or "").strip()
    if not api_key:
        raise BlockbeatsRegistrationError(f"Free API key was not returned: {key_response}")

    verification = _verify_api_key(client, api_key, request_timeout_seconds)
    return BlockbeatsRegistrationResult(
        api_key=api_key,
        api_quota=key_data.get("total_token"),
        verified_items=len(((verification.get("data") or {}).get("data") or [])),
    )


def _create_disposable_mailbox(session: requests.Session, timeout_seconds: float) -> DisposableMailbox:
    domains = _json_request(session, "GET", f"{MAIL_API}/domains?page=1", timeout_seconds=timeout_seconds)
    if isinstance(domains, dict):
        members = domains.get("hydra:member") or []
    elif isinstance(domains, list):
        members = domains
    else:
        raise BlockbeatsRegistrationError("mail.tm returned an invalid domains payload")
    if not members:
        raise BlockbeatsRegistrationError("mail.tm returned no active domains")

    domain = members[0].get("domain")
    if not domain:
        raise BlockbeatsRegistrationError("mail.tm returned an invalid domain")
    address = f"odaily-{secrets.randbelow(900000) + 100000}@{domain}"
    password = _random_password()
    _json_request(
        session,
        "POST",
        f"{MAIL_API}/accounts",
        body={"address": address, "password": password},
        timeout_seconds=timeout_seconds,
    )
    token_response = _json_request(
        session,
        "POST",
        f"{MAIL_API}/token",
        body={"address": address, "password": password},
        timeout_seconds=timeout_seconds,
    )
    token = str(token_response.get("token") or "")
    if not token:
        raise BlockbeatsRegistrationError("mail.tm did not return a token")
    return DisposableMailbox(address, password, token)


def _wait_for_verification_code(
    session: requests.Session,
    mailbox: DisposableMailbox,
    *,
    timeout_seconds: int,
    request_timeout_seconds: float,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        messages = _json_request(
            session,
            "GET",
            f"{MAIL_API}/messages?page=1",
            headers={"Authorization": f"Bearer {mailbox.token}"},
            timeout_seconds=request_timeout_seconds,
        ).get("hydra:member") or []
        for message in messages:
            sender = (message.get("from") or {}).get("address", "")
            text = " ".join(str(message.get(field) or "") for field in ("subject", "intro"))
            match = re.search(r"\b(\d{6})\b", text)
            if sender == "system@theblockbeats.org" and match:
                return match.group(1)
        time.sleep(3)
    raise BlockbeatsRegistrationError("Timed out waiting for the BlockBeats verification email")


def _blockbeats_request(
    session: requests.Session,
    method: str,
    endpoint: str,
    *,
    body: dict[str, Any] | None = None,
    token: str | None = None,
    timeout_seconds: float,
) -> dict[str, Any]:
    path = f"/v2{endpoint}"
    response = session.request(
        method.upper(),
        f"{BLOCKBEATS_API}{path}",
        json=body,
        headers=_signed_headers(method, path, body=body, token=token),
        timeout=timeout_seconds,
    )
    return _parse_response(response, context=f"BlockBeats {method.upper()} {endpoint}")


def _verify_api_key(session: requests.Session, api_key: str, timeout_seconds: float) -> dict[str, Any]:
    response = session.get(
        f"{BLOCKBEATS_PRO_API}/v1/newsflash",
        params={"page": 1, "size": 1, "lang": "cn"},
        headers={"api-key": api_key, "User-Agent": "Mozilla/5.0"},
        timeout=timeout_seconds,
    )
    payload = _parse_response(response, context="BlockBeats API key verification")
    if payload.get("status") != 0:
        raise BlockbeatsRegistrationError(f"API key verification failed: {payload}")
    return payload


def _json_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: float,
) -> Any:
    response = session.request(
        method.upper(),
        url,
        json=body,
        headers={"Accept": "application/json", **(headers or {})},
        timeout=timeout_seconds,
    )
    return _parse_response(response, context=f"{method.upper()} {url}")


def _parse_response(response: requests.Response, *, context: str) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        raise BlockbeatsRegistrationError(f"{context} returned non-JSON: {response.text[:500]}") from exc
    if response.status_code >= 400:
        raise BlockbeatsRegistrationError(f"{context} failed with HTTP {response.status_code}: {payload}")
    return payload


def _signed_headers(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    token: str | None = None,
) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    nonce = "".join(secrets.choice(ALPHANUMERIC) for _ in range(16))
    body_hash = ""
    if method.upper() != "GET" and body is not None:
        body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        body_hash = hashlib.md5(body_json.encode()).hexdigest()
    signing_text = f"{method.upper()}|{path}|{timestamp}|{nonce}|{body_hash}"
    signature = hmac.new(APP_SECRET.encode(), signing_text.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-App-Key": APP_KEY,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": signature,
        "X-Encrypt": "false",
    }
    if token:
        headers["token"] = token
    return headers


def _random_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))
