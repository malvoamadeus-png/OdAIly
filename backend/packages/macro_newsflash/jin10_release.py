from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from packages.jin10_monitor.models import Jin10Item

from .models import MacroUpdate

EASTERN_TZ = ZoneInfo("America/New_York")
JIN10_RELEASE_START_BEFORE = timedelta(seconds=10)
JIN10_RELEASE_END_AFTER = timedelta(seconds=60)
_NUMBER = r"(?P<value>--|[+-]?[\d,]+(?:\.\d+)?)(?P<unit>万人|万|人|%)?"
_CHINESE = re.compile(r"[\u3400-\u9fff]")
_COMMENTARY_PREFIX = re.compile(r"(?:^\s*【|金十数据\s*\d{1,2}月\d{1,2}日讯)")

METRIC_INDICATORS = {
    "initial_claims": "初请失业金人数",
    "pce_yoy": "PCE物价指数年率",
    "nonfarm_payrolls": "季调后非农就业人口",
    "unemployment_rate": "失业率",
    "cpi_yoy_nsa": "未季调CPI年率",
    "cpi_mom_nsa": "未季调CPI月率",
    "cpi_mom_sa": "季调后CPI月率",
    "core_cpi_yoy_nsa": "未季调核心CPI年率",
    "core_cpi_mom_sa": "季调后核心CPI月率",
    "retail_sales_mom": "零售销售月率",
    "fomc_rate": "利率决议",
}


def release_event_key(release: dict[str, Any], metric_id: str) -> str:
    """Return the idempotency event_key for a scheduled release metric."""
    release_id = str(release["release_id"])
    if len(release.get("metric_ids") or ()) > 1:
        return f"jin10:{release_id}:{metric_id}"
    return f"jin10:{release_id}"


def release_updates_from_items(
    release: dict[str, Any],
    items: list[Jin10Item],
    *,
    observed_at: datetime,
    metric_ids: list[str] | tuple[str, ...] | None = None,
) -> list[MacroUpdate]:
    """Match Jin10 flash items for every requested metric in a scheduled release."""
    target_metrics = list(metric_ids if metric_ids is not None else release.get("metric_ids") or ())
    updates: list[MacroUpdate] = []
    for metric_id in target_metrics:
        update = _release_update_for_metric(
            release,
            metric_id,
            items,
            observed_at=observed_at,
        )
        if update is not None:
            updates.append(update)
    return updates


def release_update_from_items(
    release: dict[str, Any],
    items: list[Jin10Item],
    *,
    observed_at: datetime,
) -> MacroUpdate | None:
    """Match a single-metric scheduled release from Jin10 flash items."""
    updates = release_updates_from_items(release, items, observed_at=observed_at)
    return updates[0] if updates else None


def _release_update_for_metric(
    release: dict[str, Any],
    metric_id: str,
    items: list[Jin10Item],
    *,
    observed_at: datetime,
) -> MacroUpdate | None:
    if metric_id not in METRIC_INDICATORS:
        return None
    scheduled_at = datetime.fromisoformat(str(release["scheduled_at"]))
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=UTC)
    scheduled_at = scheduled_at.astimezone(UTC)

    matches: list[tuple[int, datetime, Jin10Item, dict[str, str | None]]] = []
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
        for candidate in dict.fromkeys((item.content, item.title, combined_text)):
            if not _CHINESE.search(candidate):
                continue
            if not _matches_metric(metric_id, candidate):
                continue
            values = _parse_values(metric_id, candidate)
            if values is None:
                continue
            priority = int(values.pop("priority", "0") or "0")
            matches.append((priority, published_at, item, values))
            break

    if not matches:
        return None
    _, published_at, item, values = max(matches, key=lambda match: (match[0], match[1]))
    period_label, reference_period = _reference_period(metric_id, f"{item.title} {item.content}", scheduled_at)
    is_multi_metric = len(release.get("metric_ids") or ()) > 1
    return MacroUpdate(
        metric_id=metric_id,
        reference_period=reference_period,
        period_label=period_label,
        indicator=values.get("indicator") or METRIC_INDICATORS[metric_id],
        actual=str(values["actual"]),
        previous=str(values["previous"]),
        source_name="Jin10",
        source_url=item.source_url or "https://www.jin10.com/",
        observed_at=observed_at.astimezone(UTC).isoformat(timespec="seconds"),
        raw_payload={
            "jin10_release_id": release["release_id"],
            "jin10_multi_metric": is_multi_metric,
            "jin10_source_item_id": item.source_item_id,
            "jin10_published_at": published_at.isoformat(timespec="seconds"),
            "jin10_title": item.title,
            "jin10_content": item.content,
            "jin10_payload": item.raw_payload,
        },
        rate_action=values.get("rate_action"),
        expected=str(values.get("expected") or ""),
    )


