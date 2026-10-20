"""Was the cost estimate right? And what did execution actually cost?

The cost gate refuses a signal whenever its estimated round-trip cost exceeds a fraction of the money at
risk. That estimate is persisted on every signal as `cost_r`. Until now nothing ever compared it against
what the trade actually cost, so a gate that systematically over- or under-estimated would have looked
identical to one that was right.

Two measurements live here.

KISSELL'S Z-SCORE (ch.3)

    Z = (estimated cost - actual cost) / timing risk

    If the estimates are honest, Z is distributed N(0, 1). A positive mean says the model overestimates
    cost, which means the gate has been refusing trades it should have taken - the one route to more
    trades that needs no new edge. A negative mean says the opposite and is more dangerous. A variance
    far above 1 says costs are much less predictable than the model believes.

    The sign convention follows Kissell, not the textbook z-score: positive is GOOD (better than
    expected). This is deliberate, so that every performance number in this module reads the same way.

HARRIS'S THREE BENCHMARKS (ch.21)

    All three are the signed difference between the fill price and a quote midpoint. Only the choice of
    midpoint differs:

      effective spread    midpoint at the moment of the fill   - what crossing cost
      realized spread     midpoint N minutes after the fill    - what the other side kept
      implementation      midpoint when the signal was made    - the full cost including delay
        shortfall

    `effective - realized` is adverse selection: how far price kept moving against the fill afterwards.
    A system being picked off shows a realized spread much worse than its effective spread, and a
    points-of-slippage measure cannot reveal it.

WHY SLIPPAGE IN POINTS IS NOT COMPARABLE ACROSS SYMBOLS

    A point of gold and a point of EURUSD are different quantities. Averaging them, as the session review
    did, produces a figure with no meaning. Everything here is reported in basis points of price and, when
    an ATR is supplied, as a fraction of the instrument's own recent range.

    Market impact is NOT part of any of this. At 0.01-1.0 lots nothing this system does moves a price, so
    every basis point measured here is price appreciation - the market moving during the round-robin wait,
    plus adverse selection. That is what these numbers mean; they are not a verdict on liquidity.

Read only. Nothing in this module places, modifies or cancels anything.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

Z95 = 1.96
DEFAULT_REALIZED_MINUTES = 15.0
MIN_SAMPLES_FOR_A_VERDICT = 30


# --------------------------------------------------------------------------------------- helpers
def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _when(v) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _side_sign(direction) -> int:
    """+1 for a buy, -1 for a sell. Anything unrecognised is treated as a buy."""
    s = str(direction or "").upper()
    return -1 if ("SELL" in s or s == "-1") else 1


def mean_sd(xs: Sequence[float]) -> Dict[str, float]:
    n = len(xs)
    if n == 0:
        return {"n": 0, "mean": 0.0, "sd": 0.0, "se": 0.0, "lo": 0.0, "hi": 0.0}
    mean = sum(xs) / n
    if n < 2:
        return {"n": n, "mean": mean, "sd": 0.0, "se": 0.0, "lo": mean, "hi": mean}
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    sd = math.sqrt(var)
    se = sd / math.sqrt(n)
    return {"n": n, "mean": mean, "sd": sd, "se": se, "lo": mean - Z95 * se, "hi": mean + Z95 * se}


# --------------------------------------------------------------------------------------- benchmarks
def signed_bp(fill_price: float, benchmark_price: float, direction) -> Optional[float]:
    """Cost in basis points of the benchmark. POSITIVE = worse than the benchmark.

    Basis points rather than points, so that gold and EURUSD can be averaged together without the
    result being nonsense.
    """
    fp, bp_ = _f(fill_price), _f(benchmark_price)
    if fp is None or bp_ is None or bp_ == 0:
        return None
    return _side_sign(direction) * (fp - bp_) / bp_ * 10_000.0


@dataclass
class ExecutionCost:
    """One fill, measured against all three of Harris's benchmarks."""
    trade_id: Any = None
    symbol: str = ""
    strategy_id: str = ""
    direction: str = ""
    effective_bp: Optional[float] = None       # vs midpoint at fill
    realized_bp: Optional[float] = None        # vs midpoint N minutes later
    shortfall_bp: Optional[float] = None       # vs midpoint at the decision
    delay_bp: Optional[float] = None           # shortfall - effective: the cost of the wait
    adverse_selection_bp: Optional[float] = None   # effective - realized
    atr_fraction: Optional[float] = None       # fill-vs-decision drift as a fraction of ATR
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v)
                for k, v in self.__dict__.items() if k != "detail"}


def execution_cost(*, fill_price, direction, mid_at_fill=None, mid_at_decision=None,
                   mid_after=None, atr=None, trade_id=None, symbol="", strategy_id="") -> ExecutionCost:
    """Measure one fill. Any benchmark that is unavailable is simply left as None."""
    out = ExecutionCost(trade_id=trade_id, symbol=symbol, strategy_id=strategy_id,
                        direction=str(direction))
    out.effective_bp = signed_bp(fill_price, mid_at_fill, direction) if mid_at_fill is not None else None
    out.shortfall_bp = signed_bp(fill_price, mid_at_decision, direction) if mid_at_decision is not None else None

    # The realized spread asks what the liquidity supplier kept: compare the LATER midpoint against
    # the fill, which is the opposite direction to the other two benchmarks.
    if mid_after is not None:
        later, fp = _f(mid_after), _f(fill_price)
        if later is not None and fp is not None and fp != 0:
            out.realized_bp = _side_sign(direction) * (fp - later) / fp * 10_000.0

    if out.shortfall_bp is not None and out.effective_bp is not None:
        out.delay_bp = out.shortfall_bp - out.effective_bp
    if out.effective_bp is not None and out.realized_bp is not None:
        out.adverse_selection_bp = out.effective_bp - out.realized_bp

    a, fp, dec = _f(atr), _f(fill_price), _f(mid_at_decision)
    if a and a > 0 and fp is not None and dec is not None:
        out.atr_fraction = _side_sign(direction) * (fp - dec) / a
    return out


