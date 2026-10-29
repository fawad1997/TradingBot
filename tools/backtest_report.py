#!/usr/bin/env python3
"""Replay every configured strategy over all the history the broker gives, and write the "how good, how bad"
report the dashboard's Analysis page shows (research/results/backtest/latest.json).

The same machinery as the lab's nightly per-strategy research: live parameters, live costs (bar spreads lifted
to what recent ticks show, slippage, commission, swap), the exits the trade manager applies, a 70/30 split with
the last 30% graded for evidence. trend_daily_v1 is replayed on daily candles from all H4 history instead,
because its 100-day indicators would eat most of two years of M15.

Read-only towards the broker (market data only) and runs in its own processes at low priority, so the live bot
is not touched. Writes nothing to the database.

    python tools/backtest_report.py                   # every symbol and strategy
    python tools/backtest_report.py --symbols EURUSDm NVDAm --workers 2
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

log = logging.getLogger("backtest_report")


def _fetch(adapter, cfg, sym: str):
    """Everything one symbol's replay needs, fetched from MT5 in the main process."""
    import numpy as np
    from app.research.lab import data as labdata
    from app.research.lab.worker import _commission_money
    lab = cfg.research_lab
    offset = cfg.broker.broker_utc_offset_hours or 0.0
    got = labdata.fetch_frames(adapter, sym, offset, lab.days_m1, lab.days_m15)
    if got is None:
        return None
    m1, m15, point = got
    spec = adapter.symbol_spec(sym)
    tick = labdata.tick_open_spreads(adapter, sym, offset, lab.tick_spread_days, m1.t)
    spread = np.asarray(m1.spread, float) * point
    if tick is not None:
        spread = np.where(np.isfinite(tick), tick, spread)
    long = labdata.fetch_long(adapter, sym, offset, {"H4": lab.days_h4})
    finite = spread[np.isfinite(spread)]
    history = {tf: {"bars": len(f), "from": datetime.fromtimestamp(int(f.t[0]), tz=timezone.utc).date().isoformat()}
               for tf, f in {"M15": m15, **long}.items() if f is not None and len(f)}
    return {"sym": sym, "m15": m15, "h4": long.get("H4"), "spec": spec, "point": point,
            # money per lot: it goes to BTSettings.commission_per_lot, not to the fast simulator
            "commission": _commission_money(cfg, sym), "ratio": labdata.spread_ratio(m1, point, tick),
            "median_spread": float(np.median(finite)) if len(finite) else 0.0, "history": history}


def _replay(job: dict) -> dict:
    """One symbol: every M15 strategy through the backtester, and the daily trend rule. Runs in a worker."""
    from app.config import ConfigManager
    from app.research.backtester import BTSettings
    from app.research.lab import data as labdata
    from app.research.lab.strategy_research import research_all
    from app.research.lab.worker import set_low_priority
    set_low_priority()
    cfgm = ConfigManager()
    cfg, lab = cfgm.app, cfgm.app.research_lab
    t0 = time.time()
    m15 = job["m15"]
    h1 = labdata.resample(m15, 3600)[0]
    settings = BTSettings(slippage_points=max(float(cfg.cost.default_slippage_points), 1.0),
                          commission_per_lot=job["commission"], risk_fraction=0.01,
                          spread_mult=max(1.0, float(job["ratio"])), execution_exits=getattr(cfg, "execution", None))
    ids = [s.id for s in cfgm.strategies.strategies if s.id != "trend_daily_v1"]
    per = research_all(cfgm, m15, h1, settings, spec=job["spec"], sessions_cfg=getattr(cfg, "sessions", None),
                       strategy_ids=ids, train_fraction=lab.train_fraction, min_trades=lab.min_trades,
                       lab_cfg=lab, keep_trades=True)
    trend = None
    if job.get("h4") is not None and len(job["h4"]):
        from app.research.lab.trend import research_trend
        trend = research_trend(job["h4"], job["spec"], job["median_spread"],
                               cfg.cost.default_slippage_points * job["point"], execution=cfg.execution)
        trend.pop("_alt", None)
    return {"sym": job["sym"], "per": per, "trend": trend, "history": job["history"], "seconds": time.time() - t0}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="*", help="default: the lab's symbols")
    ap.add_argument("--workers", type=int, default=3, help="parallel replay processes (default 3)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    from app.broker.mt5_adapter import MT5Adapter
    from app.config import ConfigManager, load_secrets
    from app.market.symbols import resolve
    from app.research.lab import replay
    from app.research.lab.stats import deflate_cells
    from app.research.lab.worker import replay_risk_money, set_low_priority
    set_low_priority()
    cfgm, secrets = ConfigManager(), load_secrets()
    cfg = cfgm.app
    names = args.symbols or cfg.research_lab.symbols or cfg.broker.symbol_candidates
    adapter = MT5Adapter(None, None, None, secrets.mt5_terminal_path, cfg.broker.connect_timeout_ms)
    adapter.connect()                                   # attaches to the terminal the bot already uses
    equity = None
    try:
        jobs = []
        for wanted in names:
            sym = resolve(adapter, wanted) or wanted
            got = _fetch(adapter, cfg, sym)
            if got is None:
                log.warning("%s: not on this account, skipped", sym)
                continue
            log.info("%s: history %s", sym, got["history"])
            jobs.append(got)
        try:
            import MetaTrader5 as mt5
            acc = mt5.account_info()
            equity = float(acc.equity) if acc is not None else None
        except Exception:                                                   # noqa: BLE001
            equity = None
    finally:
        adapter.shutdown()

    per_symbol, trend_by_symbol, history = {}, {}, {}
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {pool.submit(_replay, j): j["sym"] for j in jobs}
        for f in as_completed(futs):
            sym = futs[f]
            try:
                out = f.result()
            except Exception:
                log.exception("%s: replay failed", sym)
                continue
            per_symbol[sym], history[sym] = out["per"], out["history"]
            if out["trend"] is not None:
                trend_by_symbol[sym] = out["trend"]
            log.info("%s: replayed %d strategies in %.0f s", sym, len(out["per"]), out["seconds"])

    # Grade every strategy cell against the number of cells looked at, as the nightly lab does.
    scells = {f"{sid}|{sym}": e for sym, per in per_symbol.items() for sid, e in per.items()
              if isinstance(e, dict) and e.get("stats")}
    if scells:
        deflate_cells(scells, cfg.research_lab.evidence_min_trades)
    report = replay.build(replay.cells_from_lab(per_symbol, trend_by_symbol, history),
                          replay_risk_money(cfg, equity), source="tools/backtest_report.py")
    report["history"] = history
    path = replay.save(report)
    print(f"\nreport: {path}")
    for sid, s in sorted(report["strategies"].items()):
        p = s["pooled"]
        if not p.get("n"):
            print(f"  {sid:22s} no trades")
            continue
        print(f"  {sid:22s} {p['n']:5d} trades  win {p['win_rate']:.0%}  avg {p['avg_r']:+.3f}R "
              f"[{p['lo']}, {p['hi']}]  total {p['total_r']:+.1f}R  worst drawdown {p['max_drawdown_r']:.1f}R  "
              f"worst month {p['worst_month']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
