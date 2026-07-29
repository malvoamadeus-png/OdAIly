from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DefaultSymbol:
    symbol: str
    display_name: str
    threshold: str
    price_precision: int
    unit: str


DEFAULT_SYMBOLS = (
    DefaultSymbol("EUSTX50", "欧洲斯托克50", "100", 2, "点"),
    DefaultSymbol("UK100", "英国富时100", "150", 2, "点"),
    DefaultSymbol("GER40", "德国DAX 40", "400", 2, "点"),
    DefaultSymbol("XBRUSD", "布伦特原油", "2", 2, "美元/桶"),
    DefaultSymbol("USDJPY", "美元兑日元", "0.5", 3, ""),
    DefaultSymbol("USDCNH", "美元兑人民币", "0.010", 5, ""),
    DefaultSymbol("XAUUSD", "黄金", "50", 2, "美元/盎司"),
    DefaultSymbol("XAGUSD", "白银", "3", 3, "美元/盎司"),
)


DEFAULT_TEMPLATES = (
    (
        "breakout",
        "上涨突破",
        "{display_name}上涨突破{trigger_price}{unit}，{title_change_clause}",
        "据Gate数据，{display_name}（{symbol}）上涨突破{trigger_price}{unit}，"
        "现报{current_price}{unit}，{body_change_clause}",
    ),
    (
        "pullback",
        "短时回调",
        "{display_name}短时回调至{trigger_price}{unit}，{title_change_clause}",
        "据Gate数据，{display_name}（{symbol}）短时回调至{trigger_price}{unit}，"
        "现报{current_price}{unit}，{body_change_clause}",
    ),
    (
        "decline",
        "下跌至",
        "{display_name}下跌至{trigger_price}{unit}，{title_change_clause}",
        "据Gate数据，{display_name}（{symbol}）下跌至{trigger_price}{unit}，"
        "现报{current_price}{unit}，{body_change_clause}",
    ),
    (
        "rebound",
        "短时反弹",
        "{display_name}短时反弹至{trigger_price}{unit}，{title_change_clause}",
        "据Gate数据，{display_name}（{symbol}）短时反弹至{trigger_price}{unit}，"
        "现报{current_price}{unit}，{body_change_clause}",
    ),
)