# --------------------------------------------------------------------------------------- timing risk
def timing_risk_r(atr: Optional[float], stop_distance: Optional[float],
                  horizon_minutes: float, bar_minutes: float = 5.0) -> Optional[float]:
    """How uncertain the cost estimate is, in units of the trade's own risk.

    Kissell's timing risk is price volatility scaled to the expected trading horizon. Here the horizon
    is the gap between decision and fill, which for this system is seconds, so the number is small -
    and that is the correct answer rather than a flaw. Volatility scales with the square root of time.

    Returns None when ATR or the stop distance is missing, because a z-score without a real denominator
    is worse than no z-score.
    """
    a, stop = _f(atr), _f(stop_distance)
    if a is None or stop is None or stop <= 0 or a <= 0:
        return None
    if horizon_minutes <= 0 or bar_minutes <= 0:
        return 0.0
    return (a * math.sqrt(horizon_minutes / bar_minutes)) / stop


def cost_zscore(estimated_cost_r: Optional[float], actual_cost_r: Optional[float],
                timing_risk: Optional[float]) -> Optional[float]:
    """Kissell ch.3. Positive means the execution beat the estimate.

    Note the sign: this is (estimate - actual), not the textbook (actual - expected), so that positive
    always reads as good across this module.
    """
    est, act, tr = _f(estimated_cost_r), _f(actual_cost_r), _f(timing_risk)
    if est is None or act is None or tr is None or tr <= 0:
        return None
    return (est - act) / tr


def audit_cost_model(rows: Sequence[dict]) -> Dict[str, Any]:
    """Is the cost model honest? Feed it rows carrying estimated_cost_r, actual_cost_r, timing_risk.

    Returns the z-score distribution plus a plain-language verdict. Kissell requires the test be run
    separately by order size and by strategy, because a model can overestimate for one and underestimate
    for another and still average out to N(0,1); `by_strategy` and `by_symbol` do that.
    """
    zs, by_strategy, by_symbol = [], {}, {}
    for r in rows:
        z = cost_zscore(r.get("estimated_cost_r"), r.get("actual_cost_r"), r.get("timing_risk"))
        if z is None:
            continue
        zs.append(z)
        by_strategy.setdefault(str(r.get("strategy_id") or "?"), []).append(z)
        by_symbol.setdefault(str(r.get("symbol") or "?"), []).append(z)

    overall = mean_sd(zs)
    enough = overall["n"] >= MIN_SAMPLES_FOR_A_VERDICT
    verdict, action = _verdict(overall, enough)
    return {
        "n": overall["n"],
        "enough_data": enough,
        "mean_z": round(overall["mean"], 4),
        "sd_z": round(overall["sd"], 4),
        "ci95": [round(overall["lo"], 4), round(overall["hi"], 4)],
        "verdict": verdict,
        "what_to_do": action,
        "by_strategy": {k: {"n": len(v), "mean_z": round(mean_sd(v)["mean"], 4)}
                        for k, v in sorted(by_strategy.items())},
        "by_symbol": {k: {"n": len(v), "mean_z": round(mean_sd(v)["mean"], 4)}
                      for k, v in sorted(by_symbol.items())},
    }


# A |mean z| beyond this is not a finding about the cost model, it is a sign the denominator collapsed.
# Timing risk estimated empirically goes to zero when realised costs barely vary, and dividing by it
# then produces an enormous score that looks like overwhelming evidence and is an artefact.
IMPLAUSIBLE_Z = 10.0


def _verdict(o: Dict[str, float], enough: bool) -> tuple:
    if o["n"] == 0:
        return ("NO_DATA", "No fills carry both an estimate and a realised cost yet.")
    if not enough:
        return ("TOO_FEW",
                f"{o['n']} fills measured, {MIN_SAMPLES_FOR_A_VERDICT} needed before this means anything.")
    if abs(o["mean"]) > IMPLAUSIBLE_Z:
        return ("DENOMINATOR_TOO_SMALL",
                f"Mean z of {o['mean']:+.1f} is too large to be real. Realised costs vary so little that "
                "the timing-risk estimate has collapsed toward zero, which inflates every score. Compare "
                "the raw estimated and actual costs directly instead of trusting this ratio.")
    # The confidence interval decides, not the point estimate.
    if o["lo"] > 0:
        return ("OVERESTIMATES",
                "The cost gate expects trades to cost more than they do, so it is refusing trades it "
                "should take. Loosening max_cost_fraction_of_risk is justified by this measurement.")
    if o["hi"] < 0:
        return ("UNDERESTIMATES",
                "Trades cost more than the gate expects. Costs are being understated, which means some "
                "trades that passed should not have. Tighten the gate or raise cost_buffer_r.")
    if o["sd"] > 2.0:
        return ("UNSTABLE",
                "The estimate is unbiased on average but much less reliable than it claims. The timing "
                "risk term is too small; costs vary far more than the model allows for.")
    return ("HONEST", "Estimates match outcomes within noise. No change justified by this measurement.")
