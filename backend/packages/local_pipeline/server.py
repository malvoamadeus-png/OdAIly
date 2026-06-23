from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from packages.common.config import load_external_media_alert_settings, load_x_processing_settings
from packages.common.paths import ensure_runtime_dirs, get_paths

from .processor import LocalPipelineProcessor
from .queue import LocalPipelineQueue


class LocalPipelineService:
    def __init__(self, *, queue: LocalPipelineQueue, processor: LocalPipelineProcessor) -> None:
        self.queue = queue
        self.processor = processor
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._worker_thread = threading.Thread(target=self._run_worker, name="local-pipeline-worker", daemon=True)

    def start(self) -> None:
        self._worker_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        self._worker_thread.join(timeout=5)

    def enqueue(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_type = str(payload.get("job_type") or "").strip()
        source = str(payload.get("source") or "").strip()
        source_item_id = str(payload.get("source_item_id") or "").strip()
        try:
            task_id = int(payload.get("task_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("task_id is required") from exc
        if job_type not in {"write_flow", "alert_only"}:
            raise ValueError("job_type must be write_flow or alert_only")
        if not source or not source_item_id:
            raise ValueError("source and source_item_id are required")
        job = self.queue.enqueue(
            job_type=job_type,  # type: ignore[arg-type]
            task_id=task_id,
            source=source,
            source_item_id=source_item_id,
            payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
        )
        self._wake_event.set()
        return {"id": job.id, "status": job.status}

    def health(self) -> dict[str, Any]:
        return {"ok": True, "queue": self.queue.stats()}

    def _run_worker(self) -> None:
        print("[odaily] local pipeline worker started")
        while not self._stop_event.is_set():
            job = self.queue.claim_next(worker_id=self.processor.worker_id)
            if job is None:
                self.processor.record_heartbeat(success=True, error=None, metadata={"idle": True, "queue": self.queue.stats()})
                self._wake_event.wait(5)
                self._wake_event.clear()
                continue
            try:
                result = self.processor.process(job)
            except Exception as exc:
                self.queue.mark_failed(job.id, error=str(exc), attempt_count=job.attempt_count)
                self.processor.record_heartbeat(
                    success=False,
                    error=str(exc),
                    metadata={"job_id": job.id, "task_id": job.task_id, "job_type": job.job_type},
                )
                print(f"[odaily] local pipeline failed job_id={job.id} task_id={job.task_id} error={exc}")
                continue
            self.queue.mark_succeeded(job.id)
            self.processor.record_heartbeat(
                success=True,
                error=None,
                metadata={
                    "job_id": job.id,
                    "task_id": result.task_id,
                    "job_type": job.job_type,
                    "task_status": result.status,
                    "message": result.message,
                },
            )
            print(
                "[odaily] local pipeline completed "
                f"job_id={job.id} task_id={result.task_id} status={result.status} message={result.message}"
            )


class LocalPipelineHTTPServer(ThreadingHTTPServer):
    service: LocalPipelineService


class LocalPipelineHandler(BaseHTTPRequestHandler):
    server: LocalPipelineHTTPServer

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        self._send_json(self.server.service.health())

    def do_POST(self) -> None:
        if self.path != "/pipeline/jobs":
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json()
            result = self.server.service.enqueue(payload)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(result, status=HTTPStatus.CREATED)

    def log_message(self, format: str, *args: Any) -> None:
        print("[odaily] local-pipeline " + format % args)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _send_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def run_local_pipeline_server(
    *,
    database_url: str | None,
    host: str = "127.0.0.1",
    port: int = 8776,
    queue_path: Path | None = None,
) -> None:
    paths = get_paths()
    ensure_runtime_dirs(paths)
    queue = LocalPipelineQueue(queue_path or paths.runtime_dir / "local_pipeline.sqlite")
    processor = LocalPipelineProcessor(
        database_url=database_url,
        x_settings=load_x_processing_settings(),
        alert_settings=load_external_media_alert_settings(),
    )
    service = LocalPipelineService(queue=queue, processor=processor)
    service.start()
    server = LocalPipelineHTTPServer((host, port), LocalPipelineHandler)
    server.service = service
    print(f"[odaily] local pipeline server listening on {host}:{port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.stop()
