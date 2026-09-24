from __future__ import annotations

from packages.local_pipeline.server import LocalPipelineService


class _Processor:
    worker_id = "test-worker"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def init_remote_schema(self) -> None:
        self.calls.append("init_remote_schema")

    def start_background_tasks(self) -> None:
        self.calls.append("start_background_tasks")

    def record_heartbeat(self, *, success: bool, error: str | None, metadata: dict | None = None) -> None:
        return None


def test_local_pipeline_initializes_schema_before_starting_workers(monkeypatch) -> None:
    calls: list[str] = []
    processor = _Processor(calls)
    service = LocalPipelineService(queue=object(), processor=processor)  # type: ignore[arg-type]
    monkeypatch.setattr(service, "_ensure_worker_running", lambda: calls.append("ensure_worker"))

    service.start()

    assert calls == ["init_remote_schema", "start_background_tasks", "ensure_worker"]
