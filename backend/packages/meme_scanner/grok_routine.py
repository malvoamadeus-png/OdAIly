from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv


WEBHOOK_URL_ENV = "GROK_ROUTINE_WEBHOOK_URL"
DEFAULT_CALLBACK_BASE_URL = "https://api.odaily.uk/hooks/grok-meme-result"


def validate_callback_url(value: str | None) -> str | None:
    if value is None:
        return None
    url = value.strip()
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("--callback-url must be a public HTTPS URL.")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("--callback-url must be publicly reachable; localhost cannot receive callbacks.")
    try:
        address = ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and address.is_loopback:
        raise ValueError("--callback-url must be publicly reachable; loopback addresses cannot receive callbacks.")
    return url


def _load_credentials(credentials_file: Path) -> dict[str, str]:
    if not credentials_file.is_file():
        raise RuntimeError(f"Missing Grok webhook credentials file: {credentials_file}")
    values: dict[str, str] = {}
    for raw_line in credentials_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip().lower()] = value.strip().strip('"\'')

    return values


def load_bearer_headers(credentials_file: Path) -> dict[str, str]:
    values = _load_credentials(credentials_file)
    key = values.get("key", "")
    header = values.get("header", "")
    if not key or ":" not in header:
        raise RuntimeError("grok.txt must contain key=... and header=Authorization: Bearer.")
    header_name, header_value = header.split(":", 1)
    parts = header_value.strip().split()
    if header_name.strip().lower() != "authorization" or len(parts) != 1 or parts[0].lower() != "bearer":
        raise RuntimeError("grok.txt must specify an Authorization: Bearer header.")
    return {header_name.strip(): f"{parts[0]} {key}"}


def load_webhook_url(credentials_file: Path) -> str | None:
    return _load_credentials(credentials_file).get("url") or None


def _resolve_request_id(value: str | None) -> str:
    request_id = value or str(uuid.uuid4())
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", request_id) or request_id in {".", ".."}:
        raise ValueError("request_id must use 1-128 letters, digits, dots, underscores, or hyphens.")
    return request_id


def build_payload(args: argparse.Namespace, callback_url: str | None = None) -> dict[str, Any]:
    ca = args.ca.strip()
    if not ca:
        raise ValueError("--ca cannot be empty.")
    chain = args.chain.strip() if args.chain else "solana"
    if not chain:
        raise ValueError("--chain cannot be empty.")
    payload: dict[str, Any] = {
        "type": "meme_narrative",
        "request_id": _resolve_request_id(args.request_id),
        "ca": ca,
        "chain": chain,
    }
    for key in ("token", "notes"):
        value = getattr(args, key, None)
        if value and value.strip():
            payload[key] = value.strip()
    resolved_callback_url = validate_callback_url(
        callback_url if callback_url is not None else getattr(args, "callback_url", None)
    )
    if resolved_callback_url:
        payload["callback_url"] = resolved_callback_url
    return payload


