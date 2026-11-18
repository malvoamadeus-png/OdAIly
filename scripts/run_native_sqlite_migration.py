#!/usr/bin/env python3
"""Run the native SQLite migration in a fixed, resumable order.

This orchestrator never deletes or rewrites a source SQLite file. Each source
is preflighted and imported through the native importer, whose PostgreSQL
checkpoint makes a failed source safe to retry. Reports are written directly
under /data so large source files are never copied to the root filesystem.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SOURCE_ORDER = (
    "system_information", "small_evidence", "main", "business_news", "gate_market", "stock_market", "predict_market_discovery",
    "predict_html_discovery", "macro_newsflash", "editor_plugin_local", "ai_text_self_upgrade",
    "msx_notice", "pipeline_timing", "hottopic", "local_pipeline", "meme_scanner", "writer3",
    "searcher", "ai_text_self_upgrade_observability", "hyperliquid",
    "ai_review",
)
DATA_ROOT = Path("/data")

DEFAULT_PATHS = {
    "main": "/opt/OdAIly/data/database/odaily.sqlite",
    "ai_review": "/opt/OdAIly/data/runtime/ai_review.sqlite",
    "business_news": "/opt/OdAIly/data/runtime/business_news.sqlite",
    "gate_market": "/opt/OdAIly/data/runtime/gate_market.sqlite",
    "stock_market": "/opt/OdAIly/data/runtime/stock_market.sqlite",
    "predict_market_discovery": "/opt/OdAIly/data/runtime/predict_market_discovery.sqlite",
    "predict_html_discovery": "/opt/OdAIly/data/runtime/predict_html_discovery.sqlite",
    "macro_newsflash": "/opt/OdAIly/data/runtime/macro_newsflash.sqlite",
    "editor_plugin_local": "/opt/OdAIly/data/runtime/editor_plugin_local.sqlite",
    "ai_text_self_upgrade": "/opt/OdAIly/data/runtime/ai_text_self_upgrade.sqlite",
    "msx_notice": "/opt/OdAIly/data/runtime/msx_notice.sqlite",
    "pipeline_timing": "/opt/OdAIly/data/runtime/pipeline_timing.sqlite",
    "hottopic": "/opt/OdAIly/data/runtime/hottopic.sqlite",
    "local_pipeline": "/opt/OdAIly/data/runtime/local_pipeline.sqlite",
    "meme_scanner": "/opt/OdAIly/data/processed/meme_scanner.sqlite3",
    "writer3": "/opt/OdAIly/data/processed/writer3/writer3.sqlite",
    "searcher": "/opt/OdAIly/data/processed/searcher/searcher.sqlite",
    "system_information": "/data/OdAIly/system-observability/system_information.sqlite",
    "ai_text_self_upgrade_observability": "/data/OdAIly/system-observability/ai_text_self_upgrade.sqlite",
    "small_evidence": "/data/OdAIly/system-observability/system_information/small_evidence.sqlite",
    "hyperliquid": "/data/OdAIly/hyperliquid/hyperliquid.sqlite",
}


def _report_path(root: Path, source_key: str) -> Path:
    root = root.resolve()
    if root != DATA_ROOT and DATA_ROOT not in root.parents:
        raise ValueError("report directory must be under /data")
    return root / f"{source_key}.json"


@contextmanager
def _sqlite_temp_environment(path: Path):
    previous = os.environ.get("ODAILY_SQLITE_TMPDIR")
    os.environ["ODAILY_SQLITE_TMPDIR"] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("ODAILY_SQLITE_TMPDIR", None)
        else:
            os.environ["ODAILY_SQLITE_TMPDIR"] = previous


def run(*, report_dir: Path, confirm_frozen: bool, paths: dict[str, Path], selected: tuple[str, ...] = SOURCE_ORDER) -> dict[str, Any]:
    if not confirm_frozen:
        raise RuntimeError("refusing to start: pass --confirm-frozen after manually stopping all SQLite writers")
    if not selected:
        raise ValueError("no source keys selected")
    unknown = sorted(set(selected) - set(SOURCE_ORDER))
    if unknown:
        raise ValueError(f"unknown source keys: {unknown}")
    missing = sorted(set(selected) - set(paths))
    if missing:
        raise ValueError(f"source paths are missing: {missing}")
    resolved_paths: dict[Path, str] = {}
    for key in selected:
        resolved = paths[key].expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"source file for {key} does not exist: {resolved}")
        previous = resolved_paths.get(resolved)
        if previous is not None:
            raise ValueError(f"physical SQLite path is selected more than once: {previous} and {key}: {resolved}")
        resolved_paths[resolved] = key
    from packages.common.native_sqlite_import import import_native_sqlite_source, initialize_native_schemas, preflight_native_sqlite_source

    report_dir = report_dir.resolve()
    _report_path(report_dir, "run")
    report_dir.mkdir(parents=True, exist_ok=True)
    configured_tmp = Path(os.getenv("ODAILY_SQLITE_TMPDIR") or report_dir).expanduser().resolve()
    if configured_tmp != DATA_ROOT and DATA_ROOT not in configured_tmp.parents:
        raise ValueError("SQLite temp directory must be under /data")
    configured_tmp.mkdir(parents=True, exist_ok=True)
    with _sqlite_temp_environment(configured_tmp):
        return _run_sources(selected=selected, paths=paths, report_dir=report_dir,
                            import_native_sqlite_source=import_native_sqlite_source,
                            initialize_native_schemas=initialize_native_schemas,
                            preflight_native_sqlite_source=preflight_native_sqlite_source)


def _run_sources(*, selected: tuple[str, ...], paths: dict[str, Path], report_dir: Path,
                 import_native_sqlite_source, initialize_native_schemas, preflight_native_sqlite_source) -> dict[str, Any]:
    initialize_native_schemas()
    run_report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(), "source_order": list(selected), "search_postings": "ignored",
        "status": "running", "sources": [],
    }
    for source_key in selected:
        source_path = paths[source_key]
        entry: dict[str, Any] = {"source_key": source_key, "source_path": str(source_path), "status": "preflight"}
        try:
            preflight = preflight_native_sqlite_source(source_key=source_key, source_path=source_path)
            entry["preflight"] = preflight
            if not preflight["ready_for_import"]:
                raise RuntimeError(f"preflight rejected {source_key}: unknown={preflight['unknown_tables']}")
            entry["status"] = "importing"
            entry["import"] = import_native_sqlite_source(source_key=source_key, source_path=source_path)
            entry["status"] = "complete"
            entry["source_file_group_release"] = {
                "eligible_for_owner_review": True,
                "automatic_deletion": False,
                "proof": "all mapped tables imported and row/hash verified; importer foreign-key audits completed",
                "required_before_release": [
                    "owner confirms the report matches the frozen physical SQLite file",
                    "owner verifies whole-file integrity and no remaining WAL/SHM writer or reader dependency",
                    "owner completes cross-source association checks, including external evidence references",
                    "owner confirms PostgreSQL and filesystem free-space/WAL headroom after reclaim",
                ],
            }
        except Exception as exc:
            entry.update({"status": "failed", "error_type": type(exc).__name__, "error": str(exc)[:4000], "retryable": True})
            run_report["sources"].append(entry)
            run_report.update({"status": "failed", "failed_source": source_key, "finished_at": datetime.now(UTC).isoformat()})
            _write_report(_report_path(report_dir, "run"), run_report)
            _write_report(_report_path(report_dir, source_key), entry)
            raise
        run_report["sources"].append(entry)
        _write_report(_report_path(report_dir, source_key), entry)
    run_report.update({"status": "complete", "finished_at": datetime.now(UTC).isoformat()})
    _write_report(_report_path(report_dir, "run"), run_report)
    return run_report


def _write_report(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-frozen", action="store_true", help="confirm all SQLite writers were stopped manually")
    parser.add_argument("--report-dir", type=Path, default=Path("/data/OdAIly/migration-reports"))
    parser.add_argument("--source", action="append", choices=SOURCE_ORDER, dest="sources")
    parser.add_argument("--path", action="append", metavar="SOURCE_KEY=PATH", help="override one manifest source path")
    args = parser.parse_args(argv)
    paths = {key: Path(value) for key, value in DEFAULT_PATHS.items()}
    for override in args.path or []:
        key, separator, value = override.partition("=")
        if not separator or key not in paths or not value:
            parser.error(f"invalid --path {override!r}; expected SOURCE_KEY=PATH")
        paths[key] = Path(value)
    selected = tuple(args.sources or SOURCE_ORDER)
    try:
        run(report_dir=args.report_dir, confirm_frozen=args.confirm_frozen, paths=paths, selected=selected)
    except Exception as exc:
        print(f"migration stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