def _matches_metric(metric_id: str, text: str) -> bool:
    upper = text.upper()
    if metric_id != "fomc_rate" and _COMMENTARY_PREFIX.search(text):
        return False
    if metric_id == "initial_claims":
        return "初请失业金人数" in text and "四周均值" not in text and "续请" not in text
    if metric_id == "pce_yoy":
        if "核心PCE" in text or "核心 PCE" in text or "季率" in text or "修正值" in text:
            return False
        return bool(
            re.search(r"(?:PCE|个人消费支出)(?:物价|价格)?(?:指数)?(?:年率|同比)", text, re.IGNORECASE)
        )
    if metric_id == "nonfarm_payrolls":
        if any(excluded in upper for excluded in ("ADP", "小非农", "私营", "制造业", "政府")):
            return False
        return bool(re.search(r"(?:季调后)?非农就业(?:人口|人数)", text))
    if metric_id == "unemployment_rate":
        if any(excluded in upper for excluded in ("U6", "U-6", "平均", "初请", "续请", "就业人数")):
            return False
        return bool(re.search(r"(?:美国\s*\d{1,2}月\s*)?失业率", text))
    if metric_id == "cpi_yoy_nsa":
        if "核心" in text or "月率" in text or "季调后CPI年率" in text.replace(" ", ""):
            return False
        return bool(re.search(r"(?:未季调\s*)?CPI\s*(?:物价指数)?(?:年率|同比)", text, re.IGNORECASE))
    if metric_id == "cpi_mom_nsa":
        if "核心" in text or "年率" in text:
            return False
        return bool(re.search(r"未季调\s*CPI\s*(?:物价指数)?(?:月率|环比)", text, re.IGNORECASE))
    if metric_id == "cpi_mom_sa":
        if "核心" in text or "未季调" in text or "年率" in text:
            return False
        return bool(re.search(r"(?:季调后\s*)?CPI\s*(?:物价指数)?(?:月率|环比)", text, re.IGNORECASE))
    if metric_id == "core_cpi_yoy_nsa":
        if "月率" in text:
            return False
        return bool(re.search(r"(?:未季调\s*)?核心\s*CPI\s*(?:物价指数)?(?:年率|同比)", text, re.IGNORECASE))
    if metric_id == "core_cpi_mom_sa":
        if "未季调" in text or "年率" in text:
            return False
        return bool(re.search(r"(?:季调后\s*)?核心\s*CPI\s*(?:物价指数)?(?:月率|环比)", text, re.IGNORECASE))
    if metric_id == "retail_sales_mom":
        if any(excluded in text for excluded in ("核心", "除", "对照组", "年率")):
            return False
        return bool(re.search(r"零售销售(?:月率|环比)", text))
    if metric_id == "fomc_rate":
        if re.search(r"金十数据\s*\d{1,2}月\d{1,2}日讯", text) or any(w in text for w in ("解读", "回顾", "上月", "上次")):
            return False
        if "下限" in text and "目标区间" not in text and "利率区间" not in text:
            return False
        if "美联储利率决定" in text and "上限" in text:
            return True
        return bool(
            re.search(
                r"(?:美联储|FOMC).*?(?:联邦基金利率|利率目标区间|基准利率|维持利率|降息|加息)",
                text,
                re.IGNORECASE,
            )
            and re.search(r"\d+(?:\.\d+)?%?\s*[-–至~到]\s*\d+(?:\.\d+)?%", text)
        )
    return False


def _parse_values(metric_id: str, text: str) -> dict[str, str | None] | None:
    if metric_id == "fomc_rate":
        return _parse_fomc_values(text)

    patterns = {
        "initial_claims": rf"初请失业金人数[^\d+\-，,。]{{0,16}}{_NUMBER}",
        "pce_yoy": rf"(?:PCE|个人消费支出)(?:物价|价格)?(?:指数)?(?:年率|同比)[^\d+\-，,。]{{0,20}}{_NUMBER}",
        "nonfarm_payrolls": rf"非农就业(?:人口|人数)[^\d+\-，,。]{{0,16}}{_NUMBER}",
        "unemployment_rate": rf"失业率[^\d+\-，,。]{{0,16}}{_NUMBER}",
        "cpi_yoy_nsa": rf"CPI\s*(?:物价指数)?(?:年率|同比)[^\d+\-，,。]{{0,16}}{_NUMBER}",
        "cpi_mom_nsa": rf"CPI\s*(?:物价指数)?(?:月率|环比)[^\d+\-，,。]{{0,16}}{_NUMBER}",
        "cpi_mom_sa": rf"CPI\s*(?:物价指数)?(?:月率|环比)[^\d+\-，,。]{{0,16}}{_NUMBER}",
        "core_cpi_yoy_nsa": rf"核心\s*CPI\s*(?:物价指数)?(?:年率|同比)[^\d+\-，,。]{{0,16}}{_NUMBER}",
        "core_cpi_mom_sa": rf"核心\s*CPI\s*(?:物价指数)?(?:月率|环比)[^\d+\-，,。]{{0,16}}{_NUMBER}",
        "retail_sales_mom": rf"零售销售(?:月率|环比)[^\d+\-，,。]{{0,16}}{_NUMBER}",
    }
    actual_match = re.search(patterns[metric_id], text, re.IGNORECASE)
    if actual_match is None or actual_match.group("value") == "--":
        return None
    default_unit = "万人" if metric_id in {"initial_claims", "nonfarm_payrolls"} else "%"
    actual_unit = actual_match.group("unit") or default_unit
    actual = f"{actual_match.group('value')}{actual_unit}"

    previous_match = re.search(rf"前值(?:由[^，,。]*?修正为)?\s*{_NUMBER}", text)
    expected_match = re.search(rf"预期(?:为|约)?\s*{_NUMBER}", text)
    previous = _matched_value(previous_match, actual_unit) or "暂无"
    expected = _matched_value(expected_match, actual_unit)
    return {
        "actual": actual,
        "previous": previous,
        "expected": expected,
        "rate_action": None,
        "indicator": METRIC_INDICATORS[metric_id],
        "priority": "0",
    }


