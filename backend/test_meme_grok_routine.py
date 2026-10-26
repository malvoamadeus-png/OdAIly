from __future__ import annotations

import json
import threading
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from packages.editor_plugin_api import EditorPluginApiServer
from packages.meme_scanner import grok_callback, grok_routine


def test_load_bearer_headers_reads_grok_txt_format(tmp_path: Path) -> None:
    credentials = tmp_path / "grok.txt"
    credentials.write_text(
        "url=https://automation.example/webhook\nkey=test-api-key\nheader=Authorization: Bearer\n",
        encoding="utf-8",
    )

    assert grok_routine.load_bearer_headers(credentials) == {"Authorization": "Bearer test-api-key"}
    assert grok_routine.load_webhook_url(credentials) == "https://automation.example/webhook"


def test_build_payload_omits_empty_optional_fields_and_checks_callback() -> None:
    payload = grok_routine.build_payload(
        Namespace(
            ca="  AhBidGAgv6QbKSWfrSCegGrzFLcBx4E5FNYTKHNQQj6C ",
            chain="solana",
            token="LAUNCH",
            request_id="job-test",
            callback_url=None,
            notes="  ",
        )
    )

    assert payload == {
        "type": "meme_narrative",
        "request_id": "job-test",
        "ca": "AhBidGAgv6QbKSWfrSCegGrzFLcBx4E5FNYTKHNQQj6C",
        "chain": "solana",
        "token": "LAUNCH",
    }


def test_build_payload_rejects_local_callback_url() -> None:
    args = Namespace(
        ca="mint",
        chain="solana",
        token=None,
        request_id="job-test",
        callback_url="https://localhost/hooks/result",
        notes=None,
    )

    try:
        grok_routine.build_payload(args)
    except ValueError as exc:
        assert "publicly reachable" in str(exc)
    else:
        raise AssertionError("localhost callback URL should be rejected")


def test_callback_store_requires_registration_token_and_matching_ca(tmp_path: Path) -> None:
    ca = "AhBidGAgv6QbKSWfrSCegGrzFLcBx4E5FNYTKHNQQj6C"
    token = grok_callback.create_callback_registration(tmp_path, "job-test", ca)
    assert token is not None
    result = {
        "ok": True,
        "type": "meme_narrative",
        "request_id": "job-test",
        "ca": ca,
        "summary": "Launch opposes Pump.fun fees.",
    }
    raw = json.dumps(result).encode()

    assert not grok_callback.store_callback_result(tmp_path, "job-test", "wrong-token", raw)
    mismatched = {**result, "ca": "different"}
    assert not grok_callback.store_callback_result(tmp_path, "job-test", token, json.dumps(mismatched).encode())
    assert grok_callback.store_callback_result(tmp_path, "job-test", token, raw)
    assert grok_callback.load_callback_result(tmp_path, "job-test", token) == result
    assert grok_callback.load_callback_result(tmp_path, "job-test", "wrong-token") is None
    assert not grok_callback.store_callback_result(tmp_path, "job-test", token, raw)


def test_run_accepts_cursor_webhook_receipt_without_claiming_result(tmp_path, capsys) -> None:
    credentials = tmp_path / "grok.txt"
    credentials.write_text("key=test-api-key\nheader=Authorization: Bearer\n", encoding="utf-8")
    args = Namespace(
        ca="AhBidGAgv6QbKSWfrSCegGrzFLcBx4E5FNYTKHNQQj6C",
        chain="solana",
        token="LAUNCH",
        request_id="job-test",
        callback_url=None,
        notes=None,
        webhook_url="https://automation.example/webhook",
        credentials_file=str(credentials),
        timeout=10,
    )
    response = SimpleNamespace(status_code=200, json=lambda: {"success": True, "runUuid": "run-test"})

    with patch.object(grok_routine.requests, "post", return_value=response) as post:
        assert grok_routine.run(args) == 0

    assert post.call_args.args[0] == args.webhook_url
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer test-api-key"
    assert post.call_args.kwargs["json"]["ca"] == args.ca
    assert post.call_args.kwargs["json"]["request_id"] == "job-test"
    output = capsys.readouterr().out
    assert "runUuid=run-test" in output
    assert "acceptance receipt, not the investigation result" in output


def test_run_waits_for_server_callback_and_saves_result(tmp_path: Path, capsys) -> None:
    credentials = tmp_path / "grok.txt"
    credentials.write_text(
        "url=https://automation.example/webhook\nkey=test-api-key\nheader=Authorization: Bearer\n",
        encoding="utf-8",
    )
    ca = "AhBidGAgv6QbKSWfrSCegGrzFLcBx4E5FNYTKHNQQj6C"
    result = {
        "ok": True,
        "type": "meme_narrative",
        "request_id": "job-test",
        "ca": ca,
        "primary_narrative": "Launch opposes Pump.fun fees.",
    }
    args = Namespace(
        ca=ca,
        chain="solana",
        token="LAUNCH",
        request_id="job-test",
        callback_url=None,
        callback_base_url="https://api.odaily.uk/hooks/grok-meme-result",
        wait_for_result=True,
        result_timeout=1,
        poll_interval=0.01,
        output=str(tmp_path / "result.json"),
        notes=None,
        webhook_url=None,
        credentials_file=str(credentials),
        timeout=10,
    )
    registration = SimpleNamespace(status_code=201, json=lambda: {"ok": True, "token": "t" * 48})
    accepted = SimpleNamespace(status_code=202, json=lambda: {})
    callback = SimpleNamespace(status_code=200, json=lambda: result)

    with (
        patch.object(grok_routine.requests, "post", side_effect=[registration, accepted]) as post,
        patch.object(grok_routine.requests, "get", return_value=callback) as get,
    ):
        assert grok_routine.run(args) == 0

    callback_url = post.call_args_list[1].kwargs["json"]["callback_url"]
    assert callback_url.startswith("https://api.odaily.uk/hooks/grok-meme-result/job-test/")
    assert post.call_args_list[0].args[0].endswith("/job-test/register")
    assert get.call_args.args[0] == callback_url
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) == result
    assert result["primary_narrative"] in capsys.readouterr().out


def test_server_callback_routes_register_receive_and_return_result(tmp_path: Path) -> None:
    ca = "AhBidGAgv6QbKSWfrSCegGrzFLcBx4E5FNYTKHNQQj6C"
    service = SimpleNamespace(
        paths=SimpleNamespace(runtime_dir=tmp_path),
        api_settings=SimpleNamespace(cors_allow_origin="*"),
    )
    server = EditorPluginApiServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/hooks/grok-meme-result"
    request_id = "server-route-test"

    try:
        registration = requests.post(
            f"{base_url}/{request_id}/register",
            json={"type": "meme_narrative", "request_id": request_id, "ca": ca},
            timeout=2,
        )
        assert registration.status_code == 201
        token = registration.json()["token"]
        callback_url = f"{base_url}/{request_id}/{token}"
        result = {
            "ok": True,
            "type": "meme_narrative",
            "request_id": request_id,
            "ca": ca,
            "summary": "Launch argues for fairer launches.",
        }

        posted = requests.post(callback_url, json=result, timeout=2)
        assert posted.status_code == 200
        fetched = requests.get(callback_url, timeout=2)
        assert fetched.status_code == 200
        assert fetched.json() == result
        assert requests.get(f"{base_url}/{request_id}/invalid-token", timeout=2).status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
