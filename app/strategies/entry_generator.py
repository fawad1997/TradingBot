"""Pure closed-candle trigger and relative tick-volume confirmation helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.core.types import Side
from app.features.frame import FeatureFrame


@dataclass(frozen=True)
class EntryCandidate:
    asset_name: str
    direction: Side
    initial_entry_price: float
    pattern: str
    relative_volume: float

    def as_dict(self) -> dict:
        return {
            "asset_name": self.asset_name,
            "direction": str(self.direction),
            "initial_entry_price": self.initial_entry_price,
            "pattern": self.pattern,
            "relative_volume": self.relative_volume,
        }


def detect_candlestick_trigger(frame: FeatureFrame, index: int) -> Optional[tuple[Side, str]]:
    if index < 1 or index >= len(frame):
        return None
    o, h, low, c = frame.o, frame.h, frame.l, frame.c
    values = (o[index - 1], c[index - 1], o[index], h[index], low[index], c[index])
    if not np.all(np.isfinite(values)):
        return None

    previous_open, previous_close = float(o[index - 1]), float(c[index - 1])
    current_open, current_high = float(o[index]), float(h[index])
    current_low, current_close = float(low[index]), float(c[index])
    body = abs(current_close - current_open)
    candle_range = current_high - current_low
    if body <= 0 or candle_range <= 0:
        return None

    if (previous_close < previous_open and current_close > current_open
            and current_open <= previous_close and current_close >= previous_open):
        return Side.BUY, "BULLISH_ENGULFING"
    if (previous_close > previous_open and current_close < current_open
            and current_open >= previous_close and current_close <= previous_open):
        return Side.SELL, "BEARISH_ENGULFING"

    lower_wick = min(current_open, current_close) - current_low
    upper_wick = current_high - max(current_open, current_close)
    close_location = (current_close - current_low) / candle_range
    if (current_close > current_open and lower_wick >= 2.0 * body
            and upper_wick <= body and close_location >= 2.0 / 3.0):
        return Side.BUY, "BULLISH_PIN_BAR"
    if (current_close < current_open and upper_wick >= 2.0 * body
            and lower_wick <= body and close_location <= 1.0 / 3.0):
        return Side.SELL, "BEARISH_PIN_BAR"
    return None


def confirm_relative_volume(frame: FeatureFrame, index: int, lookback: int = 20,
                            minimum_relative_volume: float = 1.0) -> Optional[float]:
    """Compare the closed candle's tick volume with the prior-bar median."""
    if (index < lookback or index >= len(frame) or minimum_relative_volume <= 0
            or len(frame.tick_volume) <= index):
        return None
    history = frame.tick_volume[index - lookback:index]
    current = float(frame.tick_volume[index])
    if not np.all(np.isfinite(history)) or not np.isfinite(current):
        return None
    baseline = float(np.median(history))
    if baseline <= 0:
        return None
    relative = current / baseline
    return relative if relative >= minimum_relative_volume else None
