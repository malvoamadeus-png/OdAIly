from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import requests

from .models import MarketObservation


class PredictFunApiError(RuntimeError):
    pass


class PredictFunClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
        page_size: int,
        max_pages: int,
    ) -> None:
        if not api_key.strip():
            raise ValueError("PREDICT_FUN_API_KEY is required")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.page_size = page_size
        self.max_pages = max_pages
        self.session = requests.Session()
        self.session.headers.update(
            {
                "x-api-key": api_key.strip(),
                "Accept": "application/json",
                "User-Agent": "OdAIly-predict-market-discovery/1.0",
            }
        )

    def fetch_open_categories(self) -> tuple[list[dict[str, Any]], int, bool]:
        categories: list[dict[str, Any]] = []
        after: str | None = None
        seen_cursors: set[str] = set()
        page_count = 0
        page_limit_reached = False
        for _ in range(self.max_pages):
            params: dict[str, Any] = {
                "first": self.page_size,
                "sort": "PUBLISHED_AT_DESC",
                "status": "OPEN",
            }
            if after:
                params["after"] = after
            payload = self._get_json("/v1/categories", params=params)
            page = payload.get("data")
            if not isinstance(page, list):
                raise PredictFunApiError("Predict.fun categories response has no data list")
            categories.extend(item for item in page if isinstance(item, dict))
            page_count += 1
            cursor = payload.get("cursor")
            if not cursor or not page:
                break
            after = str(cursor)
            if after in seen_cursors:
                raise PredictFunApiError("Predict.fun returned a repeated pagination cursor")
            seen_cursors.add(after)
            page_limit_reached = page_count == self.max_pages
        return categories, page_count, page_limit_reached

    def _get_json(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        for attempt in range(3):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout_seconds)
            except requests.RequestException as exc:
                if attempt == 2:
                    raise PredictFunApiError(f"Predict.fun request failed: {exc}") from exc
                time.sleep(2**attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 2:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(60.0, max(1.0, float(retry_after))) if retry_after else 2**attempt
                except ValueError:
                    delay = 2**attempt
                time.sleep(delay)
                continue
            if not response.ok:
                raise PredictFunApiError(
                    f"Predict.fun returned HTTP {response.status_code} for {path}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise PredictFunApiError("Predict.fun response was not valid JSON") from exc
            if not isinstance(payload, dict) or payload.get("success") is False:
                raise PredictFunApiError("Predict.fun returned an unsuccessful API response")
            return payload
        raise PredictFunApiError("Predict.fun request failed after retries")


def flatten_categories(categories: list[dict[str, Any]]) -> list[MarketObservation]:
    observations: list[MarketObservation] = []
    for category in categories:
        category_id = _string(category.get("id"))
        category_slug = _string(category.get("slug"))
        category_title = _string(category.get("title"))
        market_variant = _string(category.get("marketVariant"))
        for market in _list(category.get("markets")):
            if not isinstance(market, dict) or market.get("id") is None:
                continue
            question = _string(market.get("question") or market.get("title"))
            variant = _string(market.get("marketVariant")) or market_variant
            observations.append(
                MarketObservation(
                    market_id=_string(market.get("id")),
                    category_id=category_id,
                    category_title=category_title,
                    category_slug=category_slug,
                    category_published_at=_normalize_time(category.get("publishedAt")),
                    starts_at=_normalize_time(category.get("startsAt")),
                    ends_at=_normalize_time(category.get("endsAt")),
                    market_created_at=_normalize_time(market.get("createdAt")),
                    question=question,
                    market_variant=variant,
                    market_status=_string(market.get("status")).upper(),
                    trading_status=_string(market.get("tradingStatus")).upper(),
                    market_url=(f"https://predict.fun/market/{category_slug}" if category_slug else ""),
                    filter_reason=market_filter_reason(variant, question, category_title),
                )
            )
    return observations


def market_filter_reason(variant: str, question: str, category_title: str) -> str | None:
    normalized_variant = variant.strip().upper()
    if normalized_variant in {"CRYPTO_UP_DOWN", "UP_DOWN"}:
        return "repetitive_up_down_template"
    searchable = f"{question} {category_title}".casefold()
    if "up or down" in searchable:
        return "repetitive_up_down_template"
    if any(
        token in searchable
        for token in (
            "weather",
            "temperature",
            "rainfall",
            "precipitation",
            "snowfall",
            "天气",
            "气温",
            "降雨",
            "降雪",
        )
    ):
        return "weather_template"
    return None


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, list):
            return data
        edges = value.get("edges")
        if isinstance(edges, list):
            return [edge.get("node") for edge in edges if isinstance(edge, dict)]
    return []


def _string(value: Any) -> str:
    return "" if value is None else str(value)


def _normalize_time(value: Any) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")
