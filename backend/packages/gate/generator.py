from __future__ import annotations

from pydantic import BaseModel

from .client import BASELINE_FLOOR_PERCENT
from .models import GateAssetQuote


class GateBriefPayload(BaseModel):
    title: str
    content: str
    isPublish: bool = False
    used_symbols: list[str]
    skipped_symbols: list[str]


TITLE_CANDIDATES = ("XAUUSD", "XAGUSD", "USDCNH", "USDJPY", "XTIUSD")
FX_CANDIDATES = ("USDCNH", "USDJPY")

UNIT_BY_SYMBOL = {
    "XAUUSD": "美元/盎司",
    "XAGUSD": "美元/盎司",
    "XTIUSD": "美元/桶",
    "XBRUSD": "美元/桶",
}

TITLE_NAME_BY_SYMBOL = {
    "XAUUSD": "黄金",
    "XAGUSD": "白银",
    "USDCNH": "美元兑离岸人民币",
    "USDJPY": "美元兑日元",
    "XTIUSD": "WTI原油",
}


def _valid(quote: GateAssetQuote | None) -> bool:
    return quote is not None and quote.error is None and quote.price is not None and quote.change_percent is not None


def _action(value: float | None) -> str:
    if value is None:
        return "波动"
    return "上涨" if value >= 0 else "下跌"


def _trend(value: float | None) -> str:
    if value is None:
        return "涨跌幅"
    return "涨幅" if value >= 0 else "跌幅"


def _simple_trend(value: float | None) -> str:
    if value is None:
        return "变动"
    return "涨" if value >= 0 else "跌"


def _percent(value: float | None, *, decimals: int = 2) -> str:
    if value is None:
        return "---"
    text = f"{abs(value):.{decimals}f}".rstrip("0").rstrip(".")
    return f"{text}%"


def _price(value: float | None) -> str:
    if value is None:
        return "---"
    return f"{value:g}"


def _baseline(quote: GateAssetQuote) -> float:
    raw = quote.raw or {}
    value = raw.get("baseline_abs_change_percent")
    try:
        return max(BASELINE_FLOOR_PERCENT, float(value))
    except (TypeError, ValueError):
        return BASELINE_FLOOR_PERCENT


def _score(quote: GateAssetQuote) -> float:
    if quote.change_percent is None:
        return -1
    return abs(quote.change_percent) / _baseline(quote)


def _best_title_quote(quotes: dict[str, GateAssetQuote]) -> GateAssetQuote | None:
    candidates: list[GateAssetQuote] = []
    for symbol in ("XAUUSD", "XAGUSD", "XTIUSD"):
        quote = quotes.get(symbol)
        if _valid(quote):
            candidates.append(quote)

    fx_quotes = [quotes.get(symbol) for symbol in FX_CANDIDATES]
    valid_fx = [quote for quote in fx_quotes if _valid(quote)]
    if valid_fx:
        candidates.append(max(valid_fx, key=_score))

    if not candidates:
        return None
    return max(candidates, key=_score)


def _title_for(quote: GateAssetQuote) -> str:
    name = TITLE_NAME_BY_SYMBOL.get(quote.symbol, quote.display_name)
    unit = UNIT_BY_SYMBOL.get(quote.symbol, "")
    suffix = f"{_price(quote.price)}{unit}" if unit else _price(quote.price)
    return f"{name}{_action(quote.change_percent)}{_percent(quote.change_percent, decimals=1)}，报{suffix}"


def _line_quote(quotes: dict[str, GateAssetQuote], symbol: str) -> GateAssetQuote:
    return quotes.get(
        symbol,
        GateAssetQuote(
            symbol=symbol,
            display_name=symbol,
            source="missing",
            source_symbol=symbol,
            error="missing",
        ),
    )


def _missing_text(quote: GateAssetQuote) -> str:
    return "暂未获取到数据"


def _quote_values(quote: GateAssetQuote) -> tuple[str, str, str, str, str]:
    if not _valid(quote):
        return (_missing_text(quote), "---", "波动", "涨跌幅", "变动")
    return (
        _price(quote.price),
        _percent(quote.change_percent),
        _action(quote.change_percent),
        _trend(quote.change_percent),
        _simple_trend(quote.change_percent),
    )


