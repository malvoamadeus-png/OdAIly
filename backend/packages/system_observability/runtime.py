from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.common.storage import load_storage_settings


def system_observability_directory(default_runtime_dir: Path, configured_dir: str | None = None) -> Path:
    value = (configured_dir if configured_dir is not None else os.getenv("ODAILY_SYSTEM_OBSERVABILITY_DIR", "")).strip()
    return Path(value).expanduser() if value else default_runtime_dir


@dataclass(frozen=True, slots=True)
class SystemObservabilityRuntime:
    information_store: Any
    projector: Any
    review_repository: Any
    review_service: Any


def create_system_observability_runtime(
    *,
    primary_database_path: Path,
    system_observability_dir: Path,
    additional_databases: dict[str, Path] | None = None,
    table_specs: tuple[Any, ...] | None = None,
    model_settings: Any = None,
    initialize: bool = True,
    postgres_schema: str = "system_observability",
) -> SystemObservabilityRuntime:
    """Build the full observability stack for the selected local storage backend."""
    settings = load_storage_settings()
    directory = system_observability_dir.expanduser()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if settings.backend == "postgres":
        from .postgres_projector import PostgresSourceStateProjector
        from .postgres_reviews import PostgresSystemReviewRepository, PostgresSystemReviewService
        from .postgres_store import PostgresSystemInformationStore

        information_store = PostgresSystemInformationStore(evidence_dir=directory / "system_information" / "evidence", schema=postgres_schema)
        review_repository = PostgresSystemReviewRepository(information_store=information_store, schema=postgres_schema)
        projector = PostgresSourceStateProjector(information_store, table_specs=table_specs or _default_table_specs())
        review_service = PostgresSystemReviewService(review_repository, model_settings=model_settings)
    else:
        from .projector import MainTaskProjector, table_specs_for_business_path
        from .reviews import SystemReviewRepository, SystemReviewService
        from .store import SystemInformationStore

        information_store = SystemInformationStore(directory / "system_information.sqlite")
        review_repository = SystemReviewRepository(directory / "system_information.sqlite")
        specs = table_specs or table_specs_for_business_path(separate=bool(additional_databases and "business" in additional_databases))
        projector = MainTaskProjector(primary_database_path, information_store,
                                      additional_databases=additional_databases or {}, table_specs=specs)
        review_service = SystemReviewService(primary_database_path, review_repository, model_settings=model_settings,
                                             competitor_database_path=(additional_databases or {}).get("business", primary_database_path),
                                             evidence_database_path=primary_database_path,
                                             local_pipeline_database_path=(additional_databases or {}).get("queue"))
    if initialize:
        information_store.initialize()
        review_repository.initialize()
    return SystemObservabilityRuntime(information_store, projector, review_repository, review_service)


def _default_table_specs() -> tuple[Any, ...]:
    from .projector import TABLE_SPECS

    return TABLE_SPECS
