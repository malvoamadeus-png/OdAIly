from __future__ import annotations

from pathlib import Path
from typing import Any


def drain_frozen_source_outboxes(
    projector: Any, *, targets: dict[str, int], max_batches_per_source: int = 1000,
) -> dict[str, Any]:
    """Drain caller-captured source watermarks without source cleanup or trigger changes."""
    if not targets:
        raise ValueError("at least one frozen source target is required")
    results: dict[str, Any] = {}
    for database_key, sequence in targets.items():
        results[database_key] = projector.drain_source_outbox(
            database_key, target_sequence=int(sequence), max_batches=max_batches_per_source,
        )
    return {"sources": results, "complete": all(bool(item["complete"]) for item in results.values())}


def run_frozen_cutover_drain(
    projector: Any, *, sources: dict[str, Path], source_writes_stopped: bool,
    max_batches_per_source: int = 1000,
) -> dict[str, Any]:
    """Capture final outbox maxima and drain them after an operator confirms all writers stopped."""
    if not source_writes_stopped:
        raise RuntimeError("all SQLite source writers must be stopped before capturing cutover watermarks")
    readiness = inspect_frozen_sources(sources, projector=projector)
    if not readiness["ready"]:
        raise RuntimeError("source outbox readiness checks failed")
    targets = {key: int(value["outbox_max_sequence"]) for key, value in readiness["sources"].items()}
    drained = drain_frozen_source_outboxes(
        projector, targets=targets, max_batches_per_source=max_batches_per_source,
    )
    return {"readiness": readiness, "targets": targets, "drain": drained,
            "complete": bool(drained["complete"])}


def inspect_frozen_sources(
    sources: dict[str, Path], *, projector: Any,
) -> dict[str, Any]:
    """Read-only readiness report for a frozen set of SQLite outboxes."""
    import sqlite3
    from .projector import SOURCE_AUDIT_TRIGGER_VERSION

    if not sources:
        raise ValueError("at least one configured source database is required")
    report = {}
    for database_key, path in sources.items():
        path = Path(path).expanduser().resolve()
        specs = [spec for spec in projector.table_specs if spec.database_key == database_key]
        item: dict[str, Any] = {"path": str(path), "mapped_tables": len(specs), "exists": path.is_file()}
        if not specs:
            item.update({"ready": False, "reason": "source database has no mapped outbox tables"})
            report[database_key] = item
            continue
        if not path.is_file():
            item.update({"ready": False, "reason": "source database missing"})
            report[database_key] = item
            continue
        try:
            uri = f"file:{path}?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                conn.row_factory = sqlite3.Row
                outbox = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='odaily_observability_outbox'").fetchone()
                meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='odaily_observability_meta'").fetchone()
                if not outbox or not meta:
                    item.update({"ready": False, "reason": "source audit schema missing"})
                else:
                    mapped = {spec.table for spec in specs}
                    maximum = int(conn.execute("SELECT COALESCE(MAX(seq),0) AS n FROM odaily_observability_outbox").fetchone()["n"])
                    watermark = int(projector.store.get_state(f"source_audit_{database_key}_watermark", "0") or 0)
                    unknown = sorted({str(row[0]) for row in conn.execute(
                        "SELECT DISTINCT table_name FROM odaily_observability_outbox WHERE seq>? AND seq<=?",
                        (watermark, maximum),
                    ).fetchall() if str(row[0]) not in mapped})
                    version = conn.execute("SELECT meta_value FROM odaily_observability_meta WHERE meta_key=?",
                                            (f"source_audit_trigger_version:{database_key}",)).fetchone()
                    trigger_version = str(version["meta_value"]) if version else None
                    item.update({"outbox_max_sequence": maximum, "projected_watermark": watermark,
                                 "trigger_version": trigger_version,
                                 "unknown_outbox_tables": unknown,
                                 "ready": not unknown and trigger_version == str(SOURCE_AUDIT_TRIGGER_VERSION),
                                 "pending_at_snapshot": max(0, maximum - watermark)})
                    if unknown:
                        item["reason"] = f"unmapped outbox tables: {unknown}"
                    elif trigger_version != str(SOURCE_AUDIT_TRIGGER_VERSION):
                        item["reason"] = "source audit trigger version mismatch"
        except sqlite3.Error as exc:
            item.update({"ready": False, "reason": f"{type(exc).__name__}: {exc}"})
        report[database_key] = item
    return {"sources": report, "ready": bool(report) and all(item.get("ready") for item in report.values())}
