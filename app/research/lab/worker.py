"""Research lab worker: a SEPARATE low-priority process started with the bot.

It never imports the trading core, never sends orders and never shares the bot's MT5 connection: it attaches to the
terminal for history only. If vectorbt / backtrader are not installed, or anything fails, it logs and keeps looping -
the bot is unaffected either way. Nothing here changes live parameters; the trading core reads the saved verdicts and may block a strategy
on a symbol where it clearly has no edge (app/research/lab/gate.py).
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("research_lab")


def set_low_priority() -> None:
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL
        else:
            os.nice(10)
    except Exception:
        pass


def _other_daily_rules(cfgm, symbol: str) -> list:
    """(result key, research_trend arguments) for each enabled daily strategy besides trend_daily configured on
    `symbol`: its rule, its live parameters and the exit its live class attaches, so the replay is the strategy."""
    from app.market.symbols import contains
    from app.research.lab.hypotheses import (FAR_TARGET_R, TREND_ENSEMBLE_DEFAULTS, TURN_OF_MONTH_DEFAULTS,
                                             trend_ensemble, turn_of_month)
    from app.strategies.turn_of_month import hold_exit
    out = []
    for s in cfgm.strategies.strategies:
        if not s.enabled or (s.symbols and "*" not in s.symbols and not contains(s.symbols, symbol)):
            continue
        p = s.param_values()
        if s.class_name == "TrendEnsemble":
            params = {k: p[k] for k in TREND_ENSEMBLE_DEFAULTS if k in p}
            out.append(("trend_ensemble", {"rule": trend_ensemble, "defaults": TREND_ENSEMBLE_DEFAULTS,
                                           "params": params, "min_days": 260}))
        elif s.class_name == "TurnOfMonth":
            params = {k: p[k] for k in TURN_OF_MONTH_DEFAULTS if k in p}
            hold = int(params.get("hold_days", TURN_OF_MONTH_DEFAULTS["hold_days"]))
            # one bar is one DAY here, so the hold is in days; live it is converted to M15 bars
            out.append(("turn_of_month", {"rule": turn_of_month, "defaults": TURN_OF_MONTH_DEFAULTS,
                                          "params": params, "min_days": 22, "weekdays_only": True,
                                          "exit_model": lambda d, hold=hold: hold_exit(hold, FAR_TARGET_R)}))
    return out


def _commission_money(cfg, symbol: str) -> float:
    """Round-trip commission per lot in account currency - what BTSettings.commission_per_lot means."""
    from app.market.symbols import lookup
    return float(lookup(cfg.cost.symbol_commission_per_lot_round_trip, symbol, cfg.cost.commission_per_lot_round_trip))


def _commission_price(cfg, symbol: str, spec) -> float:
    """Round-trip commission per lot expressed as a price move (what the price must travel to pay it). For the fast
    simulator only: the generic backtester takes money (_commission_money). Handing it this number booked gold's
    commission 100x too small and EURUSD's 100,000x (audit, 23 Oct 2026)."""
    money = _commission_money(cfg, symbol)
    if money <= 0 or spec is None:
        return 0.0
    tick_value, tick_size = float(getattr(spec, "tick_value", 0) or 0), float(getattr(spec, "tick_size", 0) or 0)
    if tick_value <= 0 or tick_size <= 0:
        return 0.0
    return money / (tick_value / tick_size)


def _cost_summary(m1, point, spread_price, tick_spread, commission_price) -> dict:
    import numpy as np
    bar = np.asarray(m1.spread, float) * point
    out = {"bar_spread_median": float(np.median(bar[bar > 0])) if np.any(bar > 0) else None,
           "commission_price_round_trip": commission_price or 0.0,
           "spread_source": "bar spreads"}
    if tick_spread is not None:
        known = tick_spread[np.isfinite(tick_spread)]
        out["tick_spread_median"] = float(np.median(known)) if len(known) else None
        out["tick_bars"] = int(len(known))
        out["spread_source"] = f"ticks for {len(known)} bars, bar spreads before that"
        if out["bar_spread_median"] and out.get("tick_spread_median"):
            out["tick_vs_bar_spread"] = round(out["tick_spread_median"] / out["bar_spread_median"], 3)
    return out


