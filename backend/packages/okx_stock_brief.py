from __future__ import annotations

import argparse
import json
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

import requests


OKX_DEFAULT_BASE_URL = "https://www.okx.com"
OKX_API_PATH = "/api/v5"
PRODUCT_CATEGORY_STOCK = "3"
MIN_COVERAGE = Decimal("0.95")
STORAGE_TICKERS = frozenset({"SNDK", "MU", "SKHYNIX", "SKHY", "SNXX", "WDC", "KIOXIA"})


class OKXStockBriefError(RuntimeError):
    pass


@dataclass(frozen=True)
class Product:
    product_type: str
    underlying: str
    inst_id: str
    list_time_ms: int | None


@dataclass(frozen=True)
class GrowthCandidate:
    window_days: int
    current_volume: Decimal | None
    previous_volume: Decimal | None
    growth_pct: Decimal | None
    current_coverage: int
    previous_coverage: int
    quality: str


@dataclass(frozen=True)
class BriefResult:
    as_of_date: date
    window_start: date
    total_volume: Decimal
    perpetual_volume: Decimal
    spot_volume: Decimal
    top_stocks: tuple[tuple[str, Decimal], ...]
    top3_share_pct: Decimal
    storage_is_leading: bool
    candidates: tuple[GrowthCandidate, ...]
    product_count: int
    failed_products: tuple[str, ...]

    def render(self) -> str:
        total = _hundred_million(self.total_volume)
        perpetual = _hundred_million(self.perpetual_volume)
        spot = _hundred_million(self.spot_volume)
        names = _format_top_stocks([item[0] for item in self.top_stocks[:3]])
        sector = "存储仍是最热门板块。" if self.storage_is_leading else "当前未形成单一主导板块。"
        as_of = f"{self.as_of_date.year} 年 {self.as_of_date.month} 月 {self.as_of_date.day} 日"
        return (
            f"据 OKX 市场数据，截至 {as_of}，"
            f"过去 30 天 OKX 美股相关产品成交额达 {total} 亿 USDT。\n\n"
            f"其中，股票永续成交额为 {perpetual} 亿 USDT，现货成交额为 {spot} 亿 USDT。\n\n"
            f"近 30 日最热门股票为 {names}，{sector}"
        )

    def to_json(self) -> dict[str, Any]:
        result = asdict(self)
        result["as_of_date"] = self.as_of_date.isoformat()
        result["window_start"] = self.window_start.isoformat()
        result["total_volume"] = str(self.total_volume)
        result["perpetual_volume"] = str(self.perpetual_volume)
        result["spot_volume"] = str(self.spot_volume)
        result["top_stocks"] = [[name, str(volume)] for name, volume in self.top_stocks]
        result["top3_share_pct"] = str(self.top3_share_pct)
        result["candidates"] = [
            {
                **asdict(candidate),
                "current_volume": _decimal_string(candidate.current_volume),
                "previous_volume": _decimal_string(candidate.previous_volume),
                "growth_pct": _decimal_string(candidate.growth_pct),
            }
            for candidate in self.candidates
        ]
        return result


