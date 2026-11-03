from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

from packages.system_observability import SystemInformationStore
from packages.system_observability.store import _search_terms


ROW_COUNT = 100_000
BASE_TIME_MS = 1_790_000_000_000


def seed(store: SystemInformationStore, count: int = ROW_COUNT) -> None:
    item_sql = """INSERT INTO system_information_items(
        item_id,business_key,source,source_id,task_id,title,summary,source_url,stage,status,result,
        failure_category,created_at_ms,updated_at_ms,observed_at_ms,source_database,source_table,
        source_row_id,evidence_ref,fingerprint,legacy_history
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    with store.connect() as conn:
        item_batch: list[tuple[object, ...]] = []
        term_batch: list[tuple[str, str]] = []
        for index in range(count):
            item_id = f"bench:{index:06d}"
            created = BASE_TIME_MS + index
            business = ("x", "meme", "gate_market", "stock_market")[index % 4]
            status = ("published", "publisher_pending", "write_failed", "discarded")[index % 4]
            result = ("direct_publish", "backoffice", "failed", "stopped")[index % 4]
            title = f"中文区块链市场观察 {index:06d}"
            summary = f"第{index:06d}项多业务性能样例，记录价格变化、风险说明和稿件处理结果。"
            if index % 100 == 0:
                summary += " 长稿件段落：" + ("市场参与者应核对来源、时间和事实归属。" * 12)
            source_id = f"post-{index:06d}"
            source_url = f"https://example.invalid/posts/{index:06d}"
            item_batch.append((
                item_id, business, business, source_id, index, title, summary, source_url, "写作", status,
                result, "program_or_unknown" if result == "failed" else None, created, created, created,
                "offline-benchmark", "items", str(index), None, f"fingerprint-{index}", 0,
            ))
            searchable = " ".join((title, summary, business, source_id, source_url))
            term_batch.extend((term, item_id) for term in _search_terms(searchable))
            if len(item_batch) >= 1000:
                conn.executemany(item_sql, item_batch)
                conn.executemany(
                    "INSERT OR IGNORE INTO system_information_search(term,item_id) VALUES(?,?)",
                    term_batch,
                )
                item_batch.clear()
                term_batch.clear()
        if item_batch:
            conn.executemany(item_sql, item_batch)
            conn.executemany(
                "INSERT OR IGNORE INTO system_information_search(term,item_id) VALUES(?,?)",
                term_batch,
            )
        conn.execute("ANALYZE")
    store.set_state("search_bigrams_rebuild_done", "1")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def measure(query: Callable[[], object], iterations: int = 10) -> dict[str, float]:
    for _ in range(iterations):
        query()
    timings = []
    for _ in range(iterations):
        started = time.perf_counter()
        query()
        timings.append((time.perf_counter() - started) * 1000)
    return {"p50_ms": round(statistics.median(timings), 2), "p95_ms": round(percentile(timings, 0.95), 2)}


def main() -> None:
    with TemporaryDirectory(prefix="odaily-information-benchmark-") as directory:
        store = SystemInformationStore(Path(directory) / "system_information.sqlite")
        store.initialize()
        started = time.perf_counter()
        seed(store)
        seed_seconds = time.perf_counter() - started

        first_keyword_page = store.query({"q": "区块链", "limit": 100})
        next_cursor = first_keyword_page["next_cursor"]
        if next_cursor is None:
            raise RuntimeError("性能夹具未生成可跨越的关键词分页")
        queries = {
            "first_page": lambda: store.query({"limit": 100}),
            "keyword_first_page": lambda: store.query({"q": "区块链", "limit": 100}),
            "keyword_next_page": lambda: store.query({"q": "区块链", "limit": 100, "cursor": next_cursor}),
            "status_filter": lambda: store.query({"status": "direct_publish", "limit": 100}),
            "deep_pagination": lambda: store.query({"limit": 100, "cursor": {"time": BASE_TIME_MS + 10_000, "id": "bench:010000"}}),
        }
        results = {name: measure(query) for name, query in queries.items()}
        passed = all(value["p95_ms"] <= 1000 for value in results.values())
        print(json.dumps({
            "rows": ROW_COUNT, "seed_seconds": round(seed_seconds, 2), "iterations_per_query": 10,
            "query_results": results, "p95_target_ms": 1000, "passed": passed,
        }, ensure_ascii=False, indent=2))
        if not passed:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
