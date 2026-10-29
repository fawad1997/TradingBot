"""Exit models - first-class alpha components (section 15).

The SAME functions drive the live trade manager, the backtester and the
counterfactual tracker, so exit research results transfer to live trading.

exit_model = {
  "type": "fixed" | "structure_trail" | "atr_trail" | "chandelier",   # Exit A / B / C / D
  "target_r": 2.0 | null,                                             # fixed target (null = none)
  "partial": {"enabled", "at_r", "fraction", "move_sl_to_be"},        # Exit E
  "trail": {"activate_r", "atr_mult", "pivot_n", "buffer_atr",
            "distance_price"},                                         # distance_price: fixed, set at entry
  "time_stop": {"enabled", "max_bars", "min_r"},                      # Exit F
  "thesis_invalidation": bool,                                         # Exit G
  "breakeven": {"enabled", "activate_r", "cover_spread"},
  "profit_lock": {"enabled", "small_win_activation_r", ...}           # see with_execution_exits
}

THE PROFIT LOCK IS PART OF THE EXIT, SO IT LIVES HERE TOO
---------------------------------------------------------
Until October 2026 the profit-lock ladder (lock 70% of the peak once a trade is 0.5R up) existed only inside
TradeManager.on_tick. Every live trade went through it; the backtester, the counterfactual tracker and the
research lab's simulator did not, so the research judged a different exit from the one that traded, and the
selector pooled the two as one record. The ladder is now `profit_lock_target` below - unit-agnostic, so the
trade manager feeds it money and the simulators feed it R - and `with_execution_exits` copies the configured
levels into each plan's exit model, where a strategy can switch the lock off (trend following must: a lock that
banks 70% of a 0.5R peak never lets a 5R trend happen).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

from app.core.types import Side
from app.features.frame import FeatureFrame
from app.features.indicators import last_confirmed_pivot


@dataclass
class OpenTradeState:
    side: Side
    entry: float
    initial_sl: float
    sl: float
    tp: Optional[float]
    volume: float
    initial_volume: float
    entry_time: int                 # broker epoch of the fill (or signal bar close)
    best_price: float = 0.0         # most favourable price seen (bid for BUY, ask for SELL)
    worst_price: float = 0.0
    bars_held: int = 0
    partial_done: bool = False
    protected: bool = False
    profit_lock_active: bool = False
    profit_lock_stop: Optional[float] = None
    profit_lock_target_money: Optional[float] = None
    profit_high_watermark_money: float = 0.0
    realized_r_partial: float = 0.0  # R already banked by partials (weighted by fraction)

    def __post_init__(self):
        if not self.best_price:
            self.best_price = self.entry
        if not self.worst_price:
            self.worst_price = self.entry

    @property
    def risk(self) -> float:
        return abs(self.entry - self.initial_sl)

    def r(self, price: float) -> float:
        return 0.0 if self.risk <= 0 else (price - self.entry) * self.side.sign / self.risk

    @property
    def mfe_r(self) -> float:
        return self.r(self.best_price)

    @property
    def mae_r(self) -> float:
        return self.r(self.worst_price)

    def observe(self, fav: float, adv: float) -> None:
        if self.side is Side.BUY:
            self.best_price = max(self.best_price, fav)
            self.worst_price = min(self.worst_price, adv)
        else:
            self.best_price = min(self.best_price, fav)
            self.worst_price = max(self.worst_price, adv)


@dataclass
class ExitDecision:
    new_sl: Optional[float] = None
    close_fraction: float = 0.0
    reason: str = ""


def partial_level(st: OpenTradeState, model: dict) -> Optional[float]:
    p = model.get("partial") or {}
    if not p.get("enabled") or st.partial_done:
        return None
    return st.entry + st.side.sign * float(p.get("at_r", 1.0)) * st.risk


def breakeven_sl(st: OpenTradeState, cost_price: float) -> float:
    return st.entry + st.side.sign * cost_price


def improves(st: OpenTradeState, candidate: float, min_step_r: float = 0.0) -> bool:
    """Stops only ever tighten - and, with min_step_r, only by an amount worth a broker round trip.

    Without a minimum step every tick that nudges a trail by a fraction of a point fires another SL_CHANGE.
    WE enforce a cooldown between stop moves (execution.sl_modification_cooldown_sec, 5s), so the surplus
    attempts are refused by our own code - not, as this note previously claimed, by the broker. On 16 Oct
    one GBPUSD position attempted 65 moves and got 24 through. That churn is not harmless: an attempt
    consumes the window, so a trivial nudge can block the meaningful move that follows it.

    min_step_r is a fraction of the trade's OWN initial risk, so it scales from a 0.01-lot forex trade to a
    0.07-lot gold one without a per-symbol number.
    """
    step = 0.0
    if min_step_r > 0.0:
        risk = abs(st.entry - st.initial_sl)
        if risk > 0:
            step = min_step_r * risk
    if st.side is Side.BUY:
        return candidate > st.sl + max(1e-9, step)
    return candidate < st.sl - max(1e-9, step)


def profit_lock_target(peak: float, small_activation: float, small_floor: float, ambition_activation: float,
                       ambition_floor: float, giveback: float, previous_target: Optional[float] = None,
                       was_active: bool = False, base_floor: float = 0.0) -> Tuple[bool, float]:
    """The profit-lock ladder: (lock active?, profit that must be kept) for a trade whose best profit so far is
    `peak`. Every argument is in ONE unit - money in the live trade manager, R in the simulators.

      peak >= ambition_activation   keep max(ambition_floor, peak x (1 - giveback))
      peak >= small_activation      keep max(small_floor,    peak x (1 - giveback))
      below both                    nothing new; an already-active lock keeps its target

    With giveback 0 the legacy fixed rungs apply. The target never falls below `previous_target`, so the floor
    can only rise. The cost floor (never lock less than the round trip costs) is the caller's, because only the
    caller knows the trade's costs in its own unit."""
    if ambition_activation > 0 and peak >= ambition_activation:
        target = (max(ambition_floor, peak * (1.0 - giveback)) if giveback > 0
                  else max(ambition_floor, peak - (ambition_activation - ambition_floor)))
        active = True
    elif small_activation > 0 and peak >= small_activation:
        target = max(small_floor, peak * (1.0 - giveback)) if giveback > 0 else small_floor
        active = True
    else:
        target, active = base_floor, was_active
    return active, max(target, previous_target or 0.0)


