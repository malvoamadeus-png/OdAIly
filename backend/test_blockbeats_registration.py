from __future__ import annotations

from packages.competitor_monitor.blockbeats_registration import register_blockbeats_key


class _Response:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/domains?page=1"):
            return _Response({"hydra:member": [{"domain": "example.test"}]})
        if url.endswith("/accounts"):
            return _Response({})
        if url.endswith("/token"):
            return _Response({"token": "mail-token"})
        if url.endswith("/messages?page=1"):
            return _Response(
                {
                    "hydra:member": [
                        {
                            "from": {"address": "system@theblockbeats.org"},
                            "subject": "验证码 123456",
                            "intro": "",
                        }
                    ]
                }
            )
        if url.endswith("/v2/user/email"):
            return _Response({"code": 0})
        if url.endswith("/v2/user/register"):
            return _Response({"code": 0, "data": {"userInfo": {"token": "blockbeats-token"}}})
        if url.endswith("/v2/apiDoc/free-api-key"):
            return _Response({"data": {"api_key": "new-key", "total_token": 100}})
        raise AssertionError(f"unexpected request: {method} {url}")

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        assert url.endswith("/v1/newsflash")
        assert kwargs["headers"]["api-key"] == "new-key"
        return _Response({"status": 0, "data": {"data": [{"id": 1}]}})


class _ListDomainSession(_Session):
    def request(self, method, url, **kwargs):
        if url.endswith("/domains?page=1"):
            return _Response([{"domain": "example.test"}])
        return super().request(method, url, **kwargs)


def test_register_blockbeats_key_runs_registration_and_verification_flow():
    session = _Session()

    result = register_blockbeats_key(
        verification_timeout_seconds=1,
        request_timeout_seconds=1,
        session=session,
    )

    assert result.api_key == "new-key"
    assert result.api_quota == 100
    assert result.verified_items == 1
    blockbeats_calls = [call for call in session.calls if "api.blockbeats.cn" in call[1]]
    assert [call[0] for call in blockbeats_calls] == ["POST", "POST", "GET"]
    assert all("X-Signature" in call[2]["headers"] for call in blockbeats_calls)


def test_register_blockbeats_key_accepts_list_domain_payload():
    result = register_blockbeats_key(
        verification_timeout_seconds=1,
        request_timeout_seconds=1,
        session=_ListDomainSession(),
    )

    assert result.api_key == "new-key"
