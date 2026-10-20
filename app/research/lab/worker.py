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
from datetime import datetime, timezone
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


def _commission_price(cfg, symbol: str, spec) -> float:
    """Round-trip commission per lot expressed as a price move (what the price must travel to pay it)."""
    from app.market.symbols import lookup
    money = float(lookup(cfg.cost.symbol_commission_per_lot_round_trip, symbol, cfg.cost.commission_per_lot_round_trip))
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


def _timeframe_entry(name, trig, confirm, confirm_s, trig_s, spread, lab_cfg, micro_params, commission):
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
    return entry, cands


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


def _hypotheses(frames: dict, lab_cfg, spread_by_tf: dict, commission: float, floor: float) -> dict:
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
                   commission_price: float = 0.0, tick_spread=None) -> dict:
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
                                               commission_price)
            if tf_entry.get("verdict") not in ("TOO_FEW_SIGNALS",):
                tf_entry["optuna"] = _optuna(trig, cands, sp, commission_price, lab_cfg, _stop_floor(micro_params))
            timeframes[name] = tf_entry
        except Exception as exc:
            log.exception("%s %s timeframe research failed", symbol, name)
            timeframes[name] = {"verdict": "ERROR", "note": str(exc)}
    entry["timeframes"] = timeframes
    frames, spreads = {"M1": m1, "HTF_M1": m15}, {"M1": spread_price}
    for name, secs, htf_secs in (("M5", 300, 3600), ("M15", 900, 3600)):
        try:
            trig, sp = resample(m1, secs, spread_price)
            frames[name], spreads[name] = trig, sp
            frames["HTF_" + name] = resample(m15, htf_secs)[0]
        except Exception:
            log.exception("%s resample for hypotheses failed", name)
    entry["hypotheses"] = _hypotheses(frames, lab_cfg, spreads, commission_price, _stop_floor(micro_params))
    return entry


def _research_every_strategy(cfgm, sym, m15, h1, spec, commission_money, spread_price, lab_cfg,
                             slippage_points: float = 5.0) -> dict:
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
    settings = BTSettings(slippage_points=max(float(slippage_points), 1.0),
                          commission_per_lot=commission_money,
                          risk_fraction=0.01)
    _ = med_spread_pts          # spread is taken from the bars themselves; kept for the report only
    return research_all(cfgm, m15, h1, settings, spec=spec,
                        sessions_cfg=getattr(cfgm.app, "sessions", None),
                        train_fraction=lab_cfg.train_fraction, min_trades=lab_cfg.min_trades,
                        lab_cfg=lab_cfg)      # so each strategy is judged on its own bar


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
    per_symbol_strategies: dict = {}
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
            results["symbols"][sym] = analyse_symbol(sym, m1, m15, point, lab, micro, spread, commission, tick)
            if lab.strategy_funnel:
                try:      # why the M15/H1 strategies produce (or do not produce) setups, from their own code
                    from app.research.lab.funnel import diagnose
                    h1 = labdata.resample(m15, 3600)[0]
                    med = float(np.nanmedian(spread)) if np.any(np.isfinite(spread)) else 0.0
                    results["symbols"][sym]["strategy_funnel"] = diagnose(cfgm, m15, h1, point, med, sym)
                    if lab.threshold_sweep:
                        from app.research.lab.thresholds import sweep
                        results["symbols"][sym]["threshold_sweep"] = sweep(
                            cfgm, m15, h1, point, med, commission, sym, lab.train_fraction,
                            lab.threshold_sweep_strategies or None)
                except Exception:
                    log.exception("strategy funnel failed for %s", sym)
            if do_all:
                try:
                    h1_all = labdata.resample(m15, 3600)[0]
                    per_symbol_strategies[sym] = _research_every_strategy(
                        cfgm, sym, m15, h1_all, spec, commission, spread, lab,
                        slippage_points=cfg.cost.default_slippage_points)
                    log.info("%s: per-strategy research done for %d strategies",
                             sym, len(per_symbol_strategies[sym]))
                except Exception:
                    log.exception("per-strategy research failed for %s", sym)
            log.info("%s: %s (%s signals)", sym, results["symbols"][sym].get("verdict"), results["symbols"][sym].get("signals"))
        except Exception as exc:
            log.exception("research cycle failed for %s", sym)
            results["symbols"][sym] = {"verdict": "ERROR", "note": str(exc)}
    if per_symbol_strategies:
        from app.research.lab.strategy_research import summarise
        s = summarise(per_symbol_strategies)
        results["strategies"] = s["by_strategy"]
        results["strategy_verdict_counts"] = s["verdict_counts"]
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
