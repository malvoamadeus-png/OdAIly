from __future__ import annotations

import csv
import io
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from typing import Any

import requests
from bs4 import BeautifulSoup

from .models import MacroUpdate
from .settings import MacroNewsflashSettings


BLS_SERIES = {
    "nfp_level": "CES0000000001",
    "unemployment": "LNS14000000",
    "cpi_nsa": "CUUR0000SA0",
    "cpi_sa": "CUSR0000SA0",
    "core_cpi_sa": "CUSR0000SA0L1E",
}
BLS_SOURCE_URLS = {
    "nfp_level": "https://www.bls.gov/ces/",
    "unemployment": "https://www.bls.gov/cps/",
    "cpi_nsa": "https://www.bls.gov/cpi/",
    "cpi_sa": "https://www.bls.gov/cpi/",
    "core_cpi_sa": "https://www.bls.gov/cpi/",
}
FRED_SOURCE_URLS = {
    "retail_sales": "https://fred.stlouisfed.org/series/RSAFS",
    "initial_claims": "https://fred.stlouisfed.org/series/ICSA",
    "pce_price_index": "https://fred.stlouisfed.org/series/PCEPI",
    "nfp_level": "https://fred.stlouisfed.org/series/PAYEMS",
    "unemployment": "https://fred.stlouisfed.org/series/UNRATE",
    "cpi_nsa": "https://fred.stlouisfed.org/series/CPIAUCNS",
    "cpi_sa": "https://fred.stlouisfed.org/series/CPIAUCSL",
    "core_cpi_sa": "https://fred.stlouisfed.org/series/CPILFESL",
}
FRED_BLS_SERIES = {
    "nfp_level": "PAYEMS",
    "unemployment": "UNRATE",
    "cpi_nsa": "CPIAUCNS",
    "cpi_sa": "CPIAUCSL",
    "core_cpi_sa": "CPILFESL",
}
FRED_USER_AGENT = "curl/8.5.0 OdAIly-macro-newsflash/1.0"
_FOMC_CACHE_LOCK = threading.Lock()
_FOMC_CACHE_KEY: tuple[str, str] | None = None
_FOMC_CACHE_VALUE: MacroUpdate | None = None


@dataclass(frozen=True, slots=True)
class Observation:
    period: str
    value: Decimal


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid numeric value: {value!r}") from exc


def _fmt(value: Decimal, places: int = 1) -> str:
    quantum = Decimal(1).scaleb(-places)
    return format(value.quantize(quantum, rounding=ROUND_HALF_UP), f".{places}f")


def _period_label(period: str) -> str:
    _, month = period.split("-")
    return f"{int(month)}月"


def _monthly_adjacent(current: Observation, previous: Observation) -> bool:
    current_date = date.fromisoformat(current.period + "-01")
    previous_date = date.fromisoformat(previous.period + "-01")
    delta = (current_date.year - previous_date.year) * 12 + current_date.month - previous_date.month
    return delta == 1


def _percent_change(current: Decimal, previous: Decimal) -> Decimal:
    if previous == 0:
        raise ValueError("cannot calculate percent change from zero")
    return (current / previous - Decimal(1)) * Decimal(100)