#: Breakeven as the live trade manager applies it when a strategy's exit model does not say otherwise.
DEFAULT_BREAKEVEN = {"enabled": True, "activate_r": 0.5, "cover_spread": True}


def with_execution_exits(model: Optional[dict], execution) -> dict:
    """The exit model as it is actually TRADED: the strategy's own model plus the account-wide breakeven and
    profit-lock rules the trade manager applies to every position.

    `execution` is the ExecutionCfg (or a dict of its fields). The lock levels are copied in R; the money levels
    are left to the live manager, which takes the larger of the two, so at any account where 0.5R exceeds $0.20
    (any risk above $0.40) the R levels are the ones that bind. A model that already says
    `profit_lock: {"enabled": false}` keeps the lock off."""
    out = dict(model or {})
    out.setdefault("breakeven", dict(DEFAULT_BREAKEVEN))
    get = (execution.get if isinstance(execution, dict) else lambda k, d=None: getattr(execution, k, d))
    lock = dict(out.get("profit_lock") or {})
    lock.setdefault("enabled", True)
    for name, default in (("small_win_activation_r", 0.5), ("small_win_floor_r", 0.1),
                          ("ambition_activation_r", 0.8), ("ambition_floor_r", 0.4)):
        lock.setdefault(name, float(get(name, default) or 0.0))
    lock.setdefault("giveback", float(get("profit_giveback_fraction", 0.0) or 0.0))
    lock.setdefault("cost_multiple", float(get("profit_lock_cost_multiple", 1.5) or 1.5))
    out["profit_lock"] = lock
    return out


