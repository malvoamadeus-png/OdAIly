from __future__ import annotations

from typing import Any

from packages.common.config import Writer3Settings
from packages.writer3.confirm_worker import Writer3TelegramConfirmWorker
from packages.writer3.worker import Writer3Worker


class StubWriter3Repository:
    def __init__(self) -> None:
        self.claimed = False
        self.heartbeats: list[dict[str, Any]] = []

    def claim_task(self, **kwargs):  # noqa: ANN003
        self.claimed = True
        return None

    def record_worker_heartbeat(self, **kwargs) -> None:  # noqa: ANN003
        self.heartbeats.append(kwargs)


def test_writer3_worker_disabled_does_not_claim_or_build_clients() -> None:
    repository = StubWriter3Repository()
    worker = Writer3Worker(
        repository=repository,
        index=object(),
        settings=Writer3Settings(enabled=False),
    )

    result = worker.run_once()

    assert result.message == "writer3 disabled"
    assert result.processed == 0
    assert repository.claimed is False
    assert repository.heartbeats[-1]["metadata"]["enabled"] is False


def test_writer3_confirm_worker_disabled_does_not_poll_telegram() -> None:
    worker = Writer3TelegramConfirmWorker(
        index=object(),
        settings=Writer3Settings(enabled=False),
    )

    result = worker.run_once()

    assert result.message == "writer3 disabled"
    assert result.updates == 0
