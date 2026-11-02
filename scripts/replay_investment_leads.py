"""Replay already judged public X materials without writing the production database."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from dotenv import load_dotenv
from packages.investment_leads.analysis import (
    DERIVE_PROMPT_VERSION, SCAN_PROMPT_VERSION, InvestmentLeadAnalyzer,
)
from packages.investment_leads.service import InvestmentLeadService, _iso


def dump(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def freeze(source: Path, folder: Path) -> dict:
    target = folder / "input.json"
    if target.exists():
        return json.loads(target.read_text(encoding="utf-8"))
    db = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN")
    materials = [dict(row) for row in db.execute(
        "SELECT m.* FROM investment_lead_materials m WHERE EXISTS("
        "SELECT 1 FROM investment_lead_events e WHERE e.material_id=m.material_id "
        "AND e.content_hash=m.content_hash) ORDER BY m.first_observed_at,m.material_id"
    )]
    events = [dict(row) for row in db.execute(
        "SELECT e.* FROM investment_lead_events e WHERE e.id=(SELECT MAX(v.id) "
        "FROM investment_lead_events v WHERE v.material_id=e.material_id AND v.content_hash=e.content_hash)"
    )]
    previous = [dict(row) for row in db.execute("SELECT * FROM investment_leads")]
    sources = [dict(row) for row in db.execute("SELECT lead_id,material_id,content_hash FROM investment_lead_sources")]
    db.close()
    result = {"frozen_at": _iso(), "scan_version": SCAN_PROMPT_VERSION,
              "derive_version": DERIVE_PROMPT_VERSION, "materials": materials,
              "previous_events": events, "previous_leads": previous, "previous_sources": sources}
    dump(target, result)
    return result


def report(service: InvestmentLeadService, snapshot: dict, folder: Path) -> dict:
    db = service.db
    rows = [dict(row) for row in db.execute(
        "SELECT l.* FROM investment_leads l WHERE EXISTS(SELECT 1 FROM investment_lead_revisions r "
        "WHERE r.lead_id=l.id AND r.prompt_version=?) ORDER BY l.subject", (DERIVE_PROMPT_VERSION,)
    )]
    events = [dict(row) for row in db.execute("SELECT * FROM investment_lead_events")]
    pending = [dict(row) for row in db.execute(
        "SELECT stage,status,COUNT(*) count FROM investment_lead_tasks GROUP BY stage,status"
    )]
    usage = [dict(row) for row in db.execute(
        "SELECT kind,SUM(calls) calls,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens "
        "FROM investment_lead_usage GROUP BY kind"
    )]
    sources = [dict(row) for row in db.execute("SELECT lead_id,material_id,content_hash FROM investment_lead_sources")]
    retained = {(r["material_id"], r["content_hash"]) for r in sources if r["lead_id"] in {v["id"] for v in rows}}
    previous_retained = {r["lead_id"] for r in snapshot["previous_sources"]
                         if (r["material_id"], r["content_hash"]) in retained}
    summary = {"updated_at": _iso(), "frozen_at": snapshot["frozen_at"],
               "materials": len(snapshot["materials"]), "previous_leads": len(snapshot["previous_leads"]),
               "rescanned": len(events), "classifications": dict(Counter(r["classification"] for r in events)),
               "new_leads": len(rows), "retained_materials": len(retained),
               "previous_leads_with_retained_source": len(previous_retained), "tasks": pending, "usage": usage}
    dump(folder / "summary.json", summary)
    dump(folder / "leads.json", rows)
    dump(folder / "sources.json", sources)
    old = {(r["material_id"], r["content_hash"]): r for r in snapshot["previous_events"]}
    new = {(r["material_id"], r["content_hash"]): r for r in events}
    with (folder / "materials.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["material_id", "posted_at", "url", "old_class", "new_class", "retained", "reason"])
        for material in snapshot["materials"]:
            key = (material["material_id"], material["content_hash"])
            payload = json.loads(material["payload_json"])
            event = new.get(key, {})
            writer.writerow([key[0], material["posted_at"], payload.get("url", ""),
                             old.get(key, {}).get("classification", ""), event.get("classification", ""),
                             key in retained, event.get("reason", "")])
    lines = ["# 投资线索新版全量回放结果", "", f"冻结时间：{snapshot['frozen_at']}", "",
             "独立回放；不写生产结果、不做事实核查或身份搜索；发布时间沿用原帖。", "",
             "三个并发请求复用线上新版模型、归一化门槛、关联召回和保存逻辑；扫描按跨账号批次执行。",
             "先扫描全部冻结材料，再推导，属于存量回看；可能关联到原帖发布之后才出现的材料，不代表实时发现能力。", "",
             f"材料 {summary['materials']}，完成扫描 {len(events)}，形成线索 {len(rows)}。", ""]
    material_map = {(m["material_id"], m["content_hash"]): m for m in snapshot["materials"]}
    for index, row in enumerate(rows, 1):
        lines.extend([f"## {index}. {row['subject']}", "", row["driver_logic"], ""])
        for asset in json.loads(row["assets_json"]):
            lines.append(f"- {asset['name']}（{asset.get('ticker') or '代码未补充'}），{asset['direction']}：{asset['relation']}")
        if row["conditions"]:
            lines.extend(["", "条件：" + row["conditions"]])
        lines.append("")
        for source in sources:
            if source["lead_id"] != row["id"]:
                continue
            material = material_map.get((source["material_id"], source["content_hash"]))
            if material:
                payload = json.loads(material["payload_json"])
                lines.append(f"来源：{material['posted_at']} {payload.get('url', '')}  ")
        lines.append("")
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--input-token-budget", type=int, default=3_000_000)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    snapshot = freeze(args.source, args.output)
    if (snapshot["scan_version"], snapshot["derive_version"]) != (SCAN_PROMPT_VERSION, DERIVE_PROMPT_VERSION):
        raise RuntimeError("已有回放版本不同，请使用新输出目录")
    replay_path = args.output / "replay.sqlite"
    if replay_path.resolve() == args.source.resolve():
        raise RuntimeError("回放数据库不可与生产库相同")
    db = sqlite3.connect(replay_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS hottopic_accounts(screen_name_lower TEXT PRIMARY KEY, status TEXT, investment_leads_enabled INTEGER)")
    db.execute("INSERT OR IGNORE INTO hottopic_accounts VALUES('replay','followed',1)")
    service = InvestmentLeadService(replay_path, connection=db)
    with db:
        for material in snapshot["materials"]:
            fields = list(material)
            db.execute(f"INSERT OR IGNORE INTO investment_lead_materials({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", list(material.values()))
            db.execute("INSERT OR IGNORE INTO investment_lead_observations VALUES(?,?,'replay','original',?,?)",
                       (material["material_id"], material["content_hash"], snapshot["frozen_at"], snapshot["frozen_at"]))
            db.execute("INSERT OR IGNORE INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) VALUES(?,?,'scan',?,'pending',?,?)",
                       (material["material_id"], material["content_hash"], SCAN_PROMPT_VERSION, snapshot["frozen_at"], snapshot["frozen_at"]))
        for lead in snapshot["previous_leads"]:
            fields = list(lead)
            db.execute(f"INSERT OR IGNORE INTO investment_leads({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", list(lead.values()))
        db.execute("UPDATE investment_lead_tasks SET status='pending' WHERE status='processing'")
    analyzer = service.analyzer
    if analyzer is None:
        raise RuntimeError(service.analyzer_error)

    def used_tokens() -> int:
        return int(db.execute("SELECT COALESCE(SUM(input_tokens),0) FROM investment_lead_usage").fetchone()[0])

    workers = min(4, max(1, args.workers))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for stage in ("scan", "derive"):
            while used_tokens() < args.input_token_budget:
                jobs = [dict(r) for r in db.execute(
                    "SELECT t.*,m.payload_json FROM investment_lead_tasks t JOIN investment_lead_materials m "
                    "ON m.material_id=t.material_id AND m.content_hash=t.content_hash "
                    "WHERE t.stage=? AND t.status='pending' AND t.attempts<3 ORDER BY t.created_at,t.id LIMIT ?",
                    (stage, workers * (8 if stage == "scan" else 1))) ]
                if not jobs:
                    break
                futures = {}
                width = 8 if stage == "scan" else 1
                for start in range(0, len(jobs), width):
                    batch = jobs[start:start + width]
                    with db:
                        for job in batch:
                            db.execute("UPDATE investment_lead_tasks SET status='processing',attempts=attempts+1 WHERE id=?", (job["id"],))
                    if stage == "scan":
                        materials = [dict(json.loads(job["payload_json"]), content_hash=job["content_hash"]) for job in batch]
                        futures[pool.submit(analyzer.scan, materials)] = (batch, None)
                    else:
                        job = batch[0]
                        event = dict(db.execute("SELECT * FROM investment_lead_events WHERE material_id=? AND content_hash=? ORDER BY id DESC LIMIT 1", (job["material_id"], job["content_hash"])).fetchone())
                        candidate = dict(event, impact_relations=json.loads(event["impact_relations_json"]), material=json.loads(job["payload_json"]))
                        related = service._related_events(candidate, job["material_id"], job["content_hash"])
                        if event["classification"] == "composable" and not related:
                            service._set_job_status(job["id"], "no_candidate", _iso())
                            continue
                        futures[pool.submit(analyzer.derive, candidate, related, service._recent_leads())] = (batch, related)
                for future in as_completed(futures):
                    batch, related = futures[future]
                    now = _iso()
                    try:
                        output = future.result()
                        service._record_usage(stage, output, now)
                        if stage == "scan":
                            results = {(r["material_id"], r["content_hash"]): r for r in output.items}
                            for job in batch:
                                result = results.get((job["material_id"], job["content_hash"]))
                                if result is None:
                                    with db:
                                        db.execute("UPDATE investment_lead_tasks SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'pending' END,last_error='扫描响应缺少材料，保留重试' WHERE id=?", (job["id"],))
                                    continue
                                service._save_scan_result(job, result, output, now)
                        else:
                            job = batch[0]
                            pairs = {(job["material_id"], job["content_hash"])} | {(r["material_id"], r["content_hash"]) for r in related}
                            with db:
                                for lead in output.items:
                                    selected = [p for p in pairs if p[0] in lead["source_material_ids"]]
                                    service._save_lead(lead, selected or [(job["material_id"], job["content_hash"])], output, now)
                            service._set_job_status(job["id"], "succeeded" if output.items else "no_candidate", now)
                    except Exception as exc:
                        with db:
                            for job in batch:
                                db.execute("UPDATE investment_lead_tasks SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'pending' END,last_error=? WHERE id=?", (f"{type(exc).__name__}: {exc}"[:1000], job["id"]))
                        print(json.dumps({"stage": stage, "error": type(exc).__name__, "batch": len(batch)}), flush=True)
                    print(json.dumps(report(service, snapshot, args.output), ensure_ascii=False), flush=True)
    print(json.dumps(report(service, snapshot, args.output), ensure_ascii=False), flush=True)
    service.close()
    db.close()


if __name__ == "__main__":
    main()
