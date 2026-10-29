"""Strategy interface. Strategies are pure functions of a causal view of the
market: no I/O, no clocks, no broker calls. The same code runs live and in the
backtester."""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from app.core.types import TradePlan
from app.features.frame import FeatureFrame


@dataclass
class StrategyView:
    m15: FeatureFrame
    i: int                    # index of the last CLOSED M15 bar
    h1: FeatureFrame
    k: int                    # index of the last H1 bar closed at/before m15 bar i closed
    h4: Optional[FeatureFrame] = None
    q: Optional[int] = None   # index of the last H4 bar closed at/before m15 bar i closed
    symbol: str = ""
    m1: Optional[FeatureFrame] = None
    j: Optional[int] = None   # index of the last CLOSED M1 bar
    point: float = 0.001
    spread_price: float = 0.0      # current (or historical) spread in price units
    bid: float = 0.0               # reference bid for entry pricing
    ask: float = 0.0
    regime_trend: str = "UNKNOWN"
    session: str = ""
    spread_safe: bool = False
    spread_why: str = ""           # why the spread was judged unsafe (shown in the evaluation reason)
    min_stop_distance: float = 0.0

    def close_time(self) -> int:
        return int(self.m15.t[self.i]) + 900


@dataclass
class EvalResult:
    plan: Optional[TradePlan] = None
    reasons: List[str] = field(default_factory=list)   # why NOT (diagnostics for the dashboard)


def fin(*vals) -> bool:
    return all(v is not None and np.isfinite(v) for v in vals)


class Strategy(ABC):
    id: str = "base"

    def __init__(self, sid: str, version: str, params: Dict[str, Any], exit_model: Dict[str, Any]):
        self.id = sid
        self.version = version
        self.params = dict(params)
        self.exit_model = dict(exit_model)
        self.recent_setups: Dict[str, int] = {}   # setup_key -> bar time first seen

    @property
    def params_hash(self) -> str:
        blob = json.dumps({"p": self.params, "e": self.exit_model}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @abstractmethod
    def lookbacks(self) -> Dict[str, int]:
        """Bars needed per timeframe, e.g. {"H1": 200, "M15": 60}."""

    def min_history(self) -> Dict[str, int]:
        """Bars without which the symbol must NOT start. Defaults to `lookbacks()`. A strategy that degrades to
        INSUFFICIENT_HISTORY on its own can ask hydration for more (`lookbacks`) while requiring less here, so one
        thin history (a newly listed CFD) cannot stop every other strategy on that symbol."""
        return self.lookbacks()

    @abstractmethod
    def evaluate(self, v: StrategyView) -> EvalResult: ...

    # Signal lifecycle: one market condition -> at most one signal.
    def is_duplicate_setup(self, key: str, bar_time: int, cooldown_seconds: int) -> bool:
        first = self.recent_setups.get(key)
        if first is not None and bar_time - first < max(cooldown_seconds, 86400):
            return True
        return False

    def remember_setup(self, key: str, bar_time: int) -> None:
        self.recent_setups[key] = bar_time
        if len(self.recent_setups) > 500:
            for k in sorted(self.recent_setups, key=self.recent_setups.get)[:250]:
                self.recent_setups.pop(k, None)
