"""Research every strategy, not just micro_scalper.

WHY THIS EXISTS
---------------
Until October 2026 the research lab studied exactly one strategy. `gate_strategies` was `["micro_scalper"]`,
and the lab's fast path (`lab/signals.py`) was hand-written numpy for that strategy's candle trigger. The
other seven strategies traded with no research behind them at all - not because of a policy, but because the
lab had no opinion to give about them.

Nothing in the architecture required that. Every strategy implements the same contract:

    class Strategy(ABC):
        def evaluate(self, v: StrategyView) -> EvalResult: ...

and `app/research/backtester.py` already replays ANY strategy against it, H4 included - which the
micro_scalper fast path explicitly cannot do ("the H4 macro channel is NOT replayed"). So this module is
thin: it drives the existing generic backtester per strategy per symbol and shapes the output into the
schema `lab/gate.py` already understands.

NO PARAMETER SWEEP, AND THAT IS THE POINT
-----------------------------------------
The micro_scalper path sweeps a grid of stop/reward combinations and keeps the best on the training window.
That is defensible for one strategy. Doing it for 8 strategies x 13 symbols would be 104 cells of searching,
and picking winners out of 104 noisy cells manufactures discoveries that do not survive contact with a live
account - the central warning in both Chan ch.3 and Lopez de Prado ch.11.

So this module runs each strategy at its LIVE CONFIGURED PARAMETERS and nothing else. The lab chooses
nothing. Its only output is "at the settings you are actually trading, here is what this strategy did on
this symbol." A veto built on that cannot be inflated by selection, because no selection happened.

WHAT TRAIN/TEST MEANS HERE, HONESTLY
------------------------------------
Because no parameters are fitted, the split is NOT out-of-sample validation in the usual sense - there is no
in-sample fitting for it to validate. It is an out-of-time CONSISTENCY check: did the second period look
like the first, or did the result come from one unrepeatable stretch of market? That is a weaker claim than
the micro_scalper sweep makes, and it is the honest one. Trades straddling the split are purged so the two
windows do not share an outcome.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

from app.research.backtester import BTSettings, run_backtest
from app.research.metrics import trade_metrics

log = logging.getLogger(__name__)

#: Trades whose life overlaps the split boundary belong to neither window (Lopez de Prado ch.7).
DEFAULT_EMBARGO_SECONDS = 24 * 3600

#: A strategy that produced fewer than this many trades in the training window has not been observed enough
#: for any verdict. Deliberately NOT a reason to block - see gate.py's THIN evidence state.
DEFAULT_MIN_TRADES = 30


def split_trades(trades: Sequence[dict], split_time: int,
                 embargo_seconds: int = DEFAULT_EMBARGO_SECONDS) -> dict:
    """Split backtest trades into train/test at `split_time`, purging those that straddle the boundary.

    A trade entered before the split but still open after it carries information across the boundary, so it
    is dropped from both windows rather than assigned to one. The embargo extends that exclusion a little
    past the split, because a trade opened immediately after is still reacting to pre-split conditions."""
    train, test, purged = [], [], 0
    for t in trades:
        entry = t.get("entry_time")
        exit_ = t.get("exit_time") or entry
        if entry is None:
            purged += 1
            continue
        if exit_ is not None and entry < split_time <= exit_:
            purged += 1                                  # straddles the boundary
        elif entry < split_time:
            train.append(t)
        elif entry < split_time + embargo_seconds:
            purged += 1                                  # inside the embargo
        else:
            test.append(t)
    return {"train": train, "test": test, "purged": purged}


def _rs(trades: Sequence[dict]) -> List[float]:
    return [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]


def _verdict(train_m: dict, test_m: dict, min_trades: int) -> str:
    """Same shape of judgement the micro_scalper sweep makes, so one gate can read both."""
    if train_m.get("n_trades", 0) < min_trades:
        return "TOO_FEW_TRADES"
    need = max(10, min_trades // 2)
    if test_m.get("n_trades", 0) < need:
        return "TOO_FEW_TEST_TRADES"
    ok = (test_m.get("expectancy_r") or 0) > 0 and (test_m.get("profit_factor") or 0) > 1.0
    return "ROBUST_ON_TEST" if ok else "NOT_ROBUST_ON_TEST"


def research_one(scfg, m15, h1, settings: BTSettings, spec=None, sessions_cfg=None, calendar=None,
                 m1=None, train_fraction: float = 0.7, min_trades: int = DEFAULT_MIN_TRADES,
                 embargo_seconds: int = DEFAULT_EMBARGO_SECONDS) -> dict:
    """One strategy on one symbol, at its live settings. Returns an entry `lab/gate.py` can evaluate."""
    if m15 is None or len(m15) < 100:
        return {"verdict": "INSUFFICIENT_DATA", "note": f"{0 if m15 is None else len(m15)} M15 bars",
                "strategy_id": scfg.id}

    t0, t1 = int(m15.t[0]), int(m15.t[-1])
    split_time = int(t0 + (t1 - t0) * train_fraction)

    res = run_backtest(scfg, m15, h1, settings, spec=spec, sessions_cfg=sessions_cfg,
                       calendar=calendar, m1=m1)
    parts = split_trades(res.trades, split_time, embargo_seconds)
    train_m = trade_metrics(_rs(parts["train"]))
    test_m = trade_metrics(_rs(parts["test"]))

    return {
        "strategy_id": scfg.id,
        "strategy_version": getattr(scfg, "version", None),
        "verdict": _verdict(train_m, test_m, min_trades),
        "bars": len(m15),
        "signals": len(res.trades),
        # `best` is the live configuration, NOT a tuned pick. Named `best` only so the stored shape matches
        # the micro_scalper path that the gate and dashboard already read.
        "best": {"source": "live_config", "tuned": False},
        "train": {k: train_m.get(k) for k in ("n_trades", "expectancy_r", "win_rate", "profit_factor")},
        "test": {k: test_m.get(k) for k in ("n_trades", "expectancy_r", "win_rate", "profit_factor")},
        "purged_trades": parts["purged"],
        "split_time": split_time,
        # The bar THIS strategy was judged against, and the dispersion behind its numbers. Both are
        # needed to set a sensible per-strategy threshold later instead of reusing a global constant.
        "min_trades_applied": min_trades,
        "r_sd": round(train_m.get("sd") or 0.0, 4) or None,
        "trades_per_1000_bars": round(1000.0 * len(res.trades) / max(1, len(m15)), 2),
        "note": "live parameters, no sweep - out-of-time consistency check, not out-of-sample validation",
    }


def min_trades_for(lab_cfg, strategy_id: str, default: int = DEFAULT_MIN_TRADES) -> int:
    """This strategy's own evidence bar, not a shared one.

    A strategy that trades a fifth as often as another needs a correspondingly lower bar, or it stays
    permanently unjudged while the busy strategy gets a verdict off the same history. Falls back to the
    lab's global `gate_min_trades`, then to `default`."""
    over = (getattr(lab_cfg, "gate_overrides", None) or {}).get(strategy_id) if lab_cfg else None
    v = getattr(over, "min_trades", None) if over is not None else None
    if v is not None:
        return int(v)
    return int(getattr(lab_cfg, "gate_min_trades", default) if lab_cfg else default)


