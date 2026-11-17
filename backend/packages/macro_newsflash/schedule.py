from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as wall_time, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from packages.common.time_utils import us_market_holidays

from .settings import MacroNewsflashSettings


EASTERN_TZ = ZoneInfo("America/New_York")
FULL_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
MONTHS = {
    alias.lower(): number
    for number, name in enumerate(FULL_MONTHS, start=1)
    for alias in (name, name[:3])
}
MONTH_PATTERN = "|".join(sorted(MONTHS, key=len, reverse=True))


@dataclass(frozen=True, slots=True)
class ScheduledRelease:
    release_id: str
    source_name: str
    title: str
    metric_ids: tuple[str, ...]
    scheduled_at: str
    source_url: str
    is_estimate: bool = False


def fetch_release_schedule(
    settings: MacroNewsflashSettings,
    *,
    now: datetime | None = None,
) -> tuple[list[ScheduledRelease], dict[str, str], set[str]]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    sources = (
        (
            "BLS",
            settings.bls_schedule_url,
            ("employment situation", "consumer price index"),
            {"employment situation": ("bls_employment", ("nonfarm_payrolls", "unemployment_rate")),
             "consumer price index": ("bls_cpi", ("cpi_yoy_nsa", "cpi_mom_nsa", "cpi_mom_sa", "core_cpi_mom_sa"))},
        ),
        (
            "Census",
            settings.census_schedule_url,
            ("advance monthly sales", "retail sales"),
            {"advance monthly sales": ("census_retail_sales", ("retail_sales_mom",)),
             "retail sales": ("census_retail_sales", ("retail_sales_mom",))},
        ),
        (
            "BEA",
            settings.bea_schedule_url,
            ("personal income and outlays",),
            {"personal income and outlays": ("bea_pce", ("pce_yoy",))},
        ),
    )
    releases: list[ScheduledRelease] = []
    errors: dict[str, str] = {}
    successful_sources: set[str] = set()
    for source_name, url, needles, mapping in sources:
        try:
            response = requests.get(
                url,
                headers={"User-Agent": "OdAIly-macro-newsflash/1.0"},
                timeout=settings.request_timeout_seconds,
            )
            response.raise_for_status()
            parsed = _parse_release_rows(
                response.text,
                source_name=source_name,
                source_url=url,
                needles=needles,
                mapping=mapping,
                now=current,
            )
            if not parsed:
                raise ValueError("calendar returned no matching future releases")
            successful_sources.add(source_name)
            releases.extend(parsed)
        except Exception as exc:
            errors[source_name] = str(exc)
    try:
        response = requests.get(
            settings.fed_calendar_url,
            headers={"User-Agent": "OdAIly-macro-newsflash/1.0"},
            timeout=settings.request_timeout_seconds,
        )
        response.raise_for_status()
        parsed = _parse_fed_calendar(response.text, settings.fed_calendar_url, now=current)
        if not parsed:
            raise ValueError("FOMC calendar returned no matching future meetings")
        successful_sources.add("Federal Reserve")
        releases.extend(parsed)
    except Exception as exc:
        errors["Federal Reserve"] = str(exc)
    try:
        parsed = _fetch_tradingview_calendar(settings, now=current)
        successful_sources.update({"TradingView · Census Bureau", "TradingView · Department of Labor"})
        releases.extend(parsed)
        errors.pop("TradingView", None)
    except Exception as exc:
        errors["TradingView"] = str(exc)
    releases.extend(_next_claims_schedule(current))
    successful_sources.add("DOL ETA (estimated weekly schedule)")
    unique: dict[str, ScheduledRelease] = {}
    for release in releases:
        existing = unique.get(release.release_id)
        if existing is None or (existing.is_estimate and not release.is_estimate):
            unique[release.release_id] = release
        elif release.source_name == "Census" and existing.source_name != "Census":
            unique[release.release_id] = release
    return sorted(unique.values(), key=lambda item: item.scheduled_at), errors, successful_sources


