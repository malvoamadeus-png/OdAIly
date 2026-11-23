from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.jin10_monitor.models import Jin10Item

from .models import MacroUpdate

JIN10_RELEASE_START_BEFORE = timedelta(seconds=10)
JIN10_RELEASE_END_AFTER = timedelta(seconds=60)
_NUMBER = r"(?P<value>--|[+-]?[\d,]+(?:\.\d+)?)(?P<unit>万人|万|人|%)?"
_CHINESE = re.compile(r"[\u3400-\u9fff]")


def release_update_from_items(
    release: dict[str, Any],
    items: list[Jin10Item],
    *,
    observed_at: datetime,
) -> MacroUpdate | None:
    scheduled_at = datetime.fromisoformat(str(release["scheduled_at"]))
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=UTC)
    scheduled_at = scheduled_at.astimezone(UTC)

    metric_id = next(
        (candidate for candidate in release["metric_ids"] if candidate in {"initial_claims", "pce_yoy"}),
        None,
    )
    if metric_id is None:
        return None

    matches: list[tuple[datetime, Jin10Item, dict[str, str]]] = []
    for item in items:
        published_at = item.published_at
        if published_at is None:
            continue
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        published_at = published_at.astimezone(UTC)
        if not scheduled_at - JIN10_RELEASE_START_BEFORE <= published_at <= scheduled_at + JIN10_RELEASE_END_AFTER:
            continue
        combined_text = f"{item.title} {item.content}"
        match_text = next(
            (
                candidate
                for candidate in dict.fromkeys((item.content, item.title, combined_text))
                if _CHINESE.search(candidate)
                and _matches_metric(metric_id, candidate)
                and _parse_values(metric_id, candidate) is not None
            ),
            None,
        )
        if match_text is None:
            continue
        values = _parse_values(metric_id, match_text)
        if values is None:
            continue
        matches.append((published_at, item, values))

    if not matches:
        return None
    published_at, item, values = max(matches, key=lambda match: match[0])
    period_label, reference_period = _reference_period(metric_id, f"{item.title} {item.content}", scheduled_at)
    return MacroUpdate(
        metric_id=metric_id,
        reference_period=reference_period,
        period_label=period_label,
        indicator="初请失业金人数" if metric_id == "initial_claims" else "PCE物价指数年率",
        actual=values["actual"],
        previous=values["previous"],
        source_name="Jin10",
        source_url=item.source_url or "https://www.jin10.com/",
        observed_at=observed_at.astimezone(UTC).isoformat(timespec="seconds"),
        raw_payload={
            "jin10_release_id": release["release_id"],
            "jin10_source_item_id": item.source_item_id,
            "jin10_published_at": published_at.isoformat(timespec="seconds"),
            "jin10_title": item.title,
            "jin10_content": item.content,
            "jin10_payload": item.raw_payload,
        },
        expected=values["expected"],
    )


def _matches_metric(metric_id: str, text: str) -> bool:
    if metric_id == "initial_claims":
        return "初请失业金人数" in text and "四周均值" not in text
    if "核心PCE" in text or "核心 PCE" in text:
        return False
    return bool(
        re.search(r"(?:PCE|个人消费支出)(?:物价|价格)?(?:指数)?(?:年率|同比)", text, re.IGNORECASE)
    )


def _parse_values(metric_id: str, text: str) -> dict[str, str] | None:
    if metric_id == "initial_claims":
        actual_pattern = rf"初请失业金人数[^\d，,。]{{0,16}}{_NUMBER}"
    else:
        actual_pattern = rf"(?:PCE|个人消费支出)(?:物价|价格)?(?:指数)?(?:年率|同比)[^\d，,。]{{0,20}}{_NUMBER}"
    actual_match = re.search(actual_pattern, text, re.IGNORECASE)
    if actual_match is None or actual_match.group("value") == "--":
        return None
    actual_unit = actual_match.group("unit") or ("万人" if metric_id == "initial_claims" else "%")
    actual = f"{actual_match.group('value')}{actual_unit}"

    previous_match = re.search(rf"前值(?:由[^，,。]*?修正为)?\s*{_NUMBER}", text)
    expected_match = re.search(rf"预期(?:为|约)?\s*{_NUMBER}", text)
    previous = _matched_value(previous_match, actual_unit)
    expected = _matched_value(expected_match, actual_unit)
    return {"actual": actual, "previous": previous, "expected": expected}


def _matched_value(match: re.Match[str] | None, default_unit: str) -> str:
    if match is None or match.group("value") == "--":
        return "暂无"
    return f"{match.group('value')}{match.group('unit') or default_unit}"


def _reference_period(metric_id: str, text: str, scheduled_at: datetime) -> tuple[str, str]:
    if metric_id == "initial_claims":
        match = re.search(r"至\s*(\d{1,2})月\s*(\d{1,2})日当周", text)
        if match:
            month, day = (int(value) for value in match.groups())
            year = scheduled_at.year - (1 if month > scheduled_at.month + 6 else 0)
            return f"至{month}月{day}日当周", f"{year:04d}-{month:02d}-{day:02d}"
    else:
        match = re.search(
            r"(?<!\d)(\d{1,2})月[^，,。]{0,24}(?:PCE|个人消费支出)",
            text,
            re.IGNORECASE,
        )
        if match:
            month = int(match.group(1))
            year = scheduled_at.year - (1 if month > scheduled_at.month else 0)
            return f"{month}月", f"{year:04d}-{month:02d}"
    label = scheduled_at.astimezone(UTC).strftime("%m月%d日")
    return label, scheduled_at.astimezone(UTC).date().isoformat()