def _parse_fomc_values(text: str) -> dict[str, str | None] | None:
    range_match = re.search(
        r"(?P<low>\d+(?:\.\d+)?)%?\s*[-–至~到]\s*(?P<high>\d+(?:\.\d+)?)%",
        text,
    )
    if range_match and (
        "目标区间" in text
        or "基准利率" in text
        or "维持" in text
        or "降息" in text
        or "加息" in text
        or "下调" in text
        or "上调" in text
    ):
        actual = f"{range_match.group('low')}%至{range_match.group('high')}%"
        if "维持" in text or "不变" in text:
            rate_action = "维持"
        elif "下调" in text or "降息" in text:
            rate_action = "下调"
        elif "上调" in text or "加息" in text:
            rate_action = "上调"
        else:
            rate_action = "调整"
        prev_range = re.search(
            r"前值[^\d]{0,12}(?P<low>\d+(?:\.\d+)?)%?\s*[-–至~到]\s*(?P<high>\d+(?:\.\d+)?)%",
            text,
        )
        previous = f"{prev_range.group('low')}%至{prev_range.group('high')}%" if prev_range else "暂无"
        exp_range = re.search(
            r"预期[^\d]{0,12}(?P<low>\d+(?:\.\d+)?)%?\s*[-–至~到]\s*(?P<high>\d+(?:\.\d+)?)%",
            text,
        )
        expected = f"{exp_range.group('low')}%至{exp_range.group('high')}%" if exp_range else ""
        return {
            "actual": actual,
            "previous": previous,
            "expected": expected,
            "rate_action": rate_action,
            "indicator": "利率决议",
            "priority": "1",
        }

    upper_match = re.search(rf"美联储利率决定\s*\(上限\)[^\d+\-，,。]{{0,16}}{_NUMBER}", text)
    if upper_match is None or upper_match.group("value") == "--":
        return None
    actual_unit = upper_match.group("unit") or "%"
    actual = f"{upper_match.group('value')}{actual_unit}"
    previous_match = re.search(rf"前值(?:由[^，,。]*?修正为)?\s*{_NUMBER}", text)
    expected_match = re.search(rf"预期(?:为|约)?\s*{_NUMBER}", text)
    return {
        "actual": actual,
        "previous": _matched_value(previous_match, actual_unit) or "暂无",
        "expected": _matched_value(expected_match, actual_unit),
        "rate_action": None,
        "indicator": "美联储利率决定(上限)",
        "priority": "0",
    }


def _matched_value(match: re.Match[str] | None, default_unit: str) -> str:
    if match is None or match.group("value") == "--":
        return ""
    return f"{match.group('value')}{match.group('unit') or default_unit}"


def _reference_period(metric_id: str, text: str, scheduled_at: datetime) -> tuple[str, str]:
    eastern_scheduled = scheduled_at.astimezone(EASTERN_TZ)
    if metric_id == "initial_claims":
        match = re.search(r"至\s*(\d{1,2})月\s*(\d{1,2})日当周", text)
        if match:
            month, day = (int(value) for value in match.groups())
            year = eastern_scheduled.year - (1 if month > eastern_scheduled.month + 6 else 0)
            return f"至{month}月{day}日当周", f"{year:04d}-{month:02d}-{day:02d}"
    elif metric_id == "fomc_rate":
        return f"{eastern_scheduled.month}月", eastern_scheduled.date().isoformat()
    else:
        match = re.search(
            r"(?<!\d)(\d{1,2})月[^，,。]{0,24}(?:PCE|个人消费支出|非农|失业率|CPI|零售销售)",
            text,
            re.IGNORECASE,
        )
        if match:
            month = int(match.group(1))
            year = eastern_scheduled.year - (1 if month > eastern_scheduled.month else 0)
            return f"{month}月", f"{year:04d}-{month:02d}"
    label = eastern_scheduled.strftime("%m月%d日")
    return label, eastern_scheduled.date().isoformat()
