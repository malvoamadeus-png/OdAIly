from __future__ import annotations

from pydantic import BaseModel

from packages.common.config import BriefKind
from packages.market.models import MarketQuote


NAME_MAP = {
    "MSTR": "Strategy",
    "COIN": "Coinbase",
    "SBET": "Sharplink Gaming",
    "BMNR": "BitMine",
    "CRCL": "Circle",
    "ALTS": "ALT5 Sigma",
    "HUT": "Hut 8",
    "BNC": "BNC",
    "ABTC": "American Bitcoin",
    "ETHZ": "ETHZilla",
    "HODL": "Sol Strategies",
    "BTBT": "Bit Digital",
    "HOOD": "Robinhood",
    "RIOT": "Riot Platforms",
    "MARA": "MARA Holdings",
    "BTCS": "BTCS",
}

INDICES_MAP = {
    "DJI": "道指",
    "SPX": "标普 500 指数",
    "IXIC": "纳指",
    "VIX": "VIX 恐慌指数",
}

FOOTER_TEXT = (
    "据悉，msx.com 是一个去中心化 RWA 交易平台，累计已上线数百种 RWA 代币，"
    "涵盖 AAPL、AMZN、GOOGL、META、MSFT、NFLX、NVDA 等美股及 ETF 代币标的。"
)


class BriefPayload(BaseModel):
    title: str
    content: str
    isPublish: bool = False
    used_symbols: list[str]
    skipped_symbols: list[str]


def _display_name(quote: MarketQuote) -> str:
    return NAME_MAP.get(quote.symbol, quote.display_name or quote.symbol)


def _value_for_kind(quote: MarketQuote, kind: BriefKind) -> float | None:
    if kind == "premarket":
        return quote.pre_market_change_percent
    return quote.regular_market_change_percent


def _format_percent(value: float) -> str:
    return f"{abs(value):.2f}".rstrip("0").rstrip(".") + "%"


def _top5_sentence(quotes: list[MarketQuote], *, kind: BriefKind) -> str:
    items = [(quote, _value_for_kind(quote, kind)) for quote in quotes if quote.symbol not in INDICES_MAP]
    valid = [(quote, value) for quote, value in items if value is not None]
    if not valid:
        return ""

    mean = sum(value for _, value in valid) / len(valid)
    trend = "普涨" if mean > 0 else "普跌"
    sorted_items = sorted(valid, key=lambda item: item[1], reverse=mean > 0)
    top5 = sorted_items[:5]

    parts: list[str] = []
    for quote, value in top5:
        if kind == "close":
            action = "涨" if value >= 0 else "跌"
        else:
            action = "上涨" if value >= 0 else "下跌"
        parts.append(f"{_display_name(quote)} {action} {_format_percent(value)}")
    return f"加密概念股{trend}，{'，'.join(parts)}"


def _index_sentence(quotes: list[MarketQuote], *, kind: BriefKind) -> str:
    parts: list[str] = []
    by_symbol = {quote.symbol: quote for quote in quotes}
    for symbol in ("DJI", "SPX", "IXIC", "VIX"):
        quote = by_symbol.get(symbol)
        if quote is None:
            continue
        value = _value_for_kind(quote, kind)
        if value is None:
            continue
        if kind == "close":
            action = "收涨" if value >= 0 else "收跌"
        else:
            action = "涨" if value >= 0 else "跌"
        parts.append(f"{INDICES_MAP[symbol]}{action} {_format_percent(value)}")

    if kind == "close":
        prefix = "美股收盘"
    elif kind == "open":
        prefix = "美股开盘"
    else:
        prefix = "美股盘前"
    return f"{prefix}，{'，'.join(parts)}" if parts else prefix


def build_brief(*, kind: BriefKind, quotes: list[MarketQuote], skipped_symbols: list[str]) -> BriefPayload | None:
    valid_quotes = [
        quote
        for quote in quotes
        if quote.source_error is None and _value_for_kind(quote, kind) is not None
    ]
    if not valid_quotes:
        return None

    stock_sentence = _top5_sentence(valid_quotes, kind=kind)
    if not stock_sentence:
        return None

    if kind == "premarket":
        title = "美股盘前加密概念股快讯"
        content = f"根据 msx.com 数据，美股盘前{stock_sentence}。\n{FOOTER_TEXT}"
    elif kind == "open":
        title = "美股开盘加密概念股快讯"
        content = f"根据 msx.com 数据，{_index_sentence(valid_quotes, kind=kind)}。{stock_sentence}。\n{FOOTER_TEXT}"
    else:
        title = "美股收盘加密概念股快讯"
        content = f"根据 msx.com 数据，{_index_sentence(valid_quotes, kind=kind)}。{stock_sentence}。\n{FOOTER_TEXT}"

    return BriefPayload(
        title=title,
        content=content,
        isPublish=False,
        used_symbols=[quote.symbol for quote in valid_quotes],
        skipped_symbols=skipped_symbols,
    )
