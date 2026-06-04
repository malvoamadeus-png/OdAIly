from __future__ import annotations

from pydantic import BaseModel

from packages.common.config import BriefKind
from packages.market.models import MarketQuote


NAME_MAP = {
    "NVDA": "NVIDIA",
    "GOOGL": "Alphabet",
    "MSFT": "Microsoft",
    "AMZN": "Amazon",
    "AVGO": "Broadcom",
    "TSM": "台积电",
    "META": "Meta",
    "MU": "Micron",
    "AMD": "AMD",
    "ASML": "ASML",
    "ORCL": "Oracle",
    "ARM": "Arm",
    "PLTR": "Palantir",
    "IBM": "IBM",
    "KLAC": "KLA",
    "DELL": "Dell",
    "MRVL": "Marvell",
    "PANW": "Palo Alto Networks",
    "ANET": "Arista Networks",
    "SAP": "SAP",
    "CRWD": "CrowdStrike",
    "ISRG": "Intuitive Surgical",
    "NOW": "ServiceNow",
    "CDNS": "Cadence",
    "ACN": "Accenture",
    "ADBE": "Adobe",
    "SNPS": "Synopsys",
    "SNOW": "Snowflake",
    "NXPI": "恩智浦",
    "TER": "Teradyne",
    "ALAB": "Astera Labs",
    "CRWV": "CoreWeave",
    "ON": "onsemi",
    "ROK": "Rockwell Automation",
    "BIDU": "百度",
    "IRM": "Iron Mountain",
    "WDAY": "Workday",
    "TWLO": "Twilio",
    "SMCI": "超微电脑",
    "SYM": "Symbotic",
    "TEAM": "Atlassian",
    "CGNX": "Cognex",
    "TTD": "The Trade Desk",
    "AVAV": "AeroVironment",
    "TEM": "Tempus AI",
    "TTEK": "Tetra Tech",
    "PATH": "UiPath",
    "EPAM": "EPAM",
    "SOUN": "SoundHound AI",
    "AMBA": "Ambarella",
}

AI_STOCK_SYMBOLS = {
    "NVDA",
    "GOOGL",
    "MSFT",
    "AMZN",
    "AVGO",
    "TSM",
    "META",
    "MU",
    "AMD",
    "ASML",
    "ORCL",
    "ARM",
    "PLTR",
    "IBM",
    "KLAC",
    "DELL",
    "MRVL",
    "PANW",
    "ANET",
    "SAP",
    "CRWD",
    "ISRG",
    "NOW",
    "CDNS",
    "ACN",
    "ADBE",
    "SNPS",
    "SNOW",
    "NXPI",
    "TER",
    "ALAB",
    "CRWV",
    "ON",
    "ROK",
    "BIDU",
    "IRM",
    "WDAY",
    "TWLO",
    "SMCI",
    "SYM",
    "TEAM",
    "CGNX",
    "TTD",
    "AVAV",
    "TEM",
    "TTEK",
    "PATH",
    "EPAM",
    "SOUN",
    "AMBA",
}

INDICES_MAP = {
    "DJI": "道指",
    "SPX": "标普 500 指数",
    "IXIC": "纳指",
    "VIX": "VIX 恐慌指数",
}

FOOTER_TEXT = (
    "据悉，MSX是一家头部RWA交易平台，累计已上线数百种 RWA 代币，"
    "涵盖 NVDA、GOOGL、MSFT、AMZN、META、TSM、AMD 等热门美股及 ETF 代币标的。"
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


def _stock_moves(quotes: list[MarketQuote], *, kind: BriefKind) -> list[tuple[MarketQuote, float]]:
    return [
        (quote, value)
        for quote in quotes
        if quote.symbol in AI_STOCK_SYMBOLS
        for value in [_value_for_kind(quote, kind)]
        if value is not None
    ]


def _trend_for_moves(moves: list[tuple[MarketQuote, float]]) -> str:
    mean = sum(value for _, value in moves) / len(moves)
    return "普涨" if mean > 0 else "普跌"


def _sorted_moves_for_trend(
    moves: list[tuple[MarketQuote, float]],
    *,
    trend: str,
) -> list[tuple[MarketQuote, float]]:
    return sorted(moves, key=lambda item: item[1], reverse=trend == "普涨")


def _top5_sentence(moves: list[tuple[MarketQuote, float]], *, kind: BriefKind, trend: str) -> str:
    if not moves:
        return ""

    top5 = _sorted_moves_for_trend(moves, trend=trend)[:5]

    parts: list[str] = []
    for quote, value in top5:
        if kind == "close":
            action = "涨" if value >= 0 else "跌"
        else:
            action = "上涨" if value >= 0 else "下跌"
        parts.append(f"{_display_name(quote)} {action} {_format_percent(value)}")
    return f"AI概念股{trend}，{'，'.join(parts)}"


def _brief_prefix(kind: BriefKind) -> str:
    if kind == "close":
        return "美股收盘"
    if kind == "open":
        return "美股开盘"
    return "美股盘前"


def _title_for_stock_moves(kind: BriefKind, moves: list[tuple[MarketQuote, float]], trend: str) -> str:
    quote, value = _sorted_moves_for_trend(moves, trend=trend)[0]
    action = "涨超" if value >= 0 else "跌超"
    return f"{_brief_prefix(kind)}AI概念股{trend}，{_display_name(quote)}{action}{_format_percent(value)}"


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

    prefix = _brief_prefix(kind)
    return f"{prefix}，{'，'.join(parts)}" if parts else prefix


def build_brief(*, kind: BriefKind, quotes: list[MarketQuote], skipped_symbols: list[str]) -> BriefPayload | None:
    valid_quotes = [
        quote
        for quote in quotes
        if quote.source_error is None and _value_for_kind(quote, kind) is not None
    ]
    if not valid_quotes:
        return None

    stock_moves = _stock_moves(valid_quotes, kind=kind)
    if not stock_moves:
        return None

    trend = _trend_for_moves(stock_moves)
    stock_sentence = _top5_sentence(stock_moves, kind=kind, trend=trend)
    title = _title_for_stock_moves(kind, stock_moves, trend)

    if kind == "premarket":
        content = f"根据 MSX.COM 数据，美股盘前{stock_sentence}。\n{FOOTER_TEXT}"
    else:
        content = f"根据 MSX.COM 数据，{_index_sentence(valid_quotes, kind=kind)}。{stock_sentence}。\n{FOOTER_TEXT}"

    return BriefPayload(
        title=title,
        content=content,
        isPublish=False,
        used_symbols=[quote.symbol for quote in valid_quotes],
        skipped_symbols=skipped_symbols,
    )