def build_gate_brief(*, quotes: dict[str, GateAssetQuote]) -> GateBriefPayload | None:
    title_quote = _best_title_quote(quotes)
    if title_quote is None:
        return None

    gold = _line_quote(quotes, "XAUUSD")
    silver = _line_quote(quotes, "XAGUSD")
    bvix = _line_quote(quotes, "BVIXUSDT")
    evix = _line_quote(quotes, "EVIXUSDT")
    usdcnh = _line_quote(quotes, "USDCNH")
    usdjpy = _line_quote(quotes, "USDJPY")
    wti = _line_quote(quotes, "XTIUSD")
    brent = _line_quote(quotes, "XBRUSD")
    stoxx50 = _line_quote(quotes, "EUSTX50")
    ftse100 = _line_quote(quotes, "UK100")
    dax40 = _line_quote(quotes, "GER40")

    gold_price, gold_change, gold_action, gold_trend, gold_simple = _quote_values(gold)
    silver_price, silver_change, _silver_action, silver_trend, _silver_simple = _quote_values(silver)
    bvix_price, bvix_change, _bvix_action, bvix_trend, _bvix_simple = _quote_values(bvix)
    evix_price, evix_change, _evix_action, evix_trend, _evix_simple = _quote_values(evix)
    usdcnh_price, usdcnh_change, usdcnh_action, _usdcnh_trend, _usdcnh_simple = _quote_values(usdcnh)
    usdjpy_price, usdjpy_change, usdjpy_action, _usdjpy_trend, _usdjpy_simple = _quote_values(usdjpy)
    wti_price, wti_change, wti_action, _wti_trend, _wti_simple = _quote_values(wti)
    brent_price, brent_change, brent_action, _brent_trend, _brent_simple = _quote_values(brent)
    stoxx50_price, stoxx50_change, stoxx50_action, _stoxx50_trend, _stoxx50_simple = _quote_values(stoxx50)
    ftse100_price, ftse100_change, ftse100_action, _ftse100_trend, _ftse100_simple = _quote_values(ftse100)
    dax40_price, dax40_change, dax40_action, _dax40_trend, _dax40_simple = _quote_values(dax40)

    content = (
        f"据 Gate 最新数据，黄金价格{gold_action}至 {gold_price} 美元/盎司，"
        f"日内{gold_trend}达 {gold_change}。白银价格{gold_simple}至 {silver_price} 美元/盎司，"
        f"日内{silver_trend}达 {silver_change}。\n\n"
        f"BVIX（BTC 波动率指数）最新报价 {bvix_price}，日内{bvix_trend}为 {bvix_change}。"
        f"EVIX（ETH 波动率指数）最新报价 {evix_price}，日内{evix_trend}为 {evix_change}。\n\n"
        f"外汇方面，美元兑离岸人民币（USD/CNH）日内{usdcnh_action} {usdcnh_change}，当前汇率为 {usdcnh_price}。"
        f"美元兑日元（USD/JPY）日内{usdjpy_action} {usdjpy_change}，当前汇率为 {usdjpy_price}。\n\n"
        f"全球股指方面，欧洲50指数（EUSTX50）日内{stoxx50_action} {stoxx50_change}，报 {stoxx50_price} 点；"
        f"英国富时100指数（UK100）日内{ftse100_action} {ftse100_change}，报 {ftse100_price} 点；"
        f"德国DAX40指数（GER40）日内{dax40_action} {dax40_change}，报 {dax40_price} 点。\n\n"
        f"大宗商品方面，WTI 原油日内{wti_action} {wti_change}，报 {wti_price} 美元/桶。"
        f"布伦特原油日内{brent_action} {brent_change}，报 {brent_price} 美元/桶。\n\n"
        "Gate 支持用户在平台内直接交易传统金融市场产品，一站式覆盖贵金属、外汇、全球股票差价合约（CFD）、"
        "重要指数及大宗商品等多类资产，实现加密资产与传统金融资产的深度融合。Gate TradFi 相关功能已全面集成至 "
        "Gate App 及 Web 端，用户无需切换平台，即可便捷参与全球资产价格交易，在加密市场之外解锁更多策略与机会，"
        "持续提升多元资产配置体验。"
    )

    skipped = [symbol for symbol, quote in quotes.items() if not _valid(quote)]
    used = [symbol for symbol, quote in quotes.items() if _valid(quote)]
    return GateBriefPayload(
        title=_title_for(title_quote),
        content=content,
        isPublish=False,
        used_symbols=used,
        skipped_symbols=skipped,
    )