def _save_result(result: dict[str, Any], output: str | None) -> Path:
    if output:
        path = Path(output)
    else:
        path = Path(__file__).resolve().parents[3] / "data" / "exports" / "meme_grok_results" / (
            f"{_resolve_request_id(str(result['request_id']))}.json"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _poll_result(url: str, *, request_id: str, timeout: int, interval: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = requests.get(url, timeout=min(10, max(1, interval)), allow_redirects=False)
        except requests.RequestException:
            response = None
        if response is not None and response.status_code == 200:
            try:
                result = response.json()
            except ValueError as exc:
                raise RuntimeError("The Grok callback endpoint returned invalid JSON.") from exc
            if isinstance(result, dict) and result.get("request_id") == request_id:
                return result
            raise RuntimeError("The Grok callback endpoint returned a different request_id.")
        time.sleep(interval)
    return None


def run(args: argparse.Namespace) -> int:
    load_dotenv()
    credentials_file = Path(args.credentials_file)
    headers = load_bearer_headers(credentials_file)
    headers["Content-Type"] = "application/json"
    webhook_url = (
        getattr(args, "webhook_url", None)
        or load_webhook_url(credentials_file)
        or os.environ.get(WEBHOOK_URL_ENV)
    ).strip()
    if not webhook_url:
        raise RuntimeError(f"Set url=... in {credentials_file} or provide --webhook-url.")
    parsed_webhook_url = urlsplit(webhook_url)
    if parsed_webhook_url.scheme != "https" or not parsed_webhook_url.hostname:
        raise RuntimeError("Grok Routine Webhook URL must use HTTPS.")

    request_id = _resolve_request_id(getattr(args, "request_id", None))
    args.request_id = request_id
    wait_for_result = bool(getattr(args, "wait_for_result", False))
    if wait_for_result and getattr(args, "callback_url", None):
        raise ValueError("--wait-for-result manages its own callback URL; omit --callback-url.")
    if wait_for_result and (args.result_timeout < 1 or args.poll_interval <= 0):
        raise ValueError("--result-timeout and --poll-interval must be greater than zero.")
    callback_url = None
    if wait_for_result:
        callback_base = (
            getattr(args, "callback_base_url", None)
            or os.environ.get("GROK_CALLBACK_BASE_URL")
            or DEFAULT_CALLBACK_BASE_URL
        ).rstrip("/")
        callback_base = validate_callback_url(callback_base)
        registration_url = f"{callback_base}/{request_id}/register"
        try:
            registration = requests.post(
                registration_url,
                json={"type": "meme_narrative", "request_id": request_id, "ca": args.ca.strip()},
                headers={"Content-Type": "application/json"},
                timeout=args.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not register Grok callback ({exc.__class__.__name__}).") from exc
        try:
            registration_body = registration.json()
        except ValueError:
            registration_body = {}
        if registration.status_code != 201 or not isinstance(registration_body, dict):
            raise RuntimeError(f"Could not register Grok callback (HTTP {registration.status_code}).")
        callback_token = registration_body.get("token")
        if not isinstance(callback_token, str) or len(callback_token) < 32:
            raise RuntimeError("The callback server returned an invalid callback token.")
        callback_url = validate_callback_url(f"{callback_base}/{request_id}/{callback_token}")
    elif getattr(args, "callback_url", None):
        callback_url = args.callback_url

    payload = build_payload(args, callback_url=callback_url)
    try:
        response = requests.post(
            webhook_url,
            json=payload,
            headers=headers,
            timeout=args.timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Grok Routine Webhook request failed ({exc.__class__.__name__}).") from exc

    print(f"HTTP status: {response.status_code}")
    receipt: dict[str, Any] = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            receipt = parsed
    except ValueError:
        pass
    accepted = response.status_code == 202 or (
        200 <= response.status_code < 300 and receipt.get("success") is True
    )
    if not accepted:
        print("Grok Routine Webhook did not accept the request.")
        return 1
    print(f"Grok Bot accepted request_id={payload['request_id']} for CA {payload['ca']}.")
    if receipt.get("runUuid"):
        print(f"runUuid={receipt['runUuid']}")
    if not wait_for_result:
        if payload.get("callback_url"):
            print("The result will be sent to the supplied callback URL when the investigation finishes.")
        else:
            print("This is an acceptance receipt, not the investigation result; check the Grok Bot chat.")
        return 0

    print(f"Waiting up to {args.result_timeout} seconds for the Grok Bot result...")
    result = _poll_result(
        callback_url,
        request_id=request_id,
        timeout=args.result_timeout,
        interval=args.poll_interval,
    )
    if result is None:
        print(f"Timed out waiting for Grok Bot result request_id={request_id}.")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Result saved to {_save_result(result, getattr(args, 'output', None))}")
    return 0 if result.get("ok") is not False else 1
