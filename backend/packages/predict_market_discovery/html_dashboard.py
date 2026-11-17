"""Read-only dashboard for Predict.fun HTML discovery."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def dashboard(path: Path) -> dict:
    if not path.is_file():
        return {"available": False, "items": [], "scans": [], "message": "尚未建立扫描数据库"}
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("""SELECT category_id,slug,title,created_at,starts_at,first_seen_at,
            first_feed,status,reason,detail_json,writer_json,final_title,final_content,push_result,last_error,updated_at
            FROM html_topics WHERE status != 'baseline' ORDER BY first_seen_at DESC LIMIT 200""").fetchall()
        scans = con.execute("""SELECT r.* FROM html_scan_runs r JOIN
            (SELECT feed,MAX(id) AS id FROM html_scan_runs GROUP BY feed) latest ON r.id=latest.id
            ORDER BY r.feed""").fetchall()
        items = []
        for row in rows:
            item = dict(row)
            for key in ("detail_json", "writer_json", "push_result"):
                try:
                    item[key] = json.loads(item[key]) if item[key] else None
                except (TypeError, ValueError):
                    item[key] = None
            items.append(item)
    return {"available": True, "items": items, "scans": [dict(row) for row in scans],
            "message": "每栏目仅扫描网页热门首屏，可能遗漏新市场"}