def profit_lock_enabled(model: Optional[dict]) -> bool:
    """True unless the exit model switches the lock off. A model without the key (every trade opened before the
    lock moved here) keeps the behaviour it always had: locked."""
    return bool(((model or {}).get("profit_lock") or {}).get("enabled", True))


def trail_candidate(st: OpenTradeState, model: dict, ff: FeatureFrame, i: int) -> Optional[float]:
    typ = model.get("type", "fixed")
    tr = model.get("trail") or {}
    if typ == "fixed" or st.mfe_r < float(tr.get("activate_r", 1.0)):
        return None
    s = st.side.sign
    # A fixed distance chosen at entry (e.g. 3 x the DAILY ATR) instead of a multiple of this frame's ATR. The
    # trade manager only ever passes the M15 frame, which would turn a daily trail into an intraday one.
    dist = tr.get("distance_price")
    if dist is not None and typ in ("atr_trail", "chandelier"):
        d = float(dist)
        if not np.isfinite(d) or d <= 0:
            return None
        if typ == "atr_trail":
            return float(ff.c[i] - s * d)
        return float(st.best_price - s * d)
    atr = ff.atr(14)[i]
    if not np.isfinite(atr):
        return None
    if typ == "atr_trail":
        return float(ff.c[i] - s * float(tr.get("atr_mult", 2.5)) * atr)
    if typ == "chandelier":
        return float(st.best_price - s * float(tr.get("atr_mult", 2.5)) * atr)
    if typ == "structure_trail":
        n = int(tr.get("pivot_n", 3))
        ph, pl = ff.pivots(n)
        flags = pl if st.side is Side.BUY else ph
        j = last_confirmed_pivot(flags, i, n)
        if j < 0 or ff.t[j] < st.entry_time:
            return None
        buf = float(tr.get("buffer_atr", 0.2)) * atr
        return float(ff.l[j] - buf) if st.side is Side.BUY else float(ff.h[j] + buf)
    return None


def on_bar_close(st: OpenTradeState, model: dict, ff: FeatureFrame, i: int, regime_trend: str,
                 close_price_exit: float, min_stop_distance: float = 0.0) -> ExitDecision:
    """Decide management actions after M15 bar i closed. `close_price_exit` is the
    price we would get closing now (bid for BUY, ask for SELL)."""
    if model.get("thesis_invalidation"):
        opposite = "TREND_DOWN" if st.side is Side.BUY else "TREND_UP"
        if regime_trend == opposite:
            return ExitDecision(close_fraction=1.0, reason="THESIS_INVALIDATED")
    ts = model.get("time_stop") or {}
    if ts.get("enabled") and st.bars_held >= int(ts.get("max_bars", 48)):
        if st.r(close_price_exit) < float(ts.get("min_r", 0.0)):
            return ExitDecision(close_fraction=1.0, reason="TIME_STOP")
    cand = trail_candidate(st, model, ff, i)
    if cand is not None and improves(st, cand):
        # must stay on the correct side of the market by the broker's minimum distance
        if (st.side is Side.BUY and cand < close_price_exit - min_stop_distance) or \
           (st.side is Side.SELL and cand > close_price_exit + min_stop_distance):
            return ExitDecision(new_sl=cand, reason=f"TRAIL_{model.get('type')}".upper())
    return ExitDecision()


@dataclass
class BarEvent:
    kind: str          # SL / TP / PARTIAL / GAP_SL / GAP_TP
    price: float
    fraction: float    # of the INITIAL volume


