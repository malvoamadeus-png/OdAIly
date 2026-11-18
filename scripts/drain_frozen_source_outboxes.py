#!/usr/bin/env python3
"""Drain mapped SQLite source outboxes after operators stop every source writer."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def parse_sources(values: list[str]) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    for value in values:
        key, separator, raw_path = value.partition("=")
        if not separator or not key or not raw_path:
            raise ValueError(f"invalid --source {value!r}; expected DATABASE_KEY=PATH")
        if key in sources:
            raise ValueError(f"duplicate source database key: {key}")
        sources[key] = Path(raw_path).expanduser()
    if not sources:
        raise ValueError("provide every configured source database with --source DATABASE_KEY=PATH")
    return sources


def run(*, sources: dict[str, Path], observability_dir: Path, source_writes_stopped: bool,
        max_batches_per_source: int = 1000) -> dict:
    if not source_writes_stopped:
        raise RuntimeError("confirm --source-writes-stopped after stopping every listed SQLite writer")
    from packages.system_observability.native_import_cutover import run_frozen_cutover_drain
    from packages.system_observability.runtime import create_system_observability_runtime
    from packages.common.storage import load_storage_settings

    if load_storage_settings().backend != "sqlite":
        raise RuntimeError("frozen source outbox drain requires ODAILY_STORAGE_BACKEND=sqlite")

    primary = sources.get("main")
    if primary is None:
        raise ValueError("source list must include main=PATH")
    additional = {key: path for key, path in sources.items() if key != "main"}
    runtime = create_system_observability_runtime(
        primary_database_path=primary,
        system_observability_dir=observability_dir,
        additional_databases=additional,
        initialize=True,
    )
    return run_frozen_cutover_drain(
        runtime.projector,
        sources=sources,
        source_writes_stopped=source_writes_stopped,
        max_batches_per_source=max_batches_per_source,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", default=[], metavar="DATABASE_KEY=PATH",
                        help="one configured SQLite source; repeat for every configured source")
    parser.add_argument("--source-writes-stopped", action="store_true",
                        help="confirm all writers for the listed source databases have stopped")
    parser.add_argument("--observability-dir", type=Path, required=True,
                        help="existing target observability store directory; this is not a source copy")
    parser.add_argument("--max-batches-per-source", type=int, default=1000)
    args = parser.parse_args(argv)
    try:
        report = run(
            sources=parse_sources(args.source),
            observability_dir=args.observability_dir,
            source_writes_stopped=args.source_writes_stopped,
            max_batches_per_source=args.max_batches_per_source,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0 if report["complete"] else 2
    except Exception as exc:
        print(json.dumps({"complete": False, "error_type": type(exc).__name__, "error": str(exc)},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
