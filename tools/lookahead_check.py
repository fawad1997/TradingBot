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
    cmp["ok"] = cmp["clean"]
    cmp["verdict"] = ("No look-ahead detected: both runs agree everywhere they overlap."
                      if cmp["clean"] else
                      f"LOOK-AHEAD BIAS: {cmp['mismatches']} of {cmp['compared']} overlapping decisions "
                      f"changed when future bars were removed. The backtest is using data it would not "
                      f"have had at the time.")
    return cmp


def _slice_frame(frame, cut: int):
    """A copy of the frame holding only its first `cut` bars.

    Uses the project's own frame_slice where available so the truncation matches exactly what the
    research code does elsewhere; falls back to a shallow copy for test doubles.
    """
    try:
        from app.research.data import frame_slice
        return frame_slice(frame, None, cut)
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


def main() -> int:
    p = argparse.ArgumentParser(description="Detect look-ahead bias by truncation (Chan ch.3).")
    p.add_argument("--m15", required=True, help="path to the M15 CSV, as used by `backtest`")
    p.add_argument("--truncate", type=int, default=DEFAULT_TRUNCATE)
    p.add_argument("--utc-offset", type=float, default=0.0)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    from app.config import ConfigManager
    from app.research.data import load_bars
    from app.research.lab.signals import apply_volume, trigger_candidates

    frame = load_bars(Path(a.m15), "M15", a.utc_offset)
    if frame is None or len(frame.c) == 0:
        print(f"no bars loaded from {a.m15}")
        return 2

    cfgm = ConfigManager()
    micro = next((s for s in cfgm.strategies.strategies if s.id == "micro_scalper"), None)
    params = dict((micro.params if micro else {}) or {})
    lookback = int(params.get("volume_lookback", 20))
    min_rv = float(params.get("minimum_relative_volume", 1.2))

    def build(f):
        cands = trigger_candidates(f, f, 900, 60, lookback)
        s = apply_volume(cands, min_rv)
        return [int(x) - int(y) for x, y in zip(s["long"], s["short"])]

    d = check_frame(frame, build, a.truncate)
    print(json.dumps(d, indent=2, default=str) if a.json else
          f"{d.get('verdict')}\n  bars={d.get('bars')} compared={d.get('compared')} "
          f"mismatches={d.get('mismatches')}")
    return 0 if d.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