def _fetch_tradingview_calendar(
    settings: MacroNewsflashSettings,
    *,
    now: datetime,
) -> list[ScheduledRelease]:
    end = now + timedelta(days=120)
    response = requests.get(
        settings.tradingview_calendar_url,
        params={
            "from": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "to": end.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "countries": "US",
        },
        headers={
            "Accept": "application/json",
            "Origin": "https://www.tradingview.com",
            "Referer": "https://www.tradingview.com/",
            "User-Agent": "OdAIly-macro-newsflash/1.0",
        },
        timeout=settings.request_timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "ok" or not isinstance(payload.get("result"), list):
        raise ValueError("TradingView calendar returned an unexpected response")

    result: list[ScheduledRelease] = []
    for item in payload["result"]:
        if not isinstance(item, dict) or item.get("country") != "US":
            continue
        title = str(item.get("title") or "")
        source = str(item.get("source") or "")
        normalized_title = re.sub(r"\s+", " ", title).strip().lower()
        normalized_source = source.lower()
        if normalized_title == "retail sales mom" and "census" in normalized_source:
            metric_id = "retail_sales_mom"
            release_id_prefix = "census_retail_sales"
            source_name = "TradingView · Census Bureau"
            fallback_url = "https://www.census.gov/economic-indicators/calendar-listview.html"
        elif normalized_title == "initial jobless claims" and "department of" in normalized_source and "labour" in normalized_source:
            metric_id = "initial_claims"
            release_id_prefix = "dol_initial_claims"
            source_name = "TradingView · Department of Labor"
            fallback_url = "https://oui.doleta.gov/unemploy/claims.asp"
        else:
            continue
        try:
            scheduled_at = datetime.fromisoformat(str(item["date"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"TradingView calendar event has an invalid date: {title}") from exc
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=UTC)
        scheduled_at = scheduled_at.astimezone(UTC)
        if scheduled_at < now - timedelta(hours=4):
            continue
        event_day = scheduled_at.astimezone(EASTERN_TZ).date().isoformat()
        source_url = str(item.get("source_url") or fallback_url)
        if urlparse(source_url).scheme not in {"http", "https"}:
            source_url = fallback_url
        metric_ids = (metric_id,)
        result.append(
            ScheduledRelease(
                release_id=f"{release_id_prefix}:{event_day}",
                source_name=source_name,
                title=title,
                metric_ids=metric_ids,
                scheduled_at=scheduled_at.isoformat(timespec="seconds"),
                source_url=source_url,
            )
        )
    if not result:
        raise ValueError("TradingView calendar returned no matching Census retail sales or DOL claims dates")
    return result


def _parse_release_rows(
    html: str,
    *,
    source_name: str,
    source_url: str,
    needles: tuple[str, ...],
    mapping: dict[str, tuple[str, tuple[str, ...]]],
    now: datetime,
) -> list[ScheduledRelease]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[ScheduledRelease] = []
    seen: set[tuple[str, str]] = set()
    for row in soup.find_all(("tr", "li", "article")):
        text = re.sub(r"\s+", " ", row.get_text(" ", strip=True))
        lowered = text.lower()
        needle = next((candidate for candidate in needles if candidate in lowered), None)
        if not needle:
            continue
        date_value = _find_calendar_date(text, now=now)
        if date_value is None:
            continue
        event_id, metric_ids = mapping[needle]
        event_time = _find_calendar_time(text, default=wall_time(8, 30))
        scheduled_at = datetime.combine(date_value, event_time, tzinfo=EASTERN_TZ)
        if scheduled_at.astimezone(UTC) < now - timedelta(hours=4):
            continue
        dedupe = (event_id, date_value.isoformat())
        if dedupe in seen:
            continue
        seen.add(dedupe)
        result.append(
            ScheduledRelease(
                release_id=f"{event_id}:{date_value.isoformat()}",
                source_name=source_name,
                title=text[:240],
                metric_ids=metric_ids,
                scheduled_at=scheduled_at.astimezone(UTC).isoformat(timespec="seconds"),
                source_url=source_url,
            )
        )
    return result


def _find_calendar_date(text: str, *, now: datetime) -> date | None:
    iso_match = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text)
    if iso_match:
        return date(*(int(value) for value in iso_match.groups()))
    numeric_match = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", text)
    if numeric_match:
        month, day, year = (int(value) for value in numeric_match.groups())
        return date(year, month, day)
    month_match = re.search(rf"\b({MONTH_PATTERN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s*(20\d{{2}})?\b", text, re.IGNORECASE)
    if month_match:
        month = MONTHS[month_match.group(1).lower()]
        day = int(month_match.group(2))
        year = int(month_match.group(3)) if month_match.group(3) else now.astimezone(EASTERN_TZ).year
        candidate = date(year, month, day)
        if month_match.group(3) is None and candidate < now.astimezone(EASTERN_TZ).date() - timedelta(days=2):
            candidate = date(year + 1, month, day)
        return candidate
    return None


def _find_calendar_time(text: str, *, default: wall_time) -> wall_time:
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])", text, re.IGNORECASE)
    if not match:
        return default
    hour = int(match.group(1)) % 12
    if match.group(3).lower().startswith("p"):
        hour += 12
    return wall_time(hour, int(match.group(2) or "0"))


