"""Where research-lab results go: files (research/results/lab/<time>/), the database and a small live summary flag."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import ROOT

RESULTS = ROOT / "research" / "results" / "lab"
FLAG = "research_lab_latest"


def compact_stats(st: Optional[dict]) -> Optional[dict]:
    """The evidence block without its working (bootstrap block length, split counts, skew and kurtosis)."""
    if not st:
        return None
    ci = st.get("ci") or {}
    rnd = st.get("random") or {}
    pbo = st.get("pbo") or {}
    out = {"grade": st.get("grade"), "n": st.get("n"), "mean": ci.get("mean"), "lo": ci.get("lo"),
           "hi": ci.get("hi"), "sr": st.get("sr"), "dsr": st.get("dsr"),
           "pbo": pbo.get("pbo") if isinstance(pbo, dict) else pbo,
           "random_p": rnd.get("p_value"), "edge_vs_random": rnd.get("edge_vs_random")}
    ex = st.get("exits") or {}
    if ex:
        out["exits"] = {k: (v or {}).get("mean") for k, v in ex.items()}
    return out


def _compact(entry: dict) -> dict:
    best = entry.get("best") or {}
    pick = lambda m, *k: {x: (m or {}).get(x) for x in k}
    out = {"verdict": entry.get("verdict"), "bars": entry.get("bars"), "signals": entry.get("signals"),
           "best": {"sl_atr": best.get("sl_atr"), "rr": best.get("rr")} if best else None,
           "train": pick(best.get("train"), "n_trades", "expectancy_r", "win_rate", "profit_factor") if best else None,
           "test": pick(best.get("test"), "n_trades", "expectancy_r", "win_rate", "profit_factor") if best else None,
           "backtrader": pick(entry.get("backtrader"), "n_trades", "expectancy_r", "win_rate", "profit_factor"),
           "note": entry.get("note")}
    if entry.get("stats"):
        out["evidence_grade"] = entry.get("evidence_grade") or entry["stats"].get("grade")
        out["stats"] = compact_stats(entry["stats"])
    return out


def _compact_trend(e: dict) -> dict:
    st = e.get("stats") or {}
    return {"verdict": e.get("verdict"), "days": e.get("days"), "metrics": e.get("metrics"),
            "avg_nights": e.get("avg_nights"), "avg_swap_r": e.get("avg_swap_r"), "stats": compact_stats(st),
            "exits": {k: (v or {}).get("mean") for k, v in (e.get("exits") or {}).items()} or None,
            "halves": [(h or {}).get("mean") for h in (e.get("halves") or [])] or None}


def _compact_optuna(o: dict) -> dict:
    if not o:
        return {}
    if o.get("error"):
        return {"error": o["error"]}
    return {"verdict": o.get("verdict"), "walk_forward": o.get("walk_forward"),
            "params_on_all_data": o.get("params_on_all_data")}


def _compact_symbol(entry: dict) -> dict:
    out = _compact(entry)
    if entry.get("costs"):
        out["costs"] = entry["costs"]
    if entry.get("strategy_funnel"):
        out["strategy_funnel"] = entry["strategy_funnel"]
    if entry.get("threshold_sweep"):
        out["threshold_sweep"] = entry["threshold_sweep"]
    hyp = entry.get("hypotheses") or {}
    if hyp:
        out["hypotheses"] = {name: ({"error": per["error"]} if isinstance(per, dict) and "error" in per else
                                    {tf: {**_compact(e), "signals": e.get("signals"),
                                          "optuna": _compact_optuna(e.get("optuna") or {})}
                                     for tf, e in (per or {}).items()})
                             for name, per in hyp.items()}
    tfs = entry.get("timeframes") or {}
    if tfs:
        out["timeframes"] = {tf: {**_compact(e), "optuna": _compact_optuna(e.get("optuna") or {})}
                             for tf, e in tfs.items()}
    for key in ("history", "hypothesis_bars", "bar_spread_scale"):
        if entry.get(key) is not None:
            out[key] = entry[key]
    if entry.get("trend_daily"):
        out["trend_daily"] = _compact_trend(entry["trend_daily"])
    im = entry.get("intraday_momentum")
    if im:
        out["intraday_momentum"] = {**_compact(im), "hold_bars": im.get("hold_bars"),
                                    "train": im.get("train"), "test": im.get("test")}
    return out


def _slim_strategy(e: dict) -> dict:
    """A per-strategy entry as the gate and the selector read it, with its evidence block compacted."""
    if not isinstance(e, dict) or not e.get("stats"):
        return e
    out = {k: v for k, v in e.items() if k != "stats"}
    out["stats"] = compact_stats(e["stats"])
    out["evidence_grade"] = e.get("evidence_grade") or e["stats"].get("grade")
    return out


def save_cycle(results: dict, repo=None, root: Optional[Path] = None) -> Path:
    root = Path(root) if root else RESULTS
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = root / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    (root / "latest.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    for sym, e in results.get("symbols", {}).items():
        combos = e.get("combos") or []
        if combos:
            with open(out / f"{sym}_vectorbt_grid.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["sl_atr", "rr", "train_n", "train_expectancy_r", "train_pf", "test_n", "test_expectancy_r", "test_pf"])
                for c in combos:
                    t, v = c["train"], c["test"]
                    w.writerow([c["sl_atr"], c["rr"], t.get("n_trades"), t.get("expectancy_r"), t.get("profit_factor"),
                                v.get("n_trades"), v.get("expectancy_r"), v.get("profit_factor")])
    compact = {"created_at": results["created_at"], "report_dir": str(out),
               "symbols": {s: _compact_symbol(e) for s, e in results.get("symbols", {}).items()},
               "ml_filter": results.get("ml_filter")}
    for key in ("evidence", "trend_daily"):            # the cycle's trial count and grades; pooled trend result
        if results.get(key):
            compact[key] = results[key]
    # Per-strategy research, keyed {strategy_id: {symbol: entry}}. Already compact (no combo grid, because
    # nothing is swept), so it is stored as produced. The trading core reads it for any strategy that is
    # not micro_scalper; see TradingCore.research_gate.
    # Per-strategy research is slow and does not run every cycle. A cycle without it carries the previous result
    # forward with its OWN timestamp, so the gate and the selector still see it - and still see how old it is -
    # instead of losing it to a cycle that never looked.
    if results.get("strategies"):
        compact["strategies"] = {sid: {sym: _slim_strategy(e) for sym, e in (per or {}).items()}
                                 for sid, per in results["strategies"].items()}
        compact["strategies_created_at"] = results["created_at"]
        compact["strategy_verdict_counts"] = results.get("strategy_verdict_counts") or {}
        if results.get("strategy_evidence"):
            compact["strategy_evidence"] = results["strategy_evidence"]
    elif repo is not None:
        try:
            prev = repo.get_flag(FLAG) or {}
        except Exception:
            prev = {}
        if prev.get("strategies"):
            compact["strategies"] = prev["strategies"]
            compact["strategies_created_at"] = prev.get("strategies_created_at") or prev.get("created_at")
            compact["strategy_verdict_counts"] = prev.get("strategy_verdict_counts") or {}
            if prev.get("strategy_evidence"):
                compact["strategy_evidence"] = prev["strategy_evidence"]
    if repo is not None:
        try:
            for sym, e in results.get("symbols", {}).items():
                c = _compact(e)
                repo.insert("research_lab_runs", {
                    "created_at": datetime.fromisoformat(results["created_at"]), "symbol": sym, "bars": e.get("bars"),
                    "signals": e.get("signals"), "verdict": e.get("verdict"), "best": c["best"], "train": c["train"],
                    "test": c["test"], "backtrader": c["backtrader"], "report_path": str(out), "note": e.get("note")})
            repo.set_flag(FLAG, compact, "research_lab")
        except Exception as exc:                      # the database is optional for research output
            (out / "db_error.txt").write_text(str(exc), encoding="utf-8")
    return out