def fetch_bls_series(settings: MacroNewsflashSettings) -> dict[str, list[Observation]]:
    today = datetime.now(UTC).date()
    payload: dict[str, Any] = {
        "seriesid": list(BLS_SERIES.values()),
        "startyear": str(today.year - 2),
        "endyear": str(today.year),
    }
    if settings.bls_api_key:
        payload["registrationkey"] = settings.bls_api_key
    response = requests.post(
        settings.bls_api_endpoint,
        json=payload,
        headers={"Content-Type": "application/json", "User-Agent": "OdAIly-macro-newsflash/1.0"},
        timeout=settings.request_timeout_seconds,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS API returned {body.get('status')}: {body.get('message', [])}")
    result: dict[str, list[Observation]] = {}
    for series in body.get("Results", {}).get("series", []):
        series_id = str(series.get("seriesID") or "")
        rows: list[Observation] = []
        for row in series.get("data", []):
            period_code = str(row.get("period") or "")
            match = re.fullmatch(r"M(\d{2})", period_code)
            if not match or match.group(1) == "13":
                continue
            rows.append(Observation(f"{row['year']}-{match.group(1)}", _decimal(row["value"])))
        result[series_id] = sorted(rows, key=lambda item: item.period)
    missing = set(BLS_SERIES.values()) - result.keys()
    if missing:
        raise RuntimeError(f"BLS API omitted series: {', '.join(sorted(missing))}")
    return result


def fetch_fred_series(settings: MacroNewsflashSettings, series_id: str) -> list[Observation]:
    observation_start = (datetime.now(UTC).date() - timedelta(days=730)).isoformat()
    response = requests.get(
        settings.fred_graph_endpoint,
        params={"id": series_id, "cosd": observation_start},
        headers={"User-Agent": FRED_USER_AGENT},
        timeout=settings.request_timeout_seconds,
    )
    response.raise_for_status()
    rows: list[Observation] = []
    for row in csv.DictReader(io.StringIO(response.text)):
        date_value = row.get("observation_date") or row.get("DATE") or row.get("date")
        value = row.get(series_id) or row.get(series_id.lower())
        if not date_value or not value or value.strip() in {".", ""}:
            continue
        parsed_date = date.fromisoformat(date_value[:10])
        period = parsed_date.strftime("%Y-%m") if parsed_date.day == 1 else parsed_date.isoformat()
        rows.append(Observation(period, _decimal(value)))
    rows.sort(key=lambda item: item.period)
    if len(rows) < 2:
        raise RuntimeError(f"FRED series {series_id} returned fewer than two observations")
    return rows


def fetch_fomc_updates(settings: MacroNewsflashSettings) -> MacroUpdate:
    global _FOMC_CACHE_KEY, _FOMC_CACHE_VALUE
    response = requests.get(
        settings.fed_feed_url,
        headers={"User-Agent": "OdAIly-macro-newsflash/1.0"},
        timeout=settings.request_timeout_seconds,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    entries: list[tuple[datetime, str, str, str]] = []
    for item in root.iter():
        if _local_name(item.tag) not in {"item", "entry"}:
            continue
        fields = {_local_name(child.tag): (child.text or "").strip() for child in item}
        title = fields.get("title", "")
        if "fomc statement" not in title.lower():
            continue
        link = fields.get("link", "")
        if not link:
            link_node = next((child for child in item if _local_name(child.tag) == "link"), None)
            link = link_node.attrib.get("href", "") if link_node is not None else ""
        published_text = fields.get("pubDate") or fields.get("published") or fields.get("updated")
        published = _parse_feed_datetime(published_text)
        if link and published:
            entries.append((published, title, link, fields.get("description", "")))
    entries.sort(key=lambda item: item[0])
    if not entries:
        raise RuntimeError("Federal Reserve feed contains no FOMC statement")
    current = entries[-1]
    previous_link = entries[-2][2] if len(entries) > 1 else ""
    cache_key = (current[2], previous_link)
    with _FOMC_CACHE_LOCK:
        if cache_key == _FOMC_CACHE_KEY and _FOMC_CACHE_VALUE is not None:
            return _FOMC_CACHE_VALUE
    previous_range: tuple[Decimal, Decimal] | None = None
    if len(entries) > 1:
        try:
            previous_range = _fetch_rate_range(entries[-2][2], settings.request_timeout_seconds)
        except Exception:
            previous_range = None
    current_range = _fetch_rate_range(current[2], settings.request_timeout_seconds)
    period_label = f"{current[0].month}月"
    current_text = f"{_fmt(current_range[0], 2)}%至{_fmt(current_range[1], 2)}%"
    previous_text = (
        f"{_fmt(previous_range[0], 2)}%至{_fmt(previous_range[1], 2)}%"
        if previous_range
        else "未取得"
    )
    if previous_range is None:
        raise RuntimeError("could not parse the preceding FOMC target range")
    if current_range == previous_range:
        action = "维持"
    elif current_range[0] > previous_range[0]:
        action = "上调"
    elif current_range[0] < previous_range[0]:
        action = "下调"
    else:
        action = "调整"
    update = MacroUpdate(
        metric_id="fomc_rate",
        reference_period=current[0].date().isoformat(),
        period_label=period_label,
        indicator="利率决议",
        actual=current_text,
        previous=previous_text,
        source_name="Federal Reserve FOMC statement",
        source_url=current[2],
        observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        raw_payload={
            "feed_title": current[1],
            "published_at": current[0].isoformat(),
            "target_range": [str(current_range[0]), str(current_range[1])],
            "previous_target_range": [str(previous_range[0]), str(previous_range[1])],
        },
        rate_action=action,
    )
    with _FOMC_CACHE_LOCK:
        _FOMC_CACHE_KEY = cache_key
        _FOMC_CACHE_VALUE = update
    return update


def collect_updates(
    settings: MacroNewsflashSettings,
    *,
    source_names: set[str] | None = None,
) -> tuple[list[MacroUpdate], dict[str, str]]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    updates: list[MacroUpdate] = []
    errors: dict[str, str] = {}
    jobs = {
        "bls": lambda: _bls_updates(settings),
        "retail_sales": lambda: _retail_update(settings),
        "initial_claims": lambda: _claims_update(settings),
        "pce": lambda: _pce_update(settings),
        "fomc": lambda: [fetch_fomc_updates(settings)],
    }
    if source_names is not None:
        jobs = {name: job for name, job in jobs.items() if name in source_names}
    if not jobs:
        return [], {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(job): source for source, job in jobs.items()}
        for future in as_completed(futures):
            source = futures[future]
            try:
                updates.extend(future.result())
            except Exception as exc:
                errors[source] = str(exc)
    return sorted(updates, key=lambda item: item.metric_id), errors


def _bls_updates(settings: MacroNewsflashSettings) -> list[MacroUpdate]:
    fallback = False
    try:
        payload = fetch_bls_series(settings)
    except Exception as bls_error:
        from concurrent.futures import ThreadPoolExecutor

        try:
            with ThreadPoolExecutor(max_workers=len(FRED_BLS_SERIES)) as pool:
                fred_rows = dict(
                    zip(
                        FRED_BLS_SERIES,
                        pool.map(lambda series_id: fetch_fred_series(settings, series_id), FRED_BLS_SERIES.values()),
                        strict=True,
                    )
                )
            payload = {
                BLS_SERIES[name]: rows
                for name, rows in fred_rows.items()
            }
            fallback = True
        except Exception as fred_error:
            raise RuntimeError(f"BLS API failed ({bls_error}); FRED mirror fallback failed ({fred_error})") from fred_error
    by_id = {name: payload[series_id] for name, series_id in BLS_SERIES.items()}
    if fallback:
        source = lambda name: (
            f"FRED mirror of BLS {FRED_BLS_SERIES[name]}",
            FRED_SOURCE_URLS[name],
            FRED_BLS_SERIES[name],
        )
    else:
        bls_source_names = {
            "nfp_level": "BLS CES API",
            "unemployment": "BLS CPS API",
            "cpi_nsa": "BLS CPI API",
            "cpi_sa": "BLS CPI API",
            "core_cpi_sa": "BLS CPI API",
        }
        source = lambda name: (
            bls_source_names[name],
            BLS_SOURCE_URLS[name],
            BLS_SERIES[name],
        )
    updates: list[MacroUpdate] = []
    nfp = by_id["nfp_level"]
    if len(nfp) < 3:
        raise RuntimeError("BLS nonfarm series needs three monthly observations")
    current_nfp, previous_nfp, older_nfp = nfp[-1], nfp[-2], nfp[-3]
    if not _monthly_adjacent(current_nfp, previous_nfp) or not _monthly_adjacent(previous_nfp, older_nfp):
        raise RuntimeError("BLS nonfarm observations are not consecutive months")
    nfp_source, nfp_source_url, nfp_series_id = source("nfp_level")
    updates.append(
        _update(
            metric_id="nonfarm_payrolls",
            period=current_nfp.period,
            indicator="季调后非农就业人口",
            actual=f"{_fmt((current_nfp.value - previous_nfp.value) / Decimal(10))}万人",
            previous=f"{_fmt((previous_nfp.value - older_nfp.value) / Decimal(10))}万人",
            source=nfp_source,
            source_url=nfp_source_url,
            raw={"series_id": nfp_series_id, "level_thousands": [str(item.value) for item in nfp[-3:]]},
        )
    )

    unemployment = by_id["unemployment"]
    if len(unemployment) < 2 or not _monthly_adjacent(unemployment[-1], unemployment[-2]):
        raise RuntimeError("BLS unemployment observations are not consecutive months")
    unemployment_source, unemployment_source_url, unemployment_series_id = source("unemployment")
    updates.append(
        _update(
            metric_id="unemployment_rate",
            period=unemployment[-1].period,
            indicator="失业率",
            actual=f"{_fmt(unemployment[-1].value)}%",
            previous=f"{_fmt(unemployment[-2].value)}%",
            source=unemployment_source,
            source_url=unemployment_source_url,
            raw={"series_id": unemployment_series_id, "observations": [_json_observation(item) for item in unemployment[-2:]]},
        )
    )

    cpi_specs = (
        ("cpi_nsa", "cpi_yoy_nsa", "未季调CPI年率", "year_over_year"),
        ("cpi_nsa", "cpi_mom_nsa", "未季调CPI月率", "month_over_month"),
        ("cpi_sa", "cpi_mom_sa", "季调后CPI月率", "month_over_month"),
        ("core_cpi_sa", "core_cpi_mom_sa", "季调后核心CPI月率", "month_over_month"),
    )
    for series_name, metric_id, indicator, kind in cpi_specs:
        rows = by_id[series_name]
        result = _index_change(rows, kind)
        if not result:
            continue
        period, actual, previous_value, reference_rows = result
        cpi_source, cpi_source_url, cpi_series_id = source(series_name)
        updates.append(
            _update(
                metric_id=metric_id,
                period=period,
                indicator=indicator,
                actual=f"{_fmt(actual)}%",
                previous=f"{_fmt(previous_value)}%",
                source=cpi_source,
                source_url=cpi_source_url,
                raw={"series_id": cpi_series_id, "observations": [_json_observation(item) for item in reference_rows]},
            )
        )
    return updates


def _index_change(
    rows: list[Observation], kind: str
) -> tuple[str, Decimal, Decimal, list[Observation]] | None:
    needed = 14 if kind == "year_over_year" else 3
    if len(rows) < needed:
        raise RuntimeError(f"BLS CPI series needs at least {needed} monthly observations")
    recent = rows[-needed:]
    for first, second in zip(recent, recent[1:]):
        if not _monthly_adjacent(second, first):
            raise RuntimeError("BLS CPI observations are not consecutive months")
    if kind == "year_over_year":
        actual = _percent_change(recent[-1].value, recent[-13].value)
        previous = _percent_change(recent[-2].value, recent[-14].value)
    else:
        actual = _percent_change(recent[-1].value, recent[-2].value)
        previous = _percent_change(recent[-2].value, recent[-3].value)
    return recent[-1].period, actual, previous, recent[-14:]


def _retail_update(settings: MacroNewsflashSettings) -> list[MacroUpdate]:
    rows = fetch_fred_series(settings, "RSAFS")
    if len(rows) < 3:
        raise RuntimeError("retail sales series needs three monthly observations")
    current, previous, older = rows[-1], rows[-2], rows[-3]
    if not all(_monthly_adjacent(right, left) for left, right in ((older, previous), (previous, current))):
        raise RuntimeError("retail sales observations are not consecutive months")
    actual = _percent_change(current.value, previous.value)
    prior = _percent_change(previous.value, older.value)
    return [
        _update(
            metric_id="retail_sales_mom",
            period=current.period,
            indicator="零售销售月率",
            actual=f"{_fmt(actual)}%",
            previous=f"{_fmt(prior)}%",
            source="Census retail sales series via FRED",
            source_url=FRED_SOURCE_URLS["retail_sales"],
            raw={"series_id": "RSAFS", "unit": "millions of dollars, seasonally adjusted", "observations": [_json_observation(item) for item in rows[-3:]]},
        )
    ]


def _claims_update(settings: MacroNewsflashSettings) -> list[MacroUpdate]:
    rows = fetch_fred_series(settings, "ICSA")
    current, previous = rows[-1], rows[-2]
    current_date = date.fromisoformat(current.period[:10])
    previous_date = date.fromisoformat(previous.period[:10])
    if (current_date - previous_date).days != 7:
        raise RuntimeError("initial claims observations are not consecutive weeks")
    period_label = f"截至{current_date.month}月{current_date.day}日当周"
    return [
        MacroUpdate(
            metric_id="initial_claims",
            reference_period=current.period,
            period_label=period_label,
            indicator="初请失业金人数",
            actual=f"{_fmt(current.value / Decimal(10000))}万人",
            previous=f"{_fmt(previous.value / Decimal(10000))}万人",
            source_name="Department of Labor claims series via FRED",
            source_url=FRED_SOURCE_URLS["initial_claims"],
            observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
            raw_payload={"series_id": "ICSA", "unit": "persons, seasonally adjusted", "observations": [_json_observation(item) for item in rows[-2:]]},
        )
    ]


def _pce_update(settings: MacroNewsflashSettings) -> list[MacroUpdate]:
    rows = fetch_fred_series(settings, "PCEPI")
    if len(rows) < 14:
        raise RuntimeError("PCE series needs fourteen monthly observations")
    recent = rows[-14:]
    for first, second in zip(recent, recent[1:]):
        if not _monthly_adjacent(second, first):
            raise RuntimeError("PCE price index observations are not consecutive months")
    actual = _percent_change(recent[-1].value, recent[-13].value)
    previous = _percent_change(recent[-2].value, recent[-14].value)
    return [
        _update(
            metric_id="pce_yoy",
            period=recent[-1].period,
            indicator="PCE物价指数年率",
            actual=f"{_fmt(actual)}%",
            previous=f"{_fmt(previous)}%",
            source="BEA PCE price index via FRED",
            source_url=FRED_SOURCE_URLS["pce_price_index"],
            raw={"series_id": "PCEPI", "unit": "index, seasonally adjusted", "observations": [_json_observation(item) for item in recent]},
        )
    ]


def _update(
    *, metric_id: str, period: str, indicator: str, actual: str, previous: str,
    source: str, source_url: str, raw: dict[str, Any],
) -> MacroUpdate:
    return MacroUpdate(
        metric_id=metric_id,
        reference_period=period,
        period_label=_period_label(period[:7]),
        indicator=indicator,
        actual=actual,
        previous=previous,
        source_name=source,
        source_url=source_url,
        observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        raw_payload=raw,
    )


def _json_observation(item: Observation) -> dict[str, str]:
    return {"period": item.period, "value": str(item.value)}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _parse_feed_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _fetch_rate_range(url: str, timeout_seconds: float) -> tuple[Decimal, Decimal]:
    response = requests.get(url, headers={"User-Agent": "OdAIly-macro-newsflash/1.0"}, timeout=timeout_seconds)
    response.raise_for_status()
    text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    match = re.search(
        r"target range for the federal funds rate.{0,180}?\b(?:at|to|of)\s+"
        r"(\d+(?:-\d+/\d+|\.\d+)?)\s*(?:to|and)\s*"
        r"(\d+(?:-\d+/\d+|\.\d+)?)\s+percent",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("FOMC statement target range was not recognized")
    return _rate_number(match.group(1)), _rate_number(match.group(2))


def _rate_number(value: str) -> Decimal:
    if "-" not in value:
        return _decimal(value)
    whole, fraction = value.split("-", 1)
    numerator, denominator = fraction.split("/", 1)
    return _decimal(whole) + _decimal(numerator) / _decimal(denominator)