def _timeframe_entry(name, trig, confirm, confirm_s, trig_s, spread, lab_cfg, micro_params, commission,
                     traded_model=None):
    from app.research.lab.fastsim import grid_train_test
    from app.research.lab.signals import apply_volume, trigger_candidates
    cands = trigger_candidates(trig, confirm, confirm_s, trig_s, int(micro_params.get("volume_lookback", 20)))
    sig = apply_volume(cands, float(micro_params.get("minimum_relative_volume", 1.2)))
    entry = {"timeframe": name, "bars": len(trig)}
    if int(sig["long"].sum() + sig["short"].sum()) < lab_cfg.min_trades:
        entry.update(verdict="TOO_FEW_SIGNALS", signals=int(sig["long"].sum() + sig["short"].sum()))
        return entry, cands
    res = grid_train_test(trig.o, trig.h, trig.l, trig.c, sig["long"], sig["short"], sig["atr"],
                          lab_cfg.sl_atr_grid, lab_cfg.rr_grid, lab_cfg.train_fraction, lab_cfg.min_trades,
                          spread, commission, _stop_floor(micro_params))
    entry.update(res)
    _add_stats(entry, trig, sig, lab_cfg, spread, commission, _stop_floor(micro_params), traded_model)
    return entry, cands


def _add_stats(entry: dict, frame, sig: dict, lab_cfg, spread, commission: float, floor: float,
               traded_model=None) -> None:
    """Attach the evidence block (range, Sharpe shape, overfitting, random-direction benchmark, exits) to a
    grid-scored cell. Never fatal: a cell without stats is graded THIN later."""
    if not getattr(lab_cfg, "evidence_enabled", False) or not entry.get("best"):
        return
    try:
        from app.research.lab.evidence import cell_stats
        st = cell_stats(frame, sig["long"], sig["short"], sig["atr"], lab_cfg.sl_atr_grid, lab_cfg.rr_grid,
                        lab_cfg.train_fraction, entry["best"], spread, commission, floor, traded_model)
        if st is not None:
            entry["stats"] = st
    except Exception:
        log.exception("evidence statistics failed")