def _decimal_string(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _hundred_million(value: Decimal) -> str:
    scaled = (value / Decimal("100000000")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{scaled:,.2f}"


def _format_top_stocks(names: list[str]) -> str:
    if len(names) < 2:
        return "、".join(names)
    if len(names) == 2:
        return " 和 ".join(names)
    return "、".join(names[:-1]) + " 和 " + names[-1]


def _parse_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise OKXStockBriefError(f"invalid numeric value: {value!r}") from exc


def _parse_list_time(value: Any) -> int | None:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _bjt_date_from_candle_timestamp(timestamp_ms: int) -> date:
    # OKX 1D candles are UTC+8 calendar days. The timestamp is the UTC
    # instant at the beginning of that Beijing day.
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).date() + timedelta(days=1)


class _RateLimiter:
    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self._lock = threading.Lock()
        self._last_request = 0.0

    def wait(self) -> None:
        with self._lock:
            delay = self.min_interval_seconds - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()


class OKXPublicClient:
    def __init__(
        self,
        *,
        base_url: str = OKX_DEFAULT_BASE_URL,
        timeout_seconds: float = 20,
        max_attempts: int = 4,
        request_interval_seconds: float = 0.11,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.session = session or requests.Session()
        self.rate_limiter = _RateLimiter(request_interval_seconds)

    def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]] | list[list[Any]]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                self.rate_limiter.wait()
                response = self.session.get(
                    f"{self.base_url}{OKX_API_PATH}{path}",
                    params=params,
                    headers={"Accept": "application/json", "User-Agent": "OdAIly-OKXStockBrief/1.0"},
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or str(payload.get("code")) != "0":
                    raise OKXStockBriefError(
                        f"OKX returned code={payload.get('code') if isinstance(payload, dict) else 'invalid'}"
                    )
                data = payload.get("data")
                if not isinstance(data, list):
                    raise OKXStockBriefError("OKX response data is not a list")
                return data
            except OKXStockBriefError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    time.sleep(min(float(attempt), 4.0))
        raise OKXStockBriefError(f"GET {path} failed: {last_error}")

    def instruments(self, inst_type: str) -> list[dict[str, Any]]:
        data = self._get("/public/instruments", {"instType": inst_type})
        return [item for item in data if isinstance(item, dict)]

    def history_candles(self, inst_id: str, *, limit: int = 100) -> list[list[Any]]:
        data = self._get(
            "/market/history-candles",
            {"instId": inst_id, "bar": "1D", "limit": min(max(limit, 1), 100)},
        )
        return [row for row in data if isinstance(row, list)]


def discover_products(client: OKXPublicClient) -> list[Product]:
    products: list[Product] = []
    for item in client.instruments("SWAP"):
        if (
            item.get("state") == "live"
            and item.get("instCategory") == PRODUCT_CATEGORY_STOCK
            and item.get("settleCcy") == "USDT"
            and str(item.get("instId", "")).endswith("-USDT-SWAP")
        ):
            products.append(
                Product(
                    "stock_perpetual",
                    str(item.get("instFamily", "")).removesuffix("-USDT"),
                    str(item["instId"]),
                    _parse_list_time(item.get("listTime")),
                )
            )
    for item in client.instruments("SPOT"):
        base = str(item.get("baseCcy", ""))
        if (
            item.get("state") == "live"
            and item.get("instCategory") == PRODUCT_CATEGORY_STOCK
            and item.get("quoteCcy") == "USDT"
            and base.startswith("X")
            and len(base) > 1
        ):
            products.append(Product("tokenized_spot", base[1:], str(item["instId"]), _parse_list_time(item.get("listTime"))))
    if not products:
        raise OKXStockBriefError("OKX returned no live category-3 USDT stock products")
    return sorted(products, key=lambda product: (product.product_type, product.inst_id))


def _collect_daily_volumes(
    client: OKXPublicClient,
    products: Iterable[Product],
    *,
    first_date: date,
    last_date: date,
) -> tuple[dict[date, dict[str, Decimal]], dict[tuple[str, str], dict[date, Decimal]], list[str]]:
    daily: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    by_product: dict[tuple[str, str], dict[date, Decimal]] = {}
    failures: list[str] = []
    first_timestamp = int(datetime.combine(first_date - timedelta(days=1), datetime.min.time(), UTC).timestamp() * 1000)
    last_timestamp = int(datetime.combine(last_date, datetime.min.time(), UTC).timestamp() * 1000)
    for product in products:
        try:
            values: dict[date, Decimal] = {}
            for row in client.history_candles(product.inst_id, limit=100):
                if len(row) < 9:
                    raise OKXStockBriefError(f"{product.inst_id}: malformed candle row")
                timestamp = int(row[0])
                if not first_timestamp <= timestamp < last_timestamp:
                    continue
                if str(row[8]) != "1":
                    continue
                candle_date = _bjt_date_from_candle_timestamp(timestamp)
                if first_date <= candle_date <= last_date:
                    values[candle_date] = _parse_decimal(row[7])
            by_product[(product.product_type, product.inst_id)] = values
            for candle_date, volume in values.items():
                daily[candle_date][product.product_type] += volume
                daily[candle_date][product.underlying] += volume
        except (OKXStockBriefError, TypeError, ValueError) as exc:
            failures.append(f"{product.inst_id}: {exc}")
    return daily, by_product, failures


def _window_total(daily: dict[date, dict[str, Decimal]], end_date: date, days: int) -> tuple[Decimal, int]:
    values = [daily.get(end_date - timedelta(days=index), {}) for index in range(days)]
    total = sum((value.get("stock_perpetual", Decimal(0)) + value.get("tokenized_spot", Decimal(0)) for value in values), Decimal(0))
    coverage = sum(1 for value in values if value.get("stock_perpetual", Decimal(0)) + value.get("tokenized_spot", Decimal(0)) > 0)
    return total, coverage


def _growth_candidate(daily: dict[date, dict[str, Decimal]], as_of_date: date, days: int) -> GrowthCandidate:
    current, current_coverage = _window_total(daily, as_of_date, days)
    previous_end = as_of_date - timedelta(days=days)
    previous, previous_coverage = _window_total(daily, previous_end, days)
    required = max(1, int(days * MIN_COVERAGE))
    quality = "complete" if current_coverage >= required and previous_coverage >= required and previous > 0 else "insufficient"
    growth = (current / previous - 1) * 100 if quality == "complete" else None
    return GrowthCandidate(days, current, previous, growth, current_coverage, previous_coverage, quality)


def generate_brief(
    client: OKXPublicClient,
    *,
    as_of_date: date,
    top_n: int = 3,
) -> BriefResult:
    products = discover_products(client)
    first_date = as_of_date - timedelta(days=60)
    daily, _, failures = _collect_daily_volumes(
        client,
        products,
        first_date=first_date,
        last_date=as_of_date,
    )
    if failures:
        raise OKXStockBriefError("historical data incomplete; refusing to publish: " + "; ".join(failures[:8]))
    total, coverage = _window_total(daily, as_of_date, 30)
    if coverage < 30:
        raise OKXStockBriefError(f"30-day window has only {coverage}/30 complete aggregate days")
    perpetual = sum((daily[as_of_date - timedelta(days=index)].get("stock_perpetual", Decimal(0)) for index in range(30)), Decimal(0))
    spot = sum((daily[as_of_date - timedelta(days=index)].get("tokenized_spot", Decimal(0)) for index in range(30)), Decimal(0))
    if total <= 0 or perpetual + spot != total:
        raise OKXStockBriefError("volume invariant failed: total != perpetual + spot")
    stock_totals = {
        underlying: sum(
            (daily[as_of_date - timedelta(days=index)].get(underlying, Decimal(0)) for index in range(30)),
            Decimal(0),
        )
        for values in daily.values()
        for underlying in values
        if underlying not in {"stock_perpetual", "tokenized_spot"}
    }
    top_stocks = tuple(sorted(stock_totals.items(), key=lambda item: (-item[1], item[0]))[:top_n])
    top3_share = sum((value for _, value in top_stocks), Decimal(0)) / total * 100
    storage_volume = sum((value for name, value in top_stocks if name in STORAGE_TICKERS), Decimal(0))
    storage_is_leading = len(top_stocks) >= 2 and storage_volume / sum((value for _, value in top_stocks), Decimal(0)) >= Decimal("0.5")
    return BriefResult(
        as_of_date=as_of_date,
        window_start=as_of_date - timedelta(days=30),
        total_volume=total,
        perpetual_volume=perpetual,
        spot_volume=spot,
        top_stocks=top_stocks,
        top3_share_pct=top3_share,
        storage_is_leading=storage_is_leading,
        candidates=tuple(_growth_candidate(daily, as_of_date, days) for days in (30, 7, 1)),
        product_count=len(products),
        failed_products=tuple(failures),
    )


def _default_as_of_date() -> date:
    # The current Beijing day is not complete, so the latest safe default is yesterday.
    shanghai = timezone(timedelta(hours=8))
    return datetime.now(shanghai).date() - timedelta(days=1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate the OKX US stock weekly brief from public market data.")
    parser.add_argument("--as-of-date", type=date.fromisoformat, help="Inclusive Beijing date; defaults to the latest completed day.")
    parser.add_argument("--json", action="store_true", help="Output auditable JSON instead of the three-paragraph brief.")
    parser.add_argument("--base-url", default=OKX_DEFAULT_BASE_URL, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    as_of_date = args.as_of_date or _default_as_of_date()
    result = generate_brief(OKXPublicClient(base_url=args.base_url), as_of_date=as_of_date)
    if args.json:
        print(json.dumps(result.to_json(), ensure_ascii=False, indent=2))
    else:
        print(result.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