def research_all(cfgm, m15, h1, settings: BTSettings, spec=None, sessions_cfg=None, calendar=None,
                 m1=None, strategy_ids: Optional[Sequence[str]] = None,
                 train_fraction: float = 0.7, min_trades: int = DEFAULT_MIN_TRADES,
                 lab_cfg=None) -> Dict[str, dict]:
    """Every configured strategy on one symbol, each judged against ITS OWN evidence bar.

    A strategy that raises is recorded, not allowed to stop the rest."""
    out: Dict[str, dict] = {}
    for scfg in cfgm.strategies.strategies:
        if strategy_ids is not None and scfg.id not in strategy_ids:
            continue
        try:
            n = min_trades_for(lab_cfg, scfg.id, min_trades) if lab_cfg is not None else min_trades
            out[scfg.id] = research_one(scfg, m15, h1, settings, spec=spec, sessions_cfg=sessions_cfg,
                                        calendar=calendar, m1=m1, train_fraction=train_fraction,
                                        min_trades=n)
        except Exception as exc:                                      # noqa: BLE001
            log.exception("per-strategy research failed for %s", scfg.id)
            out[scfg.id] = {"strategy_id": scfg.id, "verdict": "ERROR", "note": str(exc)}
    return out


def summarise(per_symbol: Dict[str, Dict[str, dict]]) -> dict:
    """Flip {symbol: {strategy: entry}} into {strategy: {symbol: entry}} and count the verdicts.

    The gate looks up by strategy first, and a human reading the report wants one row per strategy rather
    than hunting through thirteen symbol blocks."""
    by_strategy: Dict[str, Dict[str, dict]] = {}
    for sym, per_strat in (per_symbol or {}).items():
        for sid, entry in (per_strat or {}).items():
            by_strategy.setdefault(sid, {})[sym] = entry

    counts = {}
    for sid, syms in by_strategy.items():
        tally: Dict[str, int] = {}
        for entry in syms.values():
            v = entry.get("verdict") or "UNKNOWN"
            tally[v] = tally.get(v, 0) + 1
        counts[sid] = tally
    return {"by_strategy": by_strategy, "verdict_counts": counts}