def _parse_fed_calendar(html: str, source_url: str, *, now: datetime) -> list[ScheduledRelease]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[ScheduledRelease] = []
    range_pattern = re.compile(rf"\b({MONTH_PATTERN})\.?\s+(\d{{1,2}})\s*[-–]\s*(\d{{1,2}})(?:,?\s*(20\d{{2}}))?", re.IGNORECASE)
    for table in soup.find_all("table"):
        text = re.sub(r"\s+", " ", table.get_text(" ", strip=True))
        year_matches = list(re.finditer(r"\b(20\d{2})\b", text))
        for match in range_pattern.finditer(text):
            year_text = match.group(4)
            if year_text:
                year = int(year_text)
            else:
                nearby = [item for item in year_matches if abs(item.start() - match.start()) <= 120]
                if not nearby:
                    continue
                year = int(min(nearby, key=lambda item: abs(item.start() - match.start())).group(1))
            month = MONTHS[match.group(1).lower()]
            meeting_day = date(year, month, int(match.group(3)))
            scheduled_at = datetime.combine(meeting_day, wall_time(14, 0), tzinfo=EASTERN_TZ)
            if scheduled_at.astimezone(UTC) < now - timedelta(hours=4):
                continue
            result.append(
                ScheduledRelease(
                    release_id=f"fomc:{meeting_day.isoformat()}",
                    source_name="Federal Reserve",
                    title=f"FOMC meeting statement expected {meeting_day.isoformat()}",
                    metric_ids=("fomc_rate",),
                    scheduled_at=scheduled_at.astimezone(UTC).isoformat(timespec="seconds"),
                    source_url=source_url,
                )
            )
    return result


def _next_claims_schedule(now: datetime) -> list[ScheduledRelease]:
    eastern_now = now.astimezone(EASTERN_TZ)
    days_until_thursday = (3 - eastern_now.weekday()) % 7
    thursday = eastern_now.date() + timedelta(days=days_until_thursday)
    for _ in range(3):
        release_date = thursday - timedelta(days=1) if thursday in us_market_holidays(thursday.year) else thursday
        scheduled_at = datetime.combine(release_date, wall_time(8, 30), tzinfo=EASTERN_TZ)
        if scheduled_at > eastern_now:
            break
        thursday += timedelta(days=7)
    return [
        ScheduledRelease(
            release_id=f"dol_initial_claims:{release_date.isoformat()}",
            source_name="DOL ETA (estimated weekly schedule)",
            title="Unemployment Insurance Weekly Claims",
            metric_ids=("initial_claims",),
            scheduled_at=scheduled_at.astimezone(UTC).isoformat(timespec="seconds"),
            source_url="https://oui.doleta.gov/unemploy/claims.asp",
            is_estimate=True,
        )
    ]
