#!/usr/bin/env python3
"""Why the research gate is, or is not, blocking - per symbol, from the live database.

Runs the SAME code the trading core runs (ConfigManager, symbol_lookup, gate.evaluate), so its verdict cannot
disagree with the bot's. For every symbol it prints the stored research result, the numbers the gate reads, and
the exact clause of evaluate() that decided the outcome.

It distinguishes the five things that look identical from outside:

  STALE        the lab stopped running; results older than gate_max_age_hours are ignored by design
  VERDICT      the lab wrote a verdict the gate does not act on (only NOT_ROBUST_ON_TEST blocks)
  TOO FEW      fewer than gate_min_trades backtest trades, so the gate declines to judge
  ABOVE LIMIT  the numbers were read and the strategy genuinely passed
  BLOCKED      the gate is doing its job

    python tools/audit_research_gate.py
    python tools/audit_research_gate.py --json      # machine-readable, for pasting back
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import ConfigManager, load_secrets                    # noqa: E402
from app.market.symbols import lookup as symbol_lookup                # noqa: E402
from app.research.lab.gate import evaluate                            # noqa: E402

BLOCKING_VERDICT = "NOT_ROBUST_ON_TEST"


def classify(entry, state, cfg) -> str:
    if state.get("blocked"):
        return "BLOCKED"
    if not cfg.gate_live:
        return "GATE OFF"
    if not entry:
        return "NO RESULT"
    reason = state.get("reason", "")
    if "stale" in reason:
        return "STALE"
    if reason.startswith("verdict "):
        return "VERDICT"
    if "backtest trades" in reason:
        return "TOO FEW"
    if "unseen data not negative" in reason or "above the" in reason:
        return "ABOVE LIMIT"
    if "incomplete" in reason:
        return "INCOMPLETE"
    return "PASSED"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    a = ap.parse_args()

    cfgm = ConfigManager()
    cfg = cfgm.app.research_lab
    secrets = load_secrets()
    from app.storage.repo import PostgresRepo
    repo = PostgresRepo(secrets.database_url)

    latest = repo.get_flag("research_lab_latest") or {}
    created_at = latest.get("created_at")
    symbols = list(cfgm.app.broker.symbol_candidates)
    now = datetime.now(timezone.utc)

    rows = []
    for sym in symbols:
        entry = symbol_lookup(latest.get("symbols") or {}, sym, None) or {}
        # the core judges the timeframe micro_scalper actually trades
        tf = str(cfgm.strategy("micro_scalper").param_values().get("trigger_timeframe", "M1")).upper()
        judged = entry if tf == "M1" else ((entry or {}).get("timeframes") or {}).get(tf)
        state = evaluate(judged, created_at, cfg, now)
        train = (judged or {}).get("train") or {}
        test = (judged or {}).get("test") or {}
        rows.append({
            "symbol": sym, "timeframe": tf,
            "verdict": (judged or {}).get("verdict"),
            "signals": (judged or {}).get("signals"),
            "train_n": train.get("n_trades"), "train_r": train.get("expectancy_r"),
            "test_n": test.get("n_trades"), "test_r": test.get("expectancy_r"),
            "pooled_r": state.get("pooled_r"), "pooled_trades": state.get("pooled_trades"),
            "age_hours": state.get("age_hours"),
            "blocked": state.get("blocked"), "outcome": classify(judged, state, cfg),
            "reason": state.get("reason"),
        })

    if a.json:
        print(json.dumps({"config": {"gate_live": cfg.gate_live, "gate_strategies": list(cfg.gate_strategies),
                                     "gate_block_below_r": cfg.gate_block_below_r,
                                     "gate_min_trades": cfg.gate_min_trades,
                                     "gate_max_age_hours": cfg.gate_max_age_hours,
                                     "gate_unseen_alone": cfg.gate_unseen_alone},
                          "lab_result_created_at": str(created_at), "symbols": rows}, indent=2, default=str))
        return 0

    print(f"gate_live={cfg.gate_live}  applies_to={list(cfg.gate_strategies)}  "
          f"block_below={cfg.gate_block_below_r:+.2f}R  min_trades={cfg.gate_min_trades}  "
          f"max_age={cfg.gate_max_age_hours}h  unseen_alone={cfg.gate_unseen_alone}")
    print(f"lab result written: {created_at}\n")
    head = f"{'symbol':<13}{'verdict':<26}{'train':>12}{'test':>12}{'pooled':>9}{'age h':>7}  outcome"
    print(head)
    print("-" * len(head))
    for r in rows:
        tr = f"{r['train_n'] or 0}@{r['train_r']:+.2f}" if r["train_r"] is not None else "-"
        te = f"{r['test_n'] or 0}@{r['test_r']:+.2f}" if r["test_r"] is not None else "-"
        po = f"{r['pooled_r']:+.2f}" if r["pooled_r"] is not None else "-"
        ag = f"{r['age_hours']:.0f}" if r["age_hours"] is not None else "-"
        print(f"{r['symbol']:<13}{str(r['verdict'])[:25]:<26}{tr:>12}{te:>12}{po:>9}{ag:>7}  {r['outcome']}")

    print()
    counts = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {v:>2} symbol(s): {k}")
    nonblocking = [r for r in rows if r["outcome"] == "VERDICT"]
    if nonblocking:
        print(f"\n  NOTE: {len(nonblocking)} symbol(s) carry a verdict the gate does not act on. Only "
              f"'{BLOCKING_VERDICT}' blocks; every other verdict passes through whatever the numbers say.")
        for r in nonblocking:
            print(f"        {r['symbol']:<13}{r['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