def _stop_floor(micro_params: dict) -> float:
    """The live micro_scalper never sets a stop closer than this many spreads; the replay must do the same."""
    try:
        return float(micro_params.get("min_stop_spread_multiple", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _optuna(frame, cands, spread, commission, lab_cfg, floor: float = 0.0) -> dict:
    if not lab_cfg.optuna_enabled:
        return {}
    try:
        from app.research.lab.optuna_wf import walk_forward
        return walk_forward(frame, cands, spread, commission, lab_cfg.optuna_folds, lab_cfg.optuna_trials,
                            lab_cfg.min_trades, min_stop_spread_multiple=floor)
    except ImportError as exc:
        return {"error": f"optuna not installed ({exc}); pip install -r requirements.txt"}
    except Exception as exc:
        log.exception("optuna walk-forward failed")
        return {"error": str(exc)}


def _hypotheses(frames: dict, lab_cfg, spread_by_tf: dict, commission: float, floor: float,
                traded_model=None) -> dict:
    """Score every candidate entry rule on every configured timeframe. Research only; none of this can trade."""
    from app.research.lab.fastsim import grid_train_test
    from app.research.lab.hypotheses import HYPOTHESES
    out = {}
    for name in (lab_cfg.hypotheses or []):
        fn = HYPOTHESES.get(name)
        if fn is None:
            out[name] = {"error": "unknown hypothesis"}
            continue
        per_tf = {}
        for tf in (lab_cfg.hypothesis_timeframes or []):
            frame, htf = frames.get(tf), frames.get("HTF_" + tf)
            if frame is None or len(frame) < 500:
                continue
            try:
                sig = fn(frame, htf, None)
                n_sig = int(sig["long"].sum() + sig["short"].sum())
                entry = {"signals": n_sig, "bars": len(frame)}
                if n_sig < lab_cfg.min_trades:
                    entry["verdict"] = "TOO_FEW_SIGNALS"
                else:
                    sp = spread_by_tf.get(tf)
                    entry.update(grid_train_test(frame.o, frame.h, frame.l, frame.c, sig["long"], sig["short"],
                                                 sig["atr"], lab_cfg.sl_atr_grid, lab_cfg.rr_grid,
                                                 lab_cfg.train_fraction, lab_cfg.min_trades, sp, commission, floor))
                    entry.pop("combos", None)
                    _add_stats(entry, frame, sig, lab_cfg, sp, commission, floor, traded_model)
                    test = (entry.get("best") or {}).get("test") or {}
                    # walk-forward is slow: spend it only where the grid's unseen result is already positive
                    if lab_cfg.optuna_enabled and (test.get("expectancy_r") or 0) > 0:
                        from app.research.lab.optuna_wf import walk_forward_fixed
                        entry["optuna"] = walk_forward_fixed(frame, sig, sp, commission, lab_cfg.optuna_folds,
                                                             lab_cfg.optuna_trials, lab_cfg.min_trades,
                                                             min_stop_spread_multiple=floor)
                per_tf[tf] = entry
            except Exception as exc:
                log.exception("hypothesis %s on %s failed", name, tf)
                per_tf[tf] = {"verdict": "ERROR", "note": str(exc)}
        out[name] = per_tf
    return out


def analyse_symbol(symbol: str, m1, m15, point: float, lab_cfg, micro_params: dict, spread_price=None,
                   commission_price: float = 0.0, tick_spread=None, long_frames: Optional[dict] = None,
                   long_spreads: Optional[dict] = None, traded_model=None) -> dict:
    """micro_scalper's M1 sweep and timeframe study, then every candidate rule.

    `long_frames` ({"M5": frame, "M15": frame}) are the longer histories fetched straight from MT5; when given,
    the rules are scored on them (with `long_spreads`) instead of on M5/M15 resampled from the 60-day M1."""
    import numpy as np
    from app.research.lab.data import resample
    from app.research.lab.signals import apply_volume, trigger_candidates
    entry = {"bars": len(m1), "signals": 0, "verdict": "INSUFFICIENT_DATA"}
    if len(m1) < 3000 or len(m15) < 100:
        entry["note"] = f"only {len(m1)} M1 / {len(m15)} M15 bars"
        return entry
    if spread_price is None:
        spread_price = np.asarray(m1.spread, float) * point
    # Record how much of the input looks like bad prints. A mean-reversion verdict built on a spiky
    # series flatters itself, so the number belongs in the report next to the verdict (Chan ch.7).
    try:
        from app.research.lab.data import despike_report
        entry["data_quality"] = despike_report(m1)
    except Exception:
        log.debug("despike report failed for %s", symbol, exc_info=True)
    entry["costs"] = _cost_summary(m1, point, spread_price, tick_spread, commission_price)
    entry["costs"]["min_stop_spread_multiple"] = _stop_floor(micro_params)
    lookback = int(micro_params.get("volume_lookback", 20))
    cands1 = trigger_candidates(m1, m15, 900, 60, lookback)
    sig = apply_volume(cands1, float(micro_params.get("minimum_relative_volume", 1.2)))
    entry["signals"] = int(sig["long"].sum() + sig["short"].sum())
    timeframes = {}
    if entry["signals"] < lab_cfg.min_trades:
        entry["verdict"] = "TOO_FEW_SIGNALS"
        entry["note"] = f"{entry['signals']} signals in the window"
    else:
        from app.research.lab.vbt_sweep import run_sweep
        sweep = run_sweep(m1, sig, point, lab_cfg.sl_atr_grid, lab_cfg.rr_grid, lab_cfg.train_fraction,
                          lab_cfg.min_trades, spread_price, commission_price, _stop_floor(micro_params))
        entry.update(sweep)
        _add_stats(entry, m1, sig, lab_cfg, spread_price, commission_price, _stop_floor(micro_params),
                   traded_model)
        if lab_cfg.run_backtrader and sweep.get("best"):
            try:
                from app.research.lab.bt_validate import validate
                b = sweep["best"]
                entry["backtrader"] = validate(m1, sig, point, b["sl_atr"], b["rr"],
                                               start_index=int(len(m1) * lab_cfg.train_fraction),
                                               spread_price=spread_price, commission_price=commission_price,
                                               min_stop_spread_multiple=_stop_floor(micro_params))
            except Exception as exc:
                entry["backtrader"] = {"error": str(exc)}
    if "M1" in lab_cfg.timeframes:
        timeframes["M1"] = {k: entry.get(k) for k in ("verdict", "best", "signals", "bars")}
        timeframes["M1"]["optuna"] = _optuna(m1, cands1, spread_price, commission_price, lab_cfg,
                                             _stop_floor(micro_params))
    slower = {"M5": (300, m15, 900), "M15": (900, None, 3600)}
    for name in ("M5", "M15"):
        if name not in lab_cfg.timeframes:
            continue
        try:
            secs, confirm, confirm_s = slower[name]
            trig, sp = resample(m1, secs, spread_price)
            if confirm is None:                     # M15 trigger confirms with H1 built from the longer M15 history
                confirm, _ = resample(m15, 3600)
            tf_entry, cands = _timeframe_entry(name, trig, confirm, confirm_s, secs, sp, lab_cfg, micro_params,
                                               commission_price, traded_model)
            if tf_entry.get("verdict") not in ("TOO_FEW_SIGNALS",):
                tf_entry["optuna"] = _optuna(trig, cands, sp, commission_price, lab_cfg, _stop_floor(micro_params))
            timeframes[name] = tf_entry
        except Exception as exc:
            log.exception("%s %s timeframe research failed", symbol, name)
            timeframes[name] = {"verdict": "ERROR", "note": str(exc)}
    entry["timeframes"] = timeframes
    frames, spreads = {"M1": m1, "HTF_M1": m15}, {"M1": spread_price}
    long_frames, long_spreads = long_frames or {}, long_spreads or {}
    h1_long = resample(m15, 3600)[0]
    for name, secs, htf_secs in (("M5", 300, 3600), ("M15", 900, 3600)):
        try:
            if long_frames.get(name) is not None and len(long_frames[name]) >= 500:
                frames[name], spreads[name] = long_frames[name], long_spreads.get(name)
            else:
                frames[name], spreads[name] = resample(m1, secs, spread_price)
            frames["HTF_" + name] = h1_long
        except Exception:
            log.exception("%s frame for hypotheses failed", name)
    entry["hypothesis_bars"] = {tf: len(f) for tf, f in frames.items() if not tf.startswith("HTF_")}
    entry["hypotheses"] = _hypotheses(frames, lab_cfg, spreads, commission_price, _stop_floor(micro_params),
                                      traded_model)
    return entry


def evidence_cells(symbols: dict) -> dict:
    """Every grid- or rule-scored cell of one cycle that carries a `stats` block, keyed symbol|rule|timeframe."""
    cells = {}
    for sym, e in (symbols or {}).items():
        if not isinstance(e, dict):
            continue
        if e.get("stats"):
            cells[f"{sym}|micro_scalper|M1"] = e
        for tf, x in (e.get("timeframes") or {}).items():
            if tf != "M1" and isinstance(x, dict) and x.get("stats"):
                cells[f"{sym}|micro_scalper|{tf}"] = x
        for name, per in (e.get("hypotheses") or {}).items():
            if not isinstance(per, dict):
                continue
            for tf, x in per.items():
                if isinstance(x, dict) and x.get("stats"):
                    cells[f"{sym}|{name}|{tf}"] = x
        im = e.get("intraday_momentum")
        if isinstance(im, dict) and im.get("stats"):
            cells[f"{sym}|intraday_momentum|M5"] = im
    return cells


def _intraday_momentum(frame, spread, commission: float, lab_cfg, stop_atr: float = 2.0) -> dict:
    """The last half hour into the New York close, in the direction of the move since the previous close.

    One fixed rule from the papers: no grid, so nothing is selected. The 2 x ATR stop only sizes the trade in R;
    the exit is the close (`hold` bars after the fill)."""
    import numpy as np
    from app.research.lab import stats as st
    from app.research.lab.evidence import RANDOM_RUNS
    from app.research.lab.fastsim import bracket_signals, simulate, trade_outcomes
    from app.research.lab.hypotheses import intraday_momentum, session_close_ny
    from app.research.metrics import trade_metrics
    close = session_close_ny(frame) or (16, 0)          # Exness stock CFDs stop at 15:45 New York, not 16:00
    sig = intraday_momentum(frame, None, {"close_hour": close[0], "close_minute": close[1]})
    hold = int(sig.get("hold") or 0)
    n_sig = int(sig["long"].sum() + sig["short"].sum())
    entry = {"rule": "intraday_momentum", "signals": n_sig, "bars": len(frame), "hold_bars": hold,
             "session_close_ny": f"{close[0]:02d}:{close[1]:02d}",
             "best": {"sl_atr": stop_atr, "rr": None, "exit": f"session close {close[0]:02d}:{close[1]:02d} NY"}}
    if hold < 1 or n_sig < 10:
        entry["verdict"] = "TOO_FEW_SIGNALS"
        return entry
    o, h, l, c, atr = frame.o, frame.h, frame.l, frame.c, sig["atr"]
    n = len(o)
    split = int(n * lab_cfg.train_fraction)
    no_target = 1e9
    tr = simulate(o, h, l, c, sig["long"], sig["short"], atr, stop_atr, no_target, spread, commission, 0, split,
                  max_hold=hold)
    te, ent = simulate(o, h, l, c, sig["long"], sig["short"], atr, stop_atr, no_target, spread, commission,
                       split, n, max_hold=hold, return_entries=True)
    pick = ("n_trades", "expectancy_r", "win_rate", "profit_factor")
    entry["train"] = {k: trade_metrics(tr).get(k) for k in pick}
    entry["test"] = {k: trade_metrics(te).get(k) for k in pick}
    ci, sh = st.mean_ci(te), st.sharpe_stats(te)
    block = {"n": ci["n"], "ci": ci, "sr": sh["sr"], "skew": sh["skew"], "kurt": sh["kurt"]}
    if len(ent) >= 2:
        sigs = bracket_signals(ent, atr, spread, stop_atr, float("inf"))
        r_long = trade_outcomes(o, h, l, c, [(j, 1, r, q) for j, _, r, q in sigs], spread, commission,
                                max_hold=hold)
        r_short = trade_outcomes(o, h, l, c, [(j, -1, r, q) for j, _, r, q in sigs], spread, commission,
                                 max_hold=hold)
        block["random"] = st.random_direction(r_long, r_short, np.asarray([x for _, x in ent], float),
                                              RANDOM_RUNS)
    entry["stats"] = block
    t = entry["test"]
    entry["verdict"] = ("TOO_FEW_TEST_TRADES" if (t.get("n_trades") or 0) < 10 else
                        "ROBUST_ON_TEST" if (t.get("expectancy_r") or 0) > 0 and (t.get("profit_factor") or 0) > 1
                        else "NOT_ROBUST_ON_TEST")
    return entry


def _research_every_strategy(cfgm, sym, m15, h1, spec, commission_money, spread_price, lab_cfg,
                             slippage_points: float = 5.0, spread_mult: float = 1.0,
                             keep_trades: bool = False) -> dict:
    """Every configured strategy on one symbol, at its live parameters, via the generic backtester.

    Separate from analyse_symbol because that function is micro_scalper's fast path (vectorised M1 candle
    triggers). This one replays the real M15/H1/H4 strategy objects, which is slower but works for all of
    them - and is the only way the other seven strategies get any research at all."""
    import numpy as np
    from app.research.backtester import BTSettings
    from app.research.lab.strategy_research import research_all

    med_spread_pts = 0.0
    if spread_price is not None and len(spread_price):
        finite = np.asarray(spread_price, float)
        finite = finite[np.isfinite(finite)]
        if len(finite) and spec is not None and getattr(spec, "point", 0):
            med_spread_pts = float(np.median(finite)) / spec.point

    # Costs come from the live cost config, not from optimistic defaults: a research verdict computed on
    # cheaper costs than the account actually charges is the exact failure the cost audit exists to catch.
    # ...and the exit is the TRADED one: breakeven and the profit lock as the trade manager applies them, so the
    # verdict describes the strategy the account actually runs. `spread_mult` lifts MT5's per-bar spreads to
    # what live ticks show (data.spread_ratio).
    settings = BTSettings(slippage_points=max(float(slippage_points), 1.0),
                          commission_per_lot=commission_money,
                          risk_fraction=0.01, spread_mult=max(1.0, float(spread_mult)),
                          execution_exits=getattr(cfgm.app, "execution", None))
    _ = med_spread_pts          # spread is taken from the bars themselves; kept for the report only
    return research_all(cfgm, m15, h1, settings, spec=spec,
                        sessions_cfg=getattr(cfgm.app, "sessions", None),
                        train_fraction=lab_cfg.train_fraction, min_trades=lab_cfg.min_trades,
                        lab_cfg=lab_cfg,      # so each strategy is judged on its own bar
                        keep_trades=keep_trades)


def replay_risk_money(cfg, equity: Optional[float] = None) -> Optional[float]:
    """Money at risk per trade for the replay's illustration: the risk a strategy without proven statistics is
    sized at (risk.research_risk_fraction), on the account's equity (the $5,000 demo when not given)."""
    try:
        return float(equity or 5000.0) * float(cfg.risk.research_risk_fraction)
    except Exception:                                                   # noqa: BLE001
        return None


def nightly_due(now: datetime, hour_utc: int, last_run) -> bool:
    """True when the per-strategy research has not run since the most recent `hour_utc` slot."""
    slot = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if now < slot:
        slot -= timedelta(days=1)
    if not last_run:
        return True
    try:
        last = last_run if isinstance(last_run, datetime) else datetime.fromisoformat(str(last_run))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last < slot


def run_cycle(cfgm, repo, adapter, symbols: Optional[list] = None,
              all_strategies: Optional[bool] = None) -> dict:
    from app.research.lab import data as labdata
    from app.research.lab.store import save_cycle
    from app.market.symbols import resolve
    cfg = cfgm.app
    lab = cfg.research_lab
    micro = next((c.param_values() for c in cfgm.strategies.strategies if c.id == "micro_scalper"), {})
    offset = cfg.broker.broker_utc_offset_hours or 0.0
    names = symbols or lab.symbols or cfg.broker.symbol_candidates
    results = {"created_at": datetime.now(timezone.utc).isoformat(), "symbols": {}}
    # Per-strategy research is slow (full M15/H1/H4 replay per strategy per symbol), so a normal cycle
    # skips it unless asked. The weekend idle window passes all_strategies=True.
    do_all = (lab.research_all_strategies and lab.research_all_in_normal_cycle) \
        if all_strategies is None else bool(all_strategies and lab.research_all_strategies)
    if all_strategies is None and not do_all and lab.research_all_strategies and lab.research_all_nightly \
            and repo is not None:
        from app.research.lab.store import FLAG
        try:
            last = (repo.get_flag(FLAG) or {}).get("strategies_created_at")
        except Exception:
            last = None
        do_all = nightly_due(datetime.now(timezone.utc), lab.research_all_nightly_hour_utc, last)
        if do_all:
            log.info("nightly per-strategy research is due (last run %s)", last or "never")
    per_symbol_strategies: dict = {}
    trend_by_symbol: dict = {}
    other_daily: dict = {}                  # {rule name: {symbol: research_trend result}}
    # The exit every candidate rule would trade with: its own fixed bracket plus the account-wide breakeven and
    # profit lock, so the lab can show what that management adds or costs on the same entries.
    from app.alpha.exits import DEFAULT_BREAKEVEN, with_execution_exits
    traded_model = with_execution_exits({"type": "fixed", "breakeven": dict(DEFAULT_BREAKEVEN)},
                                        getattr(cfg, "execution", None) or {})
    for wanted in names:
        sym = wanted
        try:
            sym = resolve(adapter, wanted) or wanted       # XAUUSDm on Standard = XAUUSD on Raw Spread / Zero
            got = labdata.fetch_frames(adapter, sym, offset, lab.days_m1, lab.days_m15)
            if got is None:
                results["symbols"][sym] = {"verdict": "SYMBOL_UNAVAILABLE", "note": "not on this account"}
                continue
            m1, m15, point = got
            try:
                spec = adapter.symbol_spec(sym)
            except Exception:
                spec = None
            commission = _commission_price(cfg, sym, spec)
            import numpy as np
            tick = labdata.tick_open_spreads(adapter, sym, offset, lab.tick_spread_days, m1.t)
            spread = np.asarray(m1.spread, float) * point
            if tick is not None:
                spread = np.where(np.isfinite(tick), tick, spread)
            # Longer histories straight from MT5, with bar spreads lifted to what the recent ticks show.
            ratio = labdata.spread_ratio(m1, point, tick)
            long = labdata.fetch_long(adapter, sym, offset, {"M5": lab.days_m5, "H4": lab.days_h4})
            long_frames = {"M5": long.get("M5"), "M15": m15}
            long_spreads = {tf: (np.asarray(f.spread, float) * point * ratio if f is not None else None)
                            for tf, f in long_frames.items()}
            results["symbols"][sym] = analyse_symbol(sym, m1, m15, point, lab, micro, spread, commission, tick,
                                                     long_frames=long_frames, long_spreads=long_spreads,
                                                     traded_model=traded_model)
            results["symbols"][sym]["history"] = {
                tf: {"bars": len(f), "from": datetime.fromtimestamp(int(f.t[0]), tz=timezone.utc).date().isoformat()}
                for tf, f in {"M1": m1, "M15": m15, **long}.items() if f is not None and len(f)}
            results["symbols"][sym]["bar_spread_scale"] = round(ratio, 3)
            if lab.trend_research and long.get("H4") is not None:
                try:
                    from app.research.lab.trend import research_trend
                    finite = spread[np.isfinite(spread)]
                    tr = research_trend(long["H4"], spec, float(np.median(finite)) if len(finite) else 0.0,
                                        cfg.cost.default_slippage_points * point, execution=cfg.execution)
                    trend_by_symbol[sym] = tr
                    results["symbols"][sym]["trend_daily"] = {k: v for k, v in tr.items()
                                                              if k not in ("trades", "_alt")}
                except Exception:
                    log.exception("trend research failed for %s", sym)
                # every other daily rule configured on this symbol, replayed on the same history the same way
                for name, kw in _other_daily_rules(cfgm, sym):
                    try:
                        from app.research.lab.trend import research_trend
                        finite = spread[np.isfinite(spread)]
                        res = research_trend(long["H4"], spec, float(np.median(finite)) if len(finite) else 0.0,
                                             cfg.cost.default_slippage_points * point, execution=cfg.execution,
                                             **kw)
                        other_daily.setdefault(name, {})[sym] = res
                        results["symbols"][sym][name] = {k: v for k, v in res.items() if k not in ("trades", "_alt")}
                    except Exception:
                        log.exception("%s research failed for %s", name, sym)
            if lab.intraday_momentum and sym in (lab.intraday_momentum_symbols or []):
                try:
                    m5 = long.get("M5")
                    if m5 is not None and len(m5) >= 500:
                        sp5 = long_spreads["M5"]
                    else:
                        m5, sp5 = labdata.resample(m1, 300, spread)
                    results["symbols"][sym]["intraday_momentum"] = _intraday_momentum(m5, sp5, commission, lab)
                except Exception:
                    log.exception("intraday momentum research failed for %s", sym)
            m15_recent = labdata.last_days(m15, lab.recent_days_m15)
            if lab.strategy_funnel:
                try:      # why the M15/H1 strategies produce (or do not produce) setups, from their own code
                    from app.research.lab.funnel import diagnose
                    h1 = labdata.resample(m15_recent, 3600)[0]
                    med = float(np.nanmedian(spread)) if np.any(np.isfinite(spread)) else 0.0
                    results["symbols"][sym]["strategy_funnel"] = diagnose(cfgm, m15_recent, h1, point, med, sym)
                    if lab.threshold_sweep:
                        from app.research.lab.thresholds import sweep
                        results["symbols"][sym]["threshold_sweep"] = sweep(
                            cfgm, m15_recent, h1, point, med, commission, sym, lab.train_fraction,
                            lab.threshold_sweep_strategies or None)
                except Exception:
                    log.exception("strategy funnel failed for %s", sym)
            if do_all:
                try:
                    h1_all = labdata.resample(m15, 3600)[0]
                    per_symbol_strategies[sym] = _research_every_strategy(
                        cfgm, sym, m15, h1_all, spec, _commission_money(cfg, sym), spread, lab,
                        slippage_points=cfg.cost.default_slippage_points, spread_mult=ratio,
                        keep_trades=True)          # for the replay report; popped before the lab stores
                    log.info("%s: per-strategy research done for %d strategies",
                             sym, len(per_symbol_strategies[sym]))
                except Exception:
                    log.exception("per-strategy research failed for %s", sym)
            log.info("%s: %s (%s signals)", sym, results["symbols"][sym].get("verdict"), results["symbols"][sym].get("signals"))
        except Exception as exc:
            log.exception("research cycle failed for %s", sym)
            results["symbols"][sym] = {"verdict": "ERROR", "note": str(exc)}
    if per_symbol_strategies:
        # The replay report ("how good, how bad") from the same trades; it also strips them off the lab entries.
        try:
            from app.research.lab import replay
            hist = {sym: e.get("history") or {} for sym, e in results["symbols"].items()}
            report = replay.build(replay.cells_from_lab(per_symbol_strategies, trend_by_symbol, hist),
                                  replay_risk_money(cfg), source="nightly lab cycle")
            log.info("replay report saved to %s", replay.save(report))
        except Exception:
            log.exception("replay report failed")
        for per in per_symbol_strategies.values():
            for e in per.values():
                if isinstance(e, dict):
                    e.pop("trades", None)
        from app.research.lab.strategy_research import summarise
        s = summarise(per_symbol_strategies)
        results["strategies"] = s["by_strategy"]
        results["strategy_verdict_counts"] = s["verdict_counts"]
    if lab.evidence_enabled:
        # Deflate every Sharpe by the number of cells LOOKED AT this cycle, kept or not, then grade.
        from app.research.lab.stats import deflate_cells
        try:
            cells = evidence_cells(results["symbols"])
            results["evidence"] = deflate_cells(cells, lab.evidence_min_trades)
            for c in cells.values():
                c["evidence_grade"] = c["stats"].get("grade")
            if per_symbol_strategies:
                scells = {f"{sid}|{sym}": e for sym, per in per_symbol_strategies.items()
                          for sid, e in per.items() if isinstance(e, dict) and e.get("stats")}
                results["strategy_evidence"] = deflate_cells(scells, lab.evidence_min_trades)
                for c in scells.values():
                    c["evidence_grade"] = c["stats"].get("grade")
        except Exception:
            log.exception("evidence deflation failed")
    if trend_by_symbol:
        from app.research.lab.trend import pooled
        try:
            results["trend_daily"] = pooled(trend_by_symbol)
        except Exception:
            log.exception("pooled trend research failed")
        for tr in trend_by_symbol.values():
            tr.pop("_alt", None)
        results["trend_daily_trades"] = {sym: tr.get("trades") or [] for sym, tr in trend_by_symbol.items()}
    for name, per_symbol in other_daily.items():
        from app.research.lab.trend import pooled
        try:
            results[name] = pooled(per_symbol)
        except Exception:
            log.exception("pooled %s research failed", name)
        for res in per_symbol.values():
            res.pop("_alt", None)
        results[f"{name}_trades"] = {sym: res.get("trades") or [] for sym, res in per_symbol.items()}
    try:
        from app.ml.filter import train_all
        results["ml_filter"] = train_all(cfgm, repo)
    except Exception as exc:
        log.exception("learned trade filter training failed")
        results["ml_filter"] = {"error": str(exc)}
    out = save_cycle(results, repo)
    log.info("research lab results saved to %s", out)
    return results


def lab_main(stop_event=None) -> None:
    """Process entry point (multiprocessing 'spawn')."""
    set_low_priority()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s research_lab: %(message)s")
    try:
        import vectorbt, backtrader  # noqa: F401   (optuna / lightgbm are optional: their parts report "not installed")
    except Exception as exc:
        log.warning("research lab disabled: %s (pip install -r requirements.txt)", exc)
        return
    from app.config import ConfigManager, load_secrets
    cfgm, secrets = ConfigManager(), load_secrets()
    repo = None
    if secrets.database_url:
        from app.storage.repo import PostgresRepo
        repo = PostgresRepo(secrets.database_url, application_name="ata_research_lab")
    wait = lambda s: (stop_event.wait(s) if stop_event is not None else time.sleep(s))
    wait(cfgm.app.research_lab.startup_delay_seconds)
    while not (stop_event is not None and stop_event.is_set()):
        lab = cfgm.app.research_lab
        if lab.enabled:
            adapter = None
            try:
                from app.broker.mt5_adapter import MT5Adapter
                adapter = MT5Adapter(None, None, None, secrets.mt5_terminal_path, cfgm.app.broker.connect_timeout_ms)
                adapter.connect()                    # no credentials: attaches to the terminal the bot already uses
                run_cycle(cfgm, repo, adapter)
            except Exception:
                log.exception("research cycle failed; will retry next interval")
            finally:
                try:
                    if adapter is not None:
                        adapter.shutdown()
                except Exception:
                    pass
        # While the markets are shut there is nothing competing for this machine, so spend the window
        # on the work that is too slow to run beside live trading. Research and measurement only - it
        # never promotes a strategy or changes a limit.
        if getattr(lab, "idle_research_enabled", True):
            try:
                from app.research.lab.idle import run_deep_research
                outcome = run_deep_research(cfgm, repo, None)
                if outcome.get("ran"):
                    log.info("idle research completed: %s", ", ".join(outcome.get("plan", [])))
            except Exception:
                log.exception("idle research failed; trading is unaffected")
        wait(max(30, lab.interval_minutes) * 60)


class LabProcess:
    def __init__(self):
        import multiprocessing as mp
        self.ctx = mp.get_context("spawn")
        self.stop = self.ctx.Event()
        self.proc = None

    def start(self) -> None:
        self.proc = self.ctx.Process(target=lab_main, args=(self.stop,), daemon=True, name="research-lab")
        self.proc.start()
        log.info("research lab process started (pid %s)", self.proc.pid)

    def shutdown(self) -> None:
        self.stop.set()
        if self.proc is not None and self.proc.is_alive():
            self.proc.join(timeout=3)
            if self.proc.is_alive():
                self.proc.terminate()
