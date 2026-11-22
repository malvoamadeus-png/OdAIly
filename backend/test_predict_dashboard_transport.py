import gzip
import json
from http import HTTPStatus
from io import BytesIO
from types import SimpleNamespace

from packages.editor_plugin_api import EditorPluginApiHandler


def test_predict_dashboard_compresses_large_response_without_changing_data():
    class Handler:
        path = "/console/predict-html/get"
        headers = {"Accept-Encoding": "gzip"}
        server = SimpleNamespace(service=SimpleNamespace(api_settings=SimpleNamespace(cors_allow_origin="*")))

        def __init__(self):
            self.wfile = BytesIO()
            self.sent = {}

        def send_response(self, status): pass
        def send_header(self, name, value): self.sent[name] = value
        def end_headers(self): pass
        def _json_default(self, value): return str(value)
        def _send_common_headers(self): EditorPluginApiHandler._send_common_headers(self)

    payload = {"ok": True, "data": {"items": [{"detail_json": {"description": "市场规则" * 10000}}]}}
    handler = Handler()
    EditorPluginApiHandler._send_json(handler, HTTPStatus.OK, payload)
    assert handler.sent.get("Content-Encoding") == "gzip"
    assert len(handler.wfile.getvalue()) < 5000
    assert json.loads(gzip.decompress(handler.wfile.getvalue())) == payload
