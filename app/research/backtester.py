"""Custom event-driven backtester (section 35) built for MT5 CFDs.

Fidelity rules
  * Strategy code is IDENTICAL to live (same Strategy.evaluate on a causal view).
  * H1 alignment: at M15 bar i (closing at t+15m) only H1 bars that CLOSED at or
    before that moment are visible.
  * Signals are formed on the close of bar i and filled at the OPEN of bar i+1
    on the correct side (ask for buys, bid for sells) using that bar's recorded
    spread x spread_mult + extra spread, plus slippage.
  * Stops trigger on the bid (longs) / ask (shorts); a gap through the stop fills
    at the open; stop and target in the same bar -> stop assumed first.
  * Same regime router (allowed regimes, stress), same session / event filters
    and the same exit functions as live (alpha/exits.py).
  * Money simulation at `start_equity` with the broker's minimum volume shows
    which trades the account could not have taken (the $100 problem).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np

from app.alpha.exits import SimulatedTrade
from app.config import SessionsCfg, StrategyCfg
from app.core.types import EventState, Side, SymbolSpec
from app.features.frame import FeatureFrame, resample
from app.market.events import EventCalendar
from app.market.sessions import SessionClassifier
from app.regime.detector import RegimeDetector
from app.research.metrics import group_metrics, trade_metrics
from app.strategies.base import StrategyView
from app.strategies.registry import build


@dataclass
class BTSettings:
    spread_mult: float = 1.0
    extra_spread_points: float = 0.0
    slippage_points: float = 5.0
    commission_per_lot: float = 0.0
    start_equity: float = 100.0
    risk_fraction: float = 0.01
    max_entry_drift_frac: float = 0.25
    apply_regime_filter: bool = True
    apply_session_filter: bool = True
    event_block: bool = False
    swap_nights: bool = True
    grid_enabled: bool = False
    grid_spacing_atr: float = 1.0
    grid_max_layers: int = 3
    grid_basket_risk_fraction: float = 0.01
    grid_target_money: float = 1.0
    grid_leverage: float = 100.0
    grid_max_margin_fraction: float = 0.5
    start_time: Optional[int] = None   # only take signals in [start_time, end_time)
    end_time: Optional[int] = None


@dataclass
class BTResult:
    trades: List[dict]
    metrics: Dict
    by_session: Dict
    by_regime: Dict
    by_direction: Dict
    by_exit: Dict
    by_month: Dict
    skipped: Dict
    feasibility: Dict
    settings: Dict = field(default_factory=dict)


def default_spec() -> SymbolSpec:
    from app.broker.sim import xau_spec
    return xau_spec()


def regime_series(h1: FeatureFrame) -> List[tuple]:
    det = RegimeDetector()
    out = []
    for k in range(len(h1)):
        st = det.update(h1, k)
        out.append((str(st.trend), str(st.vol), st.stress, st.label))
    return out


def _nights(t0: int, t1: int) -> int:
    """Number of 21:00 UTC rollovers crossed (Wednesday counts triple)."""
    n, d = 0, (t0 // 86400) * 86400 + 21 * 3600
    if d <= t0:
        d += 86400
    while d <= t1:
        wd = datetime.fromtimestamp(d, tz=timezone.utc).weekday()
        n += 3 if wd == 2 else (0 if wd in (5, 6) else 1)
        d += 86400
    return n


def run_backtest(scfg: StrategyCfg, m15: FeatureFrame, h1: FeatureFrame, settings: BTSettings,
                 spec: Optional[SymbolSpec] = None, sessions_cfg: Optional[SessionsCfg] = None,
                 calendar: Optional[EventCalendar] = None, param_overrides: Optional[dict] = None,
                 exit_overrides: Optional[dict] = None, regimes: Optional[List[tuple]] = None,
                 m1: Optional[FeatureFrame] = None) -> BTResult:
    spec = spec or default_spec()
    strat = build(scfg, param_overrides, exit_overrides)
    if settings.grid_enabled:
        if scfg.id != "micro_scalper":
            raise ValueError("Research grid mode is only supported for micro_scalper")
        if m1 is None:
            raise ValueError("Research grid mode requires M1 history")
        if settings.grid_spacing_atr <= 0 or settings.grid_max_layers < 2:
            raise ValueError("Grid spacing must be positive and at least two layers are required")
        if not 0 < settings.grid_basket_risk_fraction <= 0.01:
            raise ValueError("Grid basket risk fraction must be in (0, 0.01]")
        if settings.grid_target_money <= 0:
            raise ValueError("Grid basket target must be positive")
        if settings.grid_leverage <= 0 or not 0 < settings.grid_max_margin_fraction <= 0.5:
            raise ValueError("Grid leverage must be positive and margin fraction must be in (0, 0.5]")
    model = strat.exit_model
    sess = SessionClassifier(sessions_cfg or SessionsCfg())
    point = spec.point
    regimes = regimes if regimes is not None else regime_series(h1)
    h1_close = h1.t + 3600
    h4 = resample(h1, 4 * 3600, "H4")
    h4_close = h4.t + 4 * 3600
    N = len(m15)
    warm = max(strat.lookbacks().get("M15", 60), 60)
    trades: List[dict] = []
    skipped: Dict[str, int] = {}
    open_tr: Optional[SimulatedTrade] = None
    open_meta: dict = {}
    open_grid: Optional[dict] = None
    grid_m1_cursor = 0
    seen_setups: set = set()
    equity = settings.start_equity
    infeasible = 0
    min_stop = spec.stops_level * point

    def spread_at(j: int) -> float:
        return (m15.spread[j] * settings.spread_mult + settings.extra_spread_points) * point

    slip = settings.slippage_points * point

    def grid_net(g: dict, bid: float, spread: float) -> float:
        side = g["side"]
        exit_price = bid if side is Side.BUY else bid + spread
        pnl = sum(((exit_price - entry) if side is Side.BUY else (entry - exit_price))
                  * spec.contract_size * g["volume"] for entry in g["entries"])
        return pnl - (settings.commission_per_lot + slip * spec.contract_size) * g["volume"] * len(g["entries"])

    for i in range(warm, N - 1):
        k = int(np.searchsorted(h1_close, m15.t[i] + 900, side="right") - 1)
        q = int(np.searchsorted(h4_close, m15.t[i] + 900, side="right") - 1)
        if k < 1:
            continue
        trend, vol, stress, label = regimes[k]
        # ---- manage research-only M1 grid baskets
        if open_grid is not None:
            interval_end = int(m15.t[i]) + 900
            while grid_m1_cursor < len(m1) and int(m1.t[grid_m1_cursor]) < open_grid["entry_time"]:
                grid_m1_cursor += 1
            while grid_m1_cursor < len(m1) and int(m1.t[grid_m1_cursor]) < interval_end:
                mi = grid_m1_cursor
                grid_m1_cursor += 1
                g = open_grid
                side = g["side"]
                bar_spread = spread_at(i)
                levels = g["levels"]
                if side is Side.BUY:
                    adverse = float(m1.l[mi])
                    while len(g["entries"]) < settings.grid_max_layers:
                        level = levels[len(g["entries"]) - 1]
                        if adverse > level:
                            break
                        g["entries"].append(level + bar_spread + slip)
                    favorable = float(m1.h[mi])
                    g["mfe_r"] = max(g["mfe_r"], grid_net(g, favorable, bar_spread) / g["risk_money"])
                    g["mae_r"] = min(g["mae_r"], grid_net(g, adverse, bar_spread) / g["risk_money"])
                    if adverse <= g["stop_price"]:
                        exit_bid = min(g["stop_price"], float(m1.o[mi]))
                        pnl = grid_net(g, exit_bid, bar_spread)
                        exit_reason = "GRID_BASKET_STOP"
                        exit_price = exit_bid - slip
                    else:
                        total_volume = g["volume"] * len(g["entries"])
                        weighted_entry = sum(g["entries"]) / len(g["entries"])
                        exit_price_target = weighted_entry + (
                            settings.grid_target_money + settings.commission_per_lot * total_volume
                            + slip * spec.contract_size * total_volume
                        ) / (spec.contract_size * total_volume)
                        if total_volume > 0 and grid_net(g, favorable, bar_spread) >= settings.grid_target_money:
                            exit_reason = "GRID_BASKET_TARGET"
                            exit_price = exit_price_target - slip
                            pnl = grid_net(g, exit_price_target, bar_spread)
                        else:
                            continue
                else:
                    adverse = float(m1.h[mi])
                    while len(g["entries"]) < settings.grid_max_layers:
                        level = levels[len(g["entries"]) - 1]
                        if adverse < level:
                            break
                        g["entries"].append(level - slip)
                    favorable = float(m1.l[mi])
                    g["mfe_r"] = max(g["mfe_r"], grid_net(g, favorable, bar_spread) / g["risk_money"])
                    g["mae_r"] = min(g["mae_r"], grid_net(g, adverse, bar_spread) / g["risk_money"])
                    if adverse >= g["stop_price"]:
                        exit_bid = max(g["stop_price"] - bar_spread, float(m1.o[mi]))
                        pnl = grid_net(g, exit_bid, bar_spread)
                        exit_reason = "GRID_BASKET_STOP"
                        exit_price = exit_bid + bar_spread + slip
                    else:
                        total_volume = g["volume"] * len(g["entries"])
                        weighted_entry = sum(g["entries"]) / len(g["entries"])
                        exit_price_target = weighted_entry - bar_spread - (
                            settings.grid_target_money + settings.commission_per_lot * total_volume
                            + slip * spec.contract_size * total_volume
                        ) / (spec.contract_size * total_volume)
                        if total_volume > 0 and grid_net(g, favorable, bar_spread) >= settings.grid_target_money:
                            exit_reason = "GRID_BASKET_TARGET"
                            exit_price = exit_price_target + slip + bar_spread
                            pnl = grid_net(g, exit_price_target, bar_spread)
                        else:
                            continue
                risk_money = g["risk_money"]
                r_net = pnl / risk_money if risk_money > 0 else 0.0
                equity += pnl
                exit_time = int(m1.t[mi]) + 60
                trades.append({
                    "strategy_id": scfg.id, "side": side, "direction": str(side),
                    "signal_time": g["signal_time"], "entry_time": g["entry_time"],
                    "exit_time": exit_time, "entry": g["entries"][0],
                    "mean_entry": sum(g["entries"]) / len(g["entries"]), "exit": exit_price,
                    "sl": g["stop_price"], "tp": None, "stop": g["stop_distance"],
                    "session": g["session"], "regime": g["regime"], "volatility": g["volatility"],
                    "event_state": g["event_state"], "spread_points": g["spread_points"],
                    "volume": g["volume"] * len(g["entries"]), "layers": len(g["entries"]),
                    "feasible": g["feasible"], "min_volume_risk": g["min_volume_risk"],
                    "risk_money": g["risk_money"],
                    "features": g["features"], "quality": g["quality"],
                    "r_multiple": r_net, "r_gross": r_net, "mfe_r": g["mfe_r"], "mae_r": g["mae_r"],
                    "giveback_r": max(0.0, g["mfe_r"] - r_net),
                    "bars_held": max(1, (exit_time - g["entry_time"]) // 60),
                    "exit_reason": exit_reason, "pnl_money": pnl,
                    "cost_r": max(0.0, (settings.commission_per_lot + slip * spec.contract_size)
                                  * g["volume"] * len(g["entries"]) / risk_money) if risk_money else 0.0,
                    "equity_after": equity,
                })
                open_grid = None
                break
            if open_grid is not None:
                continue

        # ---- manage the open trade with bar i
        if open_tr is not None:
            res = open_tr.step(m15.o[i], m15.h[i], m15.l[i], m15.c[i], spread_at(i), m15, i, trend, min_stop)
            if res is not None:
                meta = open_meta
                stop = meta["stop"]
                comm_r = settings.commission_per_lot / (stop * spec.contract_size) if stop > 0 else 0.0
                swap_r = 0.0
                if settings.swap_nights:
                    sw = spec.swap_long if meta["side"] is Side.BUY else spec.swap_short
                    if sw < 0 and spec.swap_mode in (0, 1):   # points
                        swap_r = -sw * point * _nights(meta["entry_time"], int(m15.t[i]) + 900) / stop
                r_net = res.r_multiple - comm_r - swap_r
                vol_lots = meta["volume"]
                pnl = r_net * vol_lots * stop * spec.contract_size if vol_lots else 0.0
                equity += pnl
                trades.append({**{k2: v for k2, v in meta.items() if k2 not in ("side",)},
                               "direction": str(meta["side"]), "exit_time": int(m15.t[i]) + 900,
                               "exit": res.exit_price, "r_multiple": r_net, "r_gross": res.r_multiple,
                               "mfe_r": res.mfe_r, "mae_r": res.mae_r, "giveback_r": max(0.0, res.mfe_r - r_net),
                               "bars_held": res.bars_held, "exit_reason": res.exit_reason, "pnl_money": pnl,
                               "cost_r": comm_r + swap_r, "equity_after": equity})
                open_tr = None
        if open_tr is not None:
            continue
        # ---- evaluate a new signal at the close of bar i
        t_sig = int(m15.t[i])
        if settings.start_time and t_sig < settings.start_time:
            continue
        if settings.end_time and t_sig >= settings.end_time:
            break
        sp = spread_at(i)
        m1_index = -1
        if m1 is not None:
            m1_index = int(np.searchsorted(m1.t, t_sig + 900 - 60, side="right") - 1)
        spread_history = m15.spread[max(0, i - 300):i]
        spread_safe = bool(len(spread_history) >= 20 and
                           sp / point <= np.quantile(spread_history, 0.90))
        view = StrategyView(m15=m15, i=i, h1=h1, k=k, h4=h4, q=q, symbol=spec.name,
                            point=point, spread_price=sp, bid=float(m15.c[i]),
                            ask=float(m15.c[i] + sp), regime_trend=trend,
                            m1=m1 if m1_index >= 0 else None, j=m1_index if m1_index >= 0 else None,
                            spread_safe=spread_safe)
        ev = strat.evaluate(view)
        if ev.plan is None:
            continue
        plan = ev.plan
        if plan.setup_key in seen_setups:
            continue
        seen_setups.add(plan.setup_key)
        dt = datetime.fromtimestamp(t_sig + 900, tz=timezone.utc)
        session = str(sess.classify(dt))
        reason = None
        if settings.apply_regime_filter:
            if stress:
                reason = "REGIME_STRESS"
            elif scfg.allowed_regimes and trend not in scfg.allowed_regimes:
                reason = f"REGIME_{trend}"
        if reason is None and settings.apply_session_filter and scfg.allowed_sessions and session not in scfg.allowed_sessions:
            reason = f"SESSION_{session}"
        ev_state = calendar.state(dt) if calendar else EventState.UNKNOWN
        if reason is None and settings.event_block and ev_state in (EventState.PRE_EVENT, EventState.EVENT):
            reason = f"EVENT_{ev_state}"
        # ---- fill at next bar open
        j = i + 1
        sp1 = spread_at(j)
        o1 = float(m15.o[j])
        if settings.grid_enabled:
            trigger_pattern = plan.features.get("trigger_pattern", "")
            if trigger_pattern not in ("BULLISH_PIN_BAR", "BEARISH_PIN_BAR",
                                       "BULLISH_ENGULFING", "BEARISH_ENGULFING"):
                skipped["GRID_CANDLE_TRIGGER_REQUIRED"] = skipped.get("GRID_CANDLE_TRIGGER_REQUIRED", 0) + 1
                continue
            signal_atr = float(plan.features.get("atr_val", 0.0))
            if not math.isfinite(signal_atr) or signal_atr <= 0:
                skipped["GRID_ATR_UNAVAILABLE"] = skipped.get("GRID_ATR_UNAVAILABLE", 0) + 1
                continue
            spacing = signal_atr * settings.grid_spacing_atr
            entry = o1 + sp1 + slip if plan.direction is Side.BUY else o1 - slip
            stop_distance = spacing * (settings.grid_max_layers)
            stop_price = entry - stop_distance if plan.direction is Side.BUY else entry + stop_distance
            layer_distances = [spacing * (settings.grid_max_layers - layer)
                               for layer in range(settings.grid_max_layers)]
            loss_per_lot = (sum(layer_distances) * spec.contract_size
                            + settings.grid_max_layers * (
                                settings.commission_per_lot + 2.0 * slip * spec.contract_size
                            ))
            risk_money = equity * settings.grid_basket_risk_fraction
            volume_by_risk = risk_money / loss_per_lot if loss_per_lot > 0 else 0.0
            margin_per_lot = settings.grid_max_layers * spec.contract_size * entry / settings.grid_leverage
            volume_by_margin = (equity * settings.grid_max_margin_fraction / margin_per_lot
                                if margin_per_lot > 0 else 0.0)
            volume = spec.floor_volume(min(volume_by_risk, volume_by_margin))
            feasible = volume >= spec.volume_min
            if not feasible:
                infeasible += 1
                skipped["GRID_BASKET_SIZE_INFEASIBLE"] = skipped.get("GRID_BASKET_SIZE_INFEASIBLE", 0) + 1
                continue
            levels = [entry - spacing * layer if plan.direction is Side.BUY
                      else entry + spacing * layer
                      for layer in range(1, settings.grid_max_layers)]
            open_grid = {
                "side": plan.direction, "entries": [entry], "levels": levels, "volume": volume,
                "entry_time": int(m15.t[j]), "signal_time": t_sig + 900,
                "stop_price": stop_price, "stop_distance": stop_distance,
                "risk_money": risk_money, "feasible": feasible,
                "mfe_r": 0.0, "mae_r": 0.0,
                "min_volume_risk": spec.volume_min * loss_per_lot,
                "session": session, "regime": label, "volatility": vol, "event_state": str(ev_state),
                "spread_points": sp1 / point, "features": plan.features, "quality": plan.quality,
            }
            grid_m1_cursor = int(np.searchsorted(m1.t, int(m15.t[j]), side="left"))
            continue
        entry = o1 + sp1 + slip if plan.direction is Side.BUY else o1 - slip
        stop = (entry - plan.sl) * plan.direction.sign
        if reason is None:
            if stop <= 0:
                reason = "GAP_THROUGH_STOP"
            elif (entry - plan.entry) * plan.direction.sign > settings.max_entry_drift_frac * plan.stop_distance:
                reason = "ENTRY_DRIFT"
            elif plan.tp is not None and (plan.tp - entry) * plan.direction.sign <= 0:
                reason = "TARGET_ALREADY_REACHED"
        if reason is not None:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        risk_money = equity * settings.risk_fraction
        loss_per_lot = stop * spec.contract_size + settings.commission_per_lot
        vol_lots = spec.floor_volume(risk_money / loss_per_lot) if loss_per_lot > 0 else 0.0
        feasible = vol_lots >= spec.volume_min
        if not feasible:
            infeasible += 1
            vol_lots = 0.0
        open_tr = SimulatedTrade(plan.direction, entry, plan.sl, plan.tp, model, int(m15.t[j]), slip, slip + sp1)
        open_meta = {"strategy_id": scfg.id, "side": plan.direction, "signal_time": t_sig + 900,
                     "entry_time": int(m15.t[j]), "entry": entry, "sl": plan.sl, "tp": plan.tp, "stop": stop,
                     "session": session, "regime": label, "volatility": vol, "event_state": str(ev_state),
                     "spread_points": sp1 / point, "volume": vol_lots, "feasible": feasible,
                     "min_volume_risk": spec.volume_min * loss_per_lot, "features": plan.features,
                     "quality": plan.quality}
        # bar j (the fill bar) is managed by the next loop iteration: entry happens at its open,
        # then its high/low can hit the stop or target exactly like any later bar.

    rs = [t["r_multiple"] for t in trades]
    m = trade_metrics(rs, [t["mfe_r"] for t in trades], [t["mae_r"] for t in trades],
                      [t["giveback_r"] for t in trades], [t["bars_held"] for t in trades])
    feas_trades = [t for t in trades if t["feasible"]]
    feas = {"start_equity": settings.start_equity, "risk_fraction": settings.risk_fraction,
            "signals_taken": len(trades), "infeasible_at_min_volume": infeasible,
            "feasible_fraction": round(len(feas_trades) / len(trades), 3) if trades else None,
            "final_equity": round(equity, 2),
            "median_min_volume_risk_money": round(float(np.median([t["min_volume_risk"] for t in trades])), 2)
            if trades else None}
    for t in trades:
        t["month"] = datetime.fromtimestamp(t["signal_time"], tz=timezone.utc).strftime("%Y-%m")
    return BTResult(trades, m, group_metrics(trades, "session"), group_metrics(trades, "regime"),
                    group_metrics(trades, "direction"), group_metrics(trades, "exit_reason"),
                    group_metrics(trades, "month"), skipped, feas, settings.__dict__.copy())
