from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

import requests
from dotenv import load_dotenv


LocalPipelineJobType = Literal["write_flow", "alert_only"]


def get_local_pipeline_url(value: str | None = None) -> str:
    if value:
        return value.rstrip("/")
    load_dotenv()
    return (os.getenv("LOCAL_PIPELINE_URL") or "http://127.0.0.1:8776").rstrip("/")


@dataclass(frozen=True)
class LocalPipelineClient:
    base_url: str | None = None
    timeout_seconds: float = 5.0

    def submit_job(
        self,
        *,
        job_type: LocalPipelineJobType,
        task_id: int,
        source: str,
        source_item_id: str,
    ) -> None:
        response = requests.post(
            f"{get_local_pipeline_url(self.base_url)}/pipeline/jobs",
            json={
                "job_type": job_type,
                "task_id": task_id,
                "source": source,
                "source_item_id": source_item_id,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
