"""Turn a rule's signals into something you can LOOK at, with each signal coloured by what it actually did.

The lab already answers "how many setups and what R". It cannot answer "what do the losers look like" - and that
is the question that tells you whether a rule is finding real structure or fading noise. `vwap_reversion_v1`
produces 84 setups a week on gold; whether those are genuine stretches or every minor wobble is visible in two
seconds on a chart and invisible in a table.

So every signal is simulated to its own stop or target and the marker is coloured by the outcome. A screen of
red clustered in one regime is a finding. Green and red mixed evenly is a different finding - the rule has no
discrimination and no threshold tweak will give it one.

Pure data in, pure data out: no files, no HTML, no chart library. `tools/visualise_rule.py` renders it.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from app.features.frame import FeatureFrame
from app.research.lab.hypotheses import HYPOTHESES

MAX_HOLD = 96              # M15 bars; a signal still open after this is closed at market, as the lab does


def _finite(x) -> bool:
    return x == x and abs(x) != float("inf")


def outcome(m15: FeatureFrame, i: int, side: int, entry: float, stop_dist: float, rr: float,
            spread: float, slippage: float, max_hold: int = MAX_HOLD) -> dict:
    """Walk forward to the stop or the target. Pessimistic: when a bar touches both, the ADVERSE one counts.

    The same assumption is applied to every signal, so comparisons stay fair even though the true intrabar path
    is unknowable from OHLC.
    """
    tp = entry + side * stop_dist * rr
    sl = entry - side * stop_dist
    cost_r = spread / stop_dist if stop_dist > 0 else 0.0
    mfe = 0.0
    n = len(m15)
    for j in range(i + 1, min(i + 1 + max_hold, n)):
        hi, lo = float(m15.h[j]), float(m15.l[j])
        fav = (hi - entry) / stop_dist if side > 0 else (entry - lo) / stop_dist
        if _finite(fav):
            mfe = max(mfe, fav)
        if (lo <= sl) if side > 0 else (hi >= sl):
            return {"r": -1.0 - slippage / stop_dist - cost_r, "bars": j - i, "mfe_r": round(mfe, 3),
                    "exit": "stop"}
        if (hi >= tp) if side > 0 else (lo <= tp):
            return {"r": rr - cost_r, "bars": j - i, "mfe_r": round(mfe, 3), "exit": "target"}
    close = float(m15.c[min(i + max_hold, n - 1)])
    return {"r": (close - entry) / stop_dist * side - cost_r, "bars": min(max_hold, n - 1 - i),
            "mfe_r": round(mfe, 3), "exit": "timeout"}


def build_view(rule: str, m15: FeatureFrame, h1: Optional[FeatureFrame], params: Dict,
               *, spread: float, slippage: float = 0.0, stop_atr: float = 2.0, reward_risk: float = 1.5,
               min_stop_spread_multiple: float = 10.0, start: int = 0, max_signals: int = 2000) -> dict:
    """{"bars": [...], "markers": [...], "stats": {...}} for one rule over one frame.

    Stops and targets are sized exactly as the live CandidateStrategy sizes them, so what you see is what the
    bot would have traded, not an idealised version of it.
    """
    if rule not in HYPOTHESES:
        raise ValueError(f"unknown rule {rule!r}; have {sorted(HYPOTHESES)}")
    sig = HYPOTHESES[rule](m15, h1, params)
    longs, shorts = np.asarray(sig["long"], bool), np.asarray(sig["short"], bool)
    atr = np.asarray(sig["atr"], float)

    bars = [{"time": int(m15.t[k]), "open": float(m15.o[k]), "high": float(m15.h[k]),
             "low": float(m15.l[k]), "close": float(m15.c[k])} for k in range(start, len(m15))]

    markers: List[dict] = []
    rs: List[float] = []
    spread_floor = min_stop_spread_multiple * max(0.0, spread)
    for i in range(max(start, 1), len(m15)):
        side = 1 if longs[i] else (-1 if shorts[i] else 0)
        if side == 0:
            continue
        a = float(atr[i])
        if not _finite(a) or a <= 0:
            continue
        stop_dist = max(stop_atr * a, spread_floor, spread)
        if stop_dist <= 0:
            continue
        entry = float(m15.c[i]) + side * spread / 2.0
        res = outcome(m15, i, side, entry, stop_dist, reward_risk, spread, slippage)
        # Round ONCE and use the same value for the marker and the statistics, so adding up the markers on the
        # chart reproduces the headline exactly. Summing full precision behind rounded markers drifts by about
        # 0.01R over a couple of hundred signals - small, but it makes the panel unverifiable by eye.
        r = round(res["r"], 3)
        rs.append(r)
        markers.append({
            "time": int(m15.t[i]), "side": "long" if side > 0 else "short",
            "r": r, "won": r > 0, "exit": res["exit"],
            "bars_held": res["bars"], "mfe_r": res["mfe_r"],
            "entry": round(entry, 8), "stop": round(entry - side * stop_dist, 8),
            "target": round(entry + side * stop_dist * reward_risk, 8),
        })
        if len(markers) >= max_signals:
            break

    arr = np.array(rs) if rs else np.array([])
    weeks = max(1e-9, (len(m15) - start) * 900 / 604800.0)
    wins = arr[arr > 0] if len(arr) else arr
    losses = arr[arr <= 0] if len(arr) else arr
    stats = {
        "rule": rule, "signals": len(markers), "per_week": round(len(markers) / weeks, 2),
        "total_r": round(float(arr.sum()), 2) if len(arr) else 0.0,
        "avg_r": round(float(arr.mean()), 4) if len(arr) else 0.0,
        "win_rate": round(float((arr > 0).mean()), 3) if len(arr) else 0.0,
        "avg_win_r": round(float(wins.mean()), 3) if len(wins) else 0.0,
        "avg_loss_r": round(float(losses.mean()), 3) if len(losses) else 0.0,
        "bars": len(bars), "spread": spread, "slippage": slippage,
        "stop_atr": stop_atr, "reward_risk": reward_risk,
    }
    return {"bars": bars, "markers": markers, "stats": stats}
