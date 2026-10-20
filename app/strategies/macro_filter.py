"""Causal H4 regression-channel context for scalper entries."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.core.types import Side
from app.features.frame import FeatureFrame


@dataclass(frozen=True)
class MacroChannel:
    slope: float
    lower: float
    upper: float
    position: float
    is_at_channel_extreme: bool


def evaluate_macro_channel(frame: Optional[FeatureFrame], end_index: Optional[int], price: float,
                           direction: Side, window: int = 40, extreme_fraction: float = 0.2
                           ) -> Optional[MacroChannel]:
    """Return regression residual-channel position using only closed H4 bars."""
    if (frame is None or end_index is None or window < 3 or not 0 < extreme_fraction < 0.5
            or end_index >= len(frame) or end_index + 1 < window or not np.isfinite(price)):
        return None

    closes = frame.c[end_index - window + 1:end_index + 1]
    if len(closes) != window or not np.all(np.isfinite(closes)):
        return None
    x = np.arange(window, dtype=float)
    slope, intercept = np.polyfit(x, closes, 1)
    residuals = closes - (intercept + slope * x)
    lower = float(intercept + slope * (window - 1) + np.quantile(residuals, 0.1))
    upper = float(intercept + slope * (window - 1) + np.quantile(residuals, 0.9))
    if not np.isfinite(lower) or not np.isfinite(upper) or upper <= lower:
        return None

    position = float((price - lower) / (upper - lower))
    is_extreme = ((direction is Side.BUY and slope > 0 and position <= extreme_fraction)
                  or (direction is Side.SELL and slope < 0 and position >= 1.0 - extreme_fraction))
    return MacroChannel(float(slope), lower, upper, position, is_extreme)
