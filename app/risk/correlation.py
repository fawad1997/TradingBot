"""Synchronized rolling correlations for portfolio risk grouping."""
from __future__ import annotations

from typing import Optional

import numpy as np

from app.features.frame import FeatureFrame


def rolling_h1_correlation(a: Optional[FeatureFrame], b: Optional[FeatureFrame],
                           lookback_days: int = 30, min_samples: int = 400) -> Optional[float]:
    if a is None or b is None or len(a) < 2 or len(b) < 2:
        return None

    def returns(frame: FeatureFrame) -> dict[int, float]:
        values: dict[int, float] = {}
        for i in range(1, len(frame)):
            start, end = int(frame.t[i - 1]), int(frame.t[i])
            previous, current = float(frame.c[i - 1]), float(frame.c[i])
            if end - start != 3600 or not np.isfinite(previous + current) or previous <= 0:
                continue
            values[end] = current / previous - 1.0
        return values

    a_returns = returns(a)
    b_returns = returns(b)
    common_times = sorted(a_returns.keys() & b_returns.keys())
    if not common_times:
        return None
    cutoff = common_times[-1] - lookback_days * 86400
    aligned = [t for t in common_times if t >= cutoff]
    if len(aligned) < min_samples:
        return None
    x = np.asarray([a_returns[t] for t in aligned], dtype=float)
    y = np.asarray([b_returns[t] for t in aligned], dtype=float)
    if np.std(x) == 0.0 or np.std(y) == 0.0:
        return None
    correlation = float(np.corrcoef(x, y)[0, 1])
    return correlation if np.isfinite(correlation) else None
