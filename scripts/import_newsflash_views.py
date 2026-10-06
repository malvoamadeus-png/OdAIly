#!/usr/bin/env python3
"""Import all locally available newsflash XLSX coverage without overwriting facts.

Run from the repository root with no arguments:

    python3 scripts/import_newsflash_views.py

The script intentionally uses the Windows OpenSSH client configured for this
workspace. It performs a read-only production comparison first, then uploads
each needed workbook to /tmp and invokes the repository's safe import mode.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
SSH = Path("/mnt/c/WINDOWS/System32/OpenSSH/ssh.exe")
SCP = Path("/mnt/c/WINDOWS/System32/OpenSSH/scp.exe")
SSH_CONFIG = "C:/Users/A/.ssh/config"
SSH_ALIAS = "odaily-official"
REMOTE_ROOT = "/opt/OdAIly"
REQUIRED_HEADERS = {"ID", "标题", "操作人", "链接", "发布时间", "阅读量", "是否推送", "推送时间"}
XML_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@dataclass(frozen=True)
class Row:
    source_item_id: str
    published_at: datetime


@dataclass
class WorkbookInfo:
    path: Path
    rows: list[Row]

    @property
    def ids(self) -> set[str]:
        return {row.source_item_id for row in self.rows}

    @property
    def weeks(self) -> dict[str, int]:
        result: dict[str, int] = defaultdict(int)
        for row in self.rows:
            result[week_start(row.published_at.date()).isoformat()] += 1
        return dict(sorted(result.items()))

    @property
    def start_date(self) -> date:
        return min(row.published_at.date() for row in self.rows)

    @property
    def end_date(self) -> date:
        return max(row.published_at.date() for row in self.rows) + timedelta(days=1)


def normalize_id(value: object) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str | None:
    if cell.attrib.get("t") == "inlineStr":
        return "".join(item.text or "" for item in cell.findall(".//m:t", XML_NS))
    value = cell.find("m:v", XML_NS)
    if value is None:
        return None
    raw = value.text
    if cell.attrib.get("t") == "s" and raw is not None:
        return shared_strings[int(raw)]
    return raw


def parse_xlsx(path: Path) -> WorkbookInfo | None:
    with ZipFile(path) as workbook:
        names = set(workbook.namelist())
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
            for item in root.findall("m:si", XML_NS):
                shared_strings.append("".join(text.text or "" for text in item.findall(".//m:t", XML_NS)))
        sheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
        rows: list[dict[str, str | None]] = []
        for xml_row in sheet.findall(".//m:sheetData/m:row", XML_NS):
            row: dict[str, str | None] = {}
            for cell in xml_row.findall("m:c", XML_NS):
                ref = cell.attrib.get("r", "")
                column = "".join(char for char in ref if char.isalpha())
                row[column] = cell_value(cell, shared_strings)
            rows.append(row)
    if not rows:
        return None
    headers = {str(value or "").strip() for value in rows[0].values()}
    if not REQUIRED_HEADERS.issubset(headers):
        return None
    parsed: list[Row] = []
    for row in rows[1:]:
        source_item_id = normalize_id(row.get("A"))
        if not source_item_id or not row.get("E"):
            continue
        try:
            published_at = datetime(1899, 12, 30) + timedelta(days=float(row["E"]))
        except (TypeError, ValueError):
            continue
        parsed.append(Row(source_item_id=source_item_id, published_at=published_at))
    return WorkbookInfo(path=path, rows=parsed) if parsed else None


def find_workbooks() -> list[WorkbookInfo]:
    candidates = list(ROOT.glob("*.xlsx")) + list((ROOT / "data" / "raw").rglob("*.xlsx"))
    result: list[WorkbookInfo] = []
    seen: set[Path] = set()
    for path in sorted(candidates):
        path = path.resolve()
        if path in seen or path.name.startswith("~$"):
            continue
        seen.add(path)
        info = parse_xlsx(path)
        if info:
            result.append(info)
    return sorted(result, key=lambda item: (item.path.stat().st_mtime, str(item.path)))


def run_process(command: list[str], *, input_text: str | None = None) -> str:
    completed = subprocess.run(command, input=input_text, text=True, capture_output=True, cwd=ROOT)
    if completed.returncode:
        if completed.stdout:
            print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
        raise RuntimeError(f"command failed with exit code {completed.returncode}: {command[0]}")
    return completed.stdout


def windows_local_path(path: Path) -> str:
    completed = subprocess.run(["wslpath", "-w", str(path)], text=True, capture_output=True, cwd=ROOT)
    if completed.returncode:
        raise RuntimeError(f"cannot convert WSL path for Windows scp: {path}")
    return completed.stdout.strip()


def ssh_command(remote_command: str, *, input_text: str | None = None) -> str:
    return run_process([str(SSH), "-F", SSH_CONFIG, SSH_ALIAS, remote_command], input_text=input_text)


def remote_existing_ids(ids: set[str]) -> set[str]:
    payload = json.dumps(sorted(ids), ensure_ascii=False)
    code = """
