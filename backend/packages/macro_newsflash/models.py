from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class MacroUpdate:
    metric_id: str
    reference_period: str
    period_label: str
    indicator: str
    actual: str
    previous: str
    source_name: str
    source_url: str
    observed_at: str
    raw_payload: dict[str, Any] = field(default_factory=dict)
    rate_action: str | None = None

    @property
    def event_key(self) -> str:
        return f"{self.metric_id}:{self.reference_period}"

    def headline(self) -> str:
        if self.metric_id == "fomc_rate":
            action = self.rate_action or "公布"
            location = "在" if action == "维持" else "至"
            return f"美国{self.period_label}利率决议{action}联邦基金利率目标区间{location}{self.actual}，前值{self.previous}"
        return f"美国{self.period_label}{self.indicator}录得{self.actual}，前值{self.previous}"
