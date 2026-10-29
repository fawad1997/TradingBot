#!/usr/bin/env python3
"""Does the backtest secretly use information from the future?

Chan ch.3 gives a test that is mechanical and decisive, and which three cross-checking engines cannot
substitute for:

  1. Run the backtest over all the data. Save the signals it produced.
  2. Truncate the last N bars. Run again. Save the signals.
  3. On the overlapping region, the two must be IDENTICAL.

Any difference means the first run used data from beyond the truncation point to decide something
inside it. That is look-ahead bias, and it is the single most expensive mistake available in this
field, because it produces a backtest that looks excellent and cannot be traded.

Why agreement between fastsim, VectorBT and Backtrader does not catch it: all three are fed the same
feature frame. If a feature was computed using future bars - a centred moving average, a normalisation
over the whole series, a fill applied backwards - every engine inherits the error and they agree with
each other perfectly while all being wrong.

The published Apress repository for "Quantitative Trading Strategies Using Python" contains exactly
this bug in its headline pairs strategy: the hedge ratio is fitted with OLS over the entire period and
then "backtested" on the same period. A result being in print is no evidence it is sound.

    python tools/lookahead_check.py --symbol XAUUSDm
    python tools/lookahead_check.py --symbol XAUUSDm --truncate 500 --json

Read only. Runs entirely on stored bars; touches no broker and no live state.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_TRUNCATE = 200


def compare_signals(full: Sequence[Any], truncated: Sequence[Any], overlap: int) -> Dict[str, Any]:
    """Compare the two signal series over the region both cover.

    `overlap` is how many leading entries both runs should agree on - the length of the truncated run.
    """
    n = min(overlap, len(full), len(truncated))
    mismatches = []
    for i in range(n):
        a, b = full[i], truncated[i]
        if a != b:
            mismatches.append({"index": i, "full_run": _plain(a), "truncated_run": _plain(b)})
        if len(mismatches) >= 25:
            break
    return {
        "compared": n,
        "mismatches": len(mismatches),
        "clean": len(mismatches) == 0,
        "first_mismatches": mismatches[:10],
    }


def _plain(v):
    try:
        import numpy as np
        if isinstance(v, np.generic):
            return v.item()
    except Exception:
        pass
    return v


def check_frame(frame, build_signals, truncate: int = DEFAULT_TRUNCATE) -> Dict[str, Any]:
    """Run `build_signals` on the full frame and on a truncated copy, then compare the overlap.

    `build_signals(frame) -> sequence` is whatever produces the decisions: a signal array, a position
    series, anything deterministic and index-aligned to the frame.
    """
    n = len(frame.c)
    if n <= truncate + 10:
        return {"ok": False, "note": f"only {n} bars, need more than {truncate + 10} to test"}
    cut = n - truncate
    full = list(build_signals(frame))
    short = list(build_signals(_slice_frame(frame, cut)))
    cmp = compare_signals(full, short, cut)
    cmp["bars"] = n
    cmp["truncated_to"] = cut
    cmp["ok"] = cmp["clean"] and cmp["compared"] > 0
    cmp["verdict"] = ("Nothing was compared: the truncated run produced no decisions, so this proves nothing."
                      if cmp["compared"] == 0 else
                      "No look-ahead detected: both runs agree everywhere they overlap."
                      if cmp["clean"] else
                      f"LOOK-AHEAD BIAS: {cmp['mismatches']} of {cmp['compared']} overlapping decisions "
                      f"changed when future bars were removed. The backtest is using data it would not "
                      f"have had at the time.")
    return cmp


def _slice_frame(frame, cut: int):
    """A copy of the frame holding only its first `cut` bars.

    Uses the project's own frame_slice where available so the truncation matches exactly what the
    research code does elsewhere; falls back to a shallow copy for test doubles. frame_slice cuts by TIME
    (t < end): it was handed the index `cut`, below which no epoch timestamp lies, so every truncated run on a
    real frame was EMPTY, nothing was compared, and the check passed whatever it was given (23 Oct 2026).
    """
    try:
        from app.research.data import frame_slice
        return frame_slice(frame, None, int(frame.t[cut]))       # the first `cut` bars: those before bar `cut`
    except Exception:
        import copy
        out = copy.copy(frame)
        for attr in ("t", "o", "h", "l", "c", "v", "spread", "atr"):
            if hasattr(out, attr):
                try:
                    setattr(out, attr, getattr(out, attr)[:cut])
                except (TypeError, AttributeError):
                    pass
        return out


def rule_checks(m15=None, h4=None, truncate: int = DEFAULT_TRUNCATE, only=None) -> Dict[str, Any]:
    """The truncation test on every rule the live strategies trade: the M15 candidate rules (htf_continuation
    gets its H1 resampled from the SAME, truncated, frame) and the daily rules on daily bars built from H4."""
    from app.research.lab import data as labdata
    from app.research.lab.hypotheses import HYPOTHESES, daily_bars, trend_daily, trend_ensemble, turn_of_month

    def decisions(rule, needs_htf=False):
        def build(f):
            s = rule(f, labdata.resample(f, 3600)[0] if needs_htf else None, None)
            return [int(x) - int(y) for x, y in zip(s["long"], s["short"])]
        return build

    out: Dict[str, Any] = {}
    if m15 is not None:
        for name, rule in HYPOTHESES.items():
            if only in (None, name):
                out[name] = check_frame(m15, decisions(rule, name == "htf_continuation"), truncate)
    if h4 is not None:
        daily = daily_bars(h4)
        for name, rule in (("trend_daily", trend_daily), ("trend_ensemble", trend_ensemble),
                           ("turn_of_month", turn_of_month)):
            if only in (None, name):
                out[name] = check_frame(daily, decisions(rule), min(truncate, max(1, len(daily) // 4)))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Detect look-ahead bias by truncation (Chan ch.3).")
    p.add_argument("--symbol", required=True, help="symbol whose cached bars to test (research/lab_data), e.g. XAUUSDm")
    p.add_argument("--rule", default=None, help="one rule (default: every rule the live strategies trade)")
    p.add_argument("--truncate", type=int, default=DEFAULT_TRUNCATE)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    from app.research.data import load_bars
    root = Path(__file__).resolve().parents[1] / "research"
    found = {}
    for tf in ("M15", "H4"):
        for base in (root / "lab_data", root / "datasets"):
            path = base / f"{a.symbol}_{tf}.csv"
            if path.exists():
                found[tf] = load_bars(path, tf)
                break
    if not found:
        print(f"no cached M15 or H4 bars for {a.symbol} under {root / 'lab_data'} (the research lab writes them)")
        return 2
    results = rule_checks(found.get("M15"), found.get("H4"), a.truncate, a.rule)
    if a.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for name, d in results.items():
            print(f"{name:<18} {'ok ' if d.get('ok') else 'FAIL'} compared={d.get('compared')} "
                  f"mismatches={d.get('mismatches')}  {d.get('verdict') or d.get('note')}")
    return 0 if results and all(d.get("ok") for d in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