def simulate_bar(st: OpenTradeState, model: dict, o: float, h: float, l: float, c: float,
                 spread_price: float, slip_price: float) -> List[BarEvent]:
    """Bar-level fill simulation on bid OHLC (MT5 bars are bid based).

    Conservative conventions: if a stop and a favourable level are both inside
    one bar, the stop is assumed to have happened first. Gaps through the stop
    fill at the open (worse than the stop).
    """
    ev: List[BarEvent] = []
    remaining = st.volume / st.initial_volume if st.initial_volume else 1.0
    if st.side is Side.BUY:
        fav_hi, adv_lo, open_px = h, l, o           # bid prices: exits happen on the bid
        if o <= st.sl:
            ev.append(BarEvent("GAP_SL", o - slip_price, remaining))
            st.observe(o, o)
            return ev
        if st.tp and o >= st.tp:
            ev.append(BarEvent("GAP_TP", o, remaining))
            st.observe(o, o)
            return ev
        if l <= st.sl:
            ev.append(BarEvent("SL", st.sl - slip_price, remaining))
            st.observe(max(o, st.sl), l)
            return ev
        lvl = partial_level(st, model)
        if lvl is not None and h >= lvl:
            frac = float(model["partial"].get("fraction", 0.5)) * remaining
            ev.append(BarEvent("PARTIAL", lvl - slip_price, frac))
        if st.tp and h >= st.tp:
            ev.append(BarEvent("TP", st.tp, remaining - sum(e.fraction for e in ev)))
        st.observe(h, l)
    else:
        ao, ah, al = o + spread_price, h + spread_price, l + spread_price   # ask prices
        if ao >= st.sl:
            ev.append(BarEvent("GAP_SL", ao + slip_price, remaining))
            st.observe(ao, ao)
            return ev
        if st.tp and ao <= st.tp:
            ev.append(BarEvent("GAP_TP", ao, remaining))
            st.observe(ao, ao)
            return ev
        if ah >= st.sl:
            ev.append(BarEvent("SL", st.sl + slip_price, remaining))
            st.observe(min(ao, st.sl), ah)
            return ev
        lvl = partial_level(st, model)
        if lvl is not None and al <= lvl:
            frac = float(model["partial"].get("fraction", 0.5)) * remaining
            ev.append(BarEvent("PARTIAL", lvl + slip_price, frac))
        if st.tp and al <= st.tp:
            ev.append(BarEvent("TP", st.tp, remaining - sum(e.fraction for e in ev)))
        st.observe(al, ah)
    return ev


def apply_partial(st: OpenTradeState, model: dict, frac_of_initial: float, price: float, cost_price: float) -> None:
    st.partial_done = True
    st.realized_r_partial += frac_of_initial * st.r(price)
    st.volume = st.volume - frac_of_initial * st.initial_volume
    if (model.get("partial") or {}).get("move_sl_to_be", True):
        be = breakeven_sl(st, cost_price)
        if improves(st, be):
            st.sl = be
            st.protected = True


@dataclass
class SimResult:
    exit_price: float
    exit_reason: str
    r_multiple: float
    mfe_r: float
    mae_r: float
    giveback_r: float
    bars_held: int
    partial_done: bool