import json, sqlite3, sys
ids = json.load(sys.stdin)
found = set()
with sqlite3.connect('data/database/odaily.sqlite') as conn:
    for offset in range(0, len(ids), 500):
        batch = ids[offset:offset + 500]
        marks = ','.join('?' for _ in batch)
        found.update(row[0] for row in conn.execute(
            f'SELECT source_item_id FROM newsflash_operation_facts WHERE source_item_id IN ({marks})', batch
        ))
print(json.dumps(sorted(found), ensure_ascii=False))
"""
    output = ssh_command(f"cd {shlex.quote(REMOTE_ROOT)} && .venv/bin/python -c {shlex.quote(code)}", input_text=payload)
    return set(json.loads(output.strip() or "[]"))


def import_workbook(info: WorkbookInfo, index: int) -> None:
    remote_name = f"/tmp/odaily-newsflash-view-import-{os.getpid()}-{index}-{uuid.uuid4().hex}.xlsx"
    try:
        run_process([str(SCP), "-F", SSH_CONFIG, windows_local_path(info.path), f"{SSH_ALIAS}:{remote_name}"])
        command = (
            f"cd {shlex.quote(REMOTE_ROOT)} && .venv/bin/python backend/src/main.py "
            f"newsflash-ops-import-xlsx --path {shlex.quote(remote_name)} "
            f"--start-date {info.start_date.isoformat()} --end-date {info.end_date.isoformat()} "
            "--preserve-existing"
        )
        output = ssh_command(command)
        print(output, end="")
    finally:
        ssh_command(f"rm -f -- {shlex.quote(remote_name)}")


def main() -> int:
    if not SSH.exists() or not SCP.exists():
        print(f"Windows OpenSSH not found: {SSH} / {SCP}", file=sys.stderr)
        return 2
    workbooks = find_workbooks()
    if not workbooks:
        print("No compatible Odaily XLSX files found in the repository root or data/raw.", file=sys.stderr)
        return 2
    all_ids = set().union(*(info.ids for info in workbooks))
    existing = remote_existing_ids(all_ids)
    local_weeks: dict[str, int] = defaultdict(int)
    week_existing: dict[str, set[str]] = defaultdict(set)
    for info in workbooks:
        for row in info.rows:
            key = week_start(row.published_at.date()).isoformat()
            local_weeks[key] += 1
            if row.source_item_id in existing:
                week_existing[key].add(row.source_item_id)
    print("Local XLSX coverage:")
    for info in workbooks:
        print(f"  {info.path.relative_to(ROOT)}: {info.weeks}")
    print("Server comparison:")
    for key in sorted(local_weeks):
        count = local_weeks[key]
        known = len(week_existing[key])
        state = "complete" if known >= count else "missing"
        print(f"  {key}: local={count} server_existing={known} state={state}")
    pending = [info for info in workbooks if info.ids - existing]
    if not pending:
        print("Nothing to import. Existing operation facts were preserved.")
        return 0
    print(f"Importing {len(pending)} workbook(s) in preserve-existing mode...")
    for index, info in enumerate(pending, start=1):
        import_workbook(info, index)
    print("Import complete. Existing operation facts were not overwritten; remote temporary files were removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
