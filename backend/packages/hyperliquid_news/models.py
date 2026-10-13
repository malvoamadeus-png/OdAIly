from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal('0')


def number(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f'Invalid numeric field: {value!r}') from exc
    if not result.is_finite():
        raise ValueError('Non-finite numeric field')
    return result


@dataclass(frozen=True)
class Rules:
    # Except the confirmed PnL/action lines, these are experiment defaults.
    large_position: Decimal = Decimal('10000000')
    oi_share: Decimal = Decimal('0.10')
    market_cap_share: Decimal = Decimal('0.10')
    major_pnl: Decimal = Decimal('5000000')
    other_pnl: Decimal = Decimal('2000000')
    action: Decimal = Decimal('3000000')
    win_rate: Decimal = Decimal('0.70')
    asset_profit: Decimal = Decimal('8000000')
    account_profit: Decimal = Decimal('20000000')
    historical_loss: Decimal = Decimal('2000000')
    losing_rounds: int = 3
    min_rounds: int = 1
    liquidation_distance: Decimal = Decimal('0.05')
    giveback_fraction: Decimal = Decimal('0.50')
    escalation: Decimal = Decimal('2')
    new_address_window_ms: int = 3600000

    def __post_init__(self) -> None:
        for key in ('large_position', 'major_pnl', 'other_pnl', 'action', 'asset_profit',
                    'account_profit', 'historical_loss'):
            if number(getattr(self, key)) <= 0:
                raise ValueError(f'{key} must be positive')
        for key in ('oi_share', 'market_cap_share', 'win_rate', 'liquidation_distance', 'giveback_fraction'):
            if not ZERO < number(getattr(self, key)) <= 1:
                raise ValueError(f'{key} must be in (0, 1]')
        if self.escalation <= 1 or min(self.min_rounds, self.losing_rounds, self.new_address_window_ms) < 1:
            raise ValueError('Invalid escalation or sample/window rule')

    def pnl_line(self, coin: str) -> Decimal:
        # Namespaced assets retain their identity, even if their ticker is BTC.
        return self.major_pnl if coin in ('BTC', 'ETH') else self.other_pnl


@dataclass(frozen=True)
class Position:
    coin: str
    size: Decimal
    notional: Decimal
    mark: Decimal
    unrealized_pnl: Decimal
    liquidation: Decimal | None

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> Position:
        size = number(raw['szi'])
        notional = abs(number(raw['positionValue']))
        if size == 0 or notional <= 0:
            raise ValueError('Position must have nonzero size and value')
        coin = str(raw['coin'])
        return cls(coin, size, notional, notional / abs(size), number(raw['unrealizedPnl']),
                   number(raw['liquidationPx']) if raw.get('liquidationPx') is not None else None)

    def facts(self) -> dict[str, Any]:
        return {key: str(value) if isinstance(value, Decimal) else value
                for key, value in vars(self).items()}

    @classmethod
    def restore(cls, raw: dict[str, Any]) -> Position:
        return cls(raw['coin'], number(raw['size']), number(raw['notional']), number(raw['mark']),
                   number(raw['unrealized_pnl']), number(raw['liquidation']) if raw['liquidation'] is not None else None)


@dataclass(frozen=True)
class Candidate:
    key: str
    observed_ms: int
    addresses: tuple[str, ...]
    coin: str
    kinds: tuple[str, ...]
    evidence: dict[str, Any] = field(default_factory=dict)