class SimulatedTrade:
    """One hypothetical trade stepped bar by bar. Shared by the backtester and
    the counterfactual engine so both use identical fill and exit rules.

    Breakeven and the profit lock run here when the exit model carries them (`with_execution_exits` puts them
    there). Live they act on every tick; on a bar the order inside is unknown, so both are applied causally:
    the stop armed by EARLIER peaks is what this bar's low is tested against, then this bar's high raises the
    peak, and because a bar's close comes after its high, a close beyond the newly armed level means the level
    was crossed inside the bar - the trade exits there, as the tick-level manager would have. A bar that closed
    against the trade is taken to have made its peak first (MT5's tester order), so its far extreme is tested
    against the new level as well."""

    def __init__(self, side: Side, entry: float, sl: float, tp: Optional[float], model: dict, entry_time: int,
                 slip_price: float, be_cost_price: float, max_bars: Optional[int] = None):
        self.st = OpenTradeState(side, entry, sl, sl, tp, 1.0, 1.0, entry_time)
        self.model = model
        self.slip = slip_price
        self.be_cost = be_cost_price
        self.max_bars = max_bars
        self.result: Optional[SimResult] = None
        self._lock_active = False
        self._lock_target: Optional[float] = None
        self._sl_source = ""                     # what last moved the stop: BREAKEVEN / PROFIT_LOCK / TRAIL

    def _finish(self, price: float, reason: str) -> SimResult:
        st = self.st
        rem = st.volume / st.initial_volume if st.initial_volume else 1.0
        r = st.realized_r_partial + rem * st.r(price)
        mfe = st.mfe_r
        self.result = SimResult(price, reason, r, mfe, st.mae_r, max(0.0, mfe - r), st.bars_held, st.partial_done)
        return self.result

    def _managed_level(self, spread_price: float) -> Tuple[Optional[float], str]:
        """The most protective stop that breakeven and the profit lock want after the peak seen so far."""
        st = self.st
        best, why = None, ""
        be = self.model.get("breakeven") or {}
        if be.get("enabled") and st.mfe_r >= float(be.get("activate_r", 0.5)):
            offset = max(spread_price, self.be_cost) if be.get("cover_spread", True) else 0.0
            best, why = breakeven_sl(st, offset), "BREAKEVEN"
        lock = self.model.get("profit_lock") or {}
        if lock.get("enabled") and st.risk > 0:
            cost_r = self.slip / st.risk                    # what closing costs, as the live estimate counts it
            active, target = profit_lock_target(
                st.mfe_r - cost_r, float(lock.get("small_win_activation_r", 0.5)),
                float(lock.get("small_win_floor_r", 0.1)), float(lock.get("ambition_activation_r", 0.8)),
                float(lock.get("ambition_floor_r", 0.4)), float(lock.get("giveback", 0.0)),
                self._lock_target, self._lock_active)
            if active:
                target = max(target, float(lock.get("cost_multiple", 1.5)) * cost_r)
                self._lock_active, self._lock_target = True, target
                level = st.entry + st.side.sign * (target + cost_r) * st.risk
                if best is None or (level - best) * st.side.sign > 0:
                    best, why = level, "PROFIT_LOCK"
        return best, why

    def step(self, o: float, h: float, l: float, c: float, spread_price: float,
             ff: Optional[FeatureFrame] = None, i: Optional[int] = None, regime_trend: str = "",
             min_stop: float = 0.0) -> Optional[SimResult]:
        if self.result is not None:
            return self.result
        st = self.st
        for e in simulate_bar(st, self.model, o, h, l, c, spread_price, self.slip):
            if e.kind == "PARTIAL":
                apply_partial(st, self.model, e.fraction, e.price, self.be_cost)
            else:
                st.bars_held += 1
                kind = e.kind
                if kind in ("SL", "GAP_SL") and self._sl_source in ("BREAKEVEN", "PROFIT_LOCK"):
                    kind = self._sl_source
                return self._finish(e.price, kind)
        st.bars_held += 1
        exit_px = c if st.side is Side.BUY else c + spread_price
        level, why = self._managed_level(spread_price)
        if level is not None and improves(st, level):
            # What traded AFTER this bar's peak: its close, and - on a bar that closed against the trade, which
            # MT5's tester orders open-high-low-close for a buy (open-low-high-close for a sell) - its far
            # extreme too. Testing only the close let a bar that peaked, fell through the new stop and closed back
            # above it count as never stopped, though the live manager would have been (audit, 23 Oct 2026).
            after_peak = exit_px
            if (c - o) * st.side.sign < 0:
                after_peak = l if st.side is Side.BUY else h + spread_price
            if (after_peak - level) * st.side.sign <= 0:   # crossed after this bar's peak
                return self._finish(level - st.side.sign * self.slip, why)
            st.sl, self._sl_source = level, why
        if ff is not None and i is not None:
            dec = on_bar_close(st, self.model, ff, i, regime_trend, exit_px, min_stop)
            if dec.close_fraction >= 1.0:
                return self._finish(exit_px - st.side.sign * self.slip, dec.reason)
            if dec.new_sl is not None:
                st.sl, self._sl_source = dec.new_sl, "TRAIL"
        if self.max_bars and st.bars_held >= self.max_bars:
            return self._finish(exit_px - st.side.sign * self.slip, "MAX_HOLD")
        return None

    def mark_to_market(self, c: float, spread_price: float) -> float:
        st = self.st
        px = c if st.side is Side.BUY else c + spread_price
        rem = st.volume / st.initial_volume if st.initial_volume else 1.0
        return st.realized_r_partial + rem * st.r(px)
