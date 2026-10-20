"""Research experiments -> evidence packages.

Each experiment gets an id, is stored in `experiments` (+ `backtest_trades`) when
a database is configured, and writes a Markdown report + JSON + trades CSV to
research/reports/. A human approves the EVIDENCE PACKAGE with `promote`.
"""
from __future__ import annotations

import csv
import json
import logging
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from app.config import ROOT, ConfigManager, StrategyCfg
from app.core.types import EvidenceLevel, SymbolSpec, new_id, utcnow
from app.features.frame import FeatureFrame
from app.market.events import EventCalendar
from app.research.backtester import BTResult, BTSettings, regime_series, run_backtest
from app.research.metrics import classify_evidence, deflated_sharpe, monte_carlo, probabilistic_sharpe, welch_p_value
from app.research.walk_forward import walk_forward
from app.storage.repo import Repository, to_jsonable

log = logging.getLogger(__name__)
REPORTS = ROOT / "research" / "reports"

EXIT_VARIANTS = {
    "A_fixed_2R": {"type": "fixed", "target_r": 2.0, "partial": {"enabled": False},
                   "time_stop": {"enabled": False}, "thesis_invalidation": False},
    "B_structure_trail": {"type": "structure_trail", "target_r": None, "partial": {"enabled": False},
                          "trail": {"activate_r": 1.0, "pivot_n": 3, "buffer_atr": 0.2},
                          "time_stop": {"enabled": False}, "thesis_invalidation": False},
    "C_atr_trail": {"type": "atr_trail", "target_r": None, "partial": {"enabled": False},
                    "trail": {"activate_r": 1.0, "atr_mult": 2.5}, "time_stop": {"enabled": False},
                    "thesis_invalidation": False},
    "D_chandelier": {"type": "chandelier", "target_r": None, "partial": {"enabled": False},
                     "trail": {"activate_r": 1.0, "atr_mult": 3.0}, "time_stop": {"enabled": False},
                     "thesis_invalidation": False},
    "E_partial_plus_trail": {"type": "structure_trail", "target_r": None,
                             "partial": {"enabled": True, "at_r": 1.0, "fraction": 0.5, "move_sl_to_be": True},
                             "trail": {"activate_r": 1.0, "pivot_n": 3, "buffer_atr": 0.2},
                             "time_stop": {"enabled": False}, "thesis_invalidation": False},
    "F_fixed_2R_time_stop": {"type": "fixed", "target_r": 2.0, "partial": {"enabled": False},
                             "time_stop": {"enabled": True, "max_bars": 24, "min_r": 0.25},
                             "thesis_invalidation": False},
    "G_fixed_2R_thesis": {"type": "fixed", "target_r": 2.0, "partial": {"enabled": False},
                          "time_stop": {"enabled": False}, "thesis_invalidation": True},
}

HYPOTHESES = {
    "H1": "Pullbacks during London/NY overlap outperform Asian pullbacks",
    "H2": "Shallow pullbacks outperform deep pullbacks",
    "H3": "Higher signal quality (momentum recovery strength) has higher continuation",
    "H4": "Certain volatility regimes produce higher expectancy",
    "H5": "Certain session/event combinations destroy expectancy",
    "H6": "Time-based exits reduce negative drift",
    "H7": "Structure-based trailing increases average winner",
    "H8": "Partial exits improve risk-adjusted expectancy",
}


# ----------------------------------------------------------------- reporting
def _fmt(v):
    if isinstance(v, float):
        return f"{v:.4f}"
    if isinstance(v, dict):
        return json.dumps(v)
    return str(v)


def md_table(rows: Dict[str, Dict]) -> str:
    if not rows:
        return "_no data_\n"
    cols = sorted({c for r in rows.values() for c in r})
    out = "| key | " + " | ".join(cols) + " |\n|---|" + "---|" * len(cols) + "\n"
    for k, r in rows.items():
        out += f"| {k} | " + " | ".join(_fmt(r.get(c, "")) for c in cols) + " |\n"
    return out


def md_kv(d: Dict) -> str:
    return "| metric | value |\n|---|---|\n" + "".join(f"| {k} | {_fmt(v)} |\n" for k, v in d.items())


def save_report(kind: str, sid: str, title: str, sections: List[tuple], data: dict, trades: Optional[List[dict]],
                exp_id: str) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base = REPORTS / f"{stamp}_{kind.lower()}_{sid}"
    md = [f"# {title}", "", f"Experiment id: `{exp_id}`  ", f"Created: {utcnow().isoformat()}", "",
          "> Research results describe the past under modelled costs. They are evidence, not a promise.", ""]
    for heading, body in sections:
        md += [f"## {heading}", "", body, ""]
    base.with_suffix(".md").write_text("\n".join(md), encoding="utf-8")
    base.with_suffix(".json").write_text(json.dumps(to_jsonable(data), indent=2), encoding="utf-8")
    if trades:
        keys = [k for k in trades[0].keys() if k != "features"]
        with open(str(base) + "_trades.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(trades)
    return base.with_suffix(".md")


def persist(repo: Optional[Repository], exp: dict, trades: Optional[List[dict]]) -> None:
    if repo is None:
        return
    try:
        repo.insert("experiments", exp)
        if trades:
            rows = []
            for t in trades:
                rows.append({"experiment_id": exp["id"], "strategy_id": exp["strategy_id"], "direction": t["direction"],
                             "signal_time": datetime.fromtimestamp(t["signal_time"], tz=timezone.utc),
                             "entry_time": datetime.fromtimestamp(t["entry_time"], tz=timezone.utc),
                             "exit_time": datetime.fromtimestamp(t["exit_time"], tz=timezone.utc),
                             "entry": t["entry"], "exit": t["exit"], "sl": t["sl"], "tp": t["tp"],
                             "r_multiple": t["r_multiple"], "mfe_r": t["mfe_r"], "mae_r": t["mae_r"],
                             "giveback_r": t["giveback_r"], "bars_held": t["bars_held"],
                             "exit_reason": t["exit_reason"], "session": t["session"], "regime": t["regime"],
                             "volatility": t["volatility"], "spread_points": t["spread_points"],
                             "cost_r": t["cost_r"], "pnl_money": t["pnl_money"], "volume": t["volume"],
                             "features": t.get("features")})
            for i in range(0, len(rows), 500):
                repo.insert_many("backtest_trades", rows[i:i + 500])
    except Exception as e:
        log.warning("could not store experiment in database: %s", e)


def _exp_row(kind, scfg, hyp, title, params, exit_model, ff, settings, metrics, evidence, path, notes="") -> dict:
    return {"id": new_id(), "created_at": utcnow(), "kind": kind, "strategy_id": scfg.id, "hypothesis_id": hyp,
            "title": title, "params": params, "exit_model": exit_model,
            "data_range": {"start": int(ff.t[0]), "end": int(ff.t[-1]), "bars_m15": len(ff)},
            "settings": settings, "metrics": metrics, "evidence_level": str(evidence) if evidence else None,
            "report_path": str(path) if path else None, "notes": notes}


# ----------------------------------------------------------------- experiments
def experiment_backtest(cfgm: ConfigManager, sid: str, m15: FeatureFrame, h1: FeatureFrame, settings: BTSettings,
                        spec: Optional[SymbolSpec], repo: Optional[Repository] = None,
                        calendar: Optional[EventCalendar] = None, exit_overrides: Optional[dict] = None,
                        m1: Optional[FeatureFrame] = None) -> dict:
    scfg = cfgm.strategy(sid)
    regimes = regime_series(h1)
    base = run_backtest(scfg, m15, h1, settings, spec, cfgm.app.sessions, calendar, exit_overrides=exit_overrides,
                        regimes=regimes, m1=m1)
    stress_settings = BTSettings(**{**settings.__dict__, "spread_mult": settings.spread_mult * 1.5,
                                    "slippage_points": settings.slippage_points + 5})
    stressed = run_backtest(scfg, m15, h1, stress_settings, spec, cfgm.app.sessions, calendar,
                            exit_overrides=exit_overrides, regimes=regimes, m1=m1)
    rs = [t["r_multiple"] for t in base.trades]
    psr = probabilistic_sharpe(rs)
    mc = monte_carlo(rs, risk_fraction=settings.risk_fraction)
    evidence = classify_evidence(base.metrics, stressed.metrics)
    exp_id = new_id()
    sections = [
        ("Verdict", f"**Evidence level: {evidence}**  (in-sample only - run `walkforward` before any promotion)\n\n"
                    f"Probabilistic Sharpe (P[true edge > 0]): {psr if psr is None else round(psr, 3)}"),
        ("Core metrics (net of modelled spread, slippage, commission, swap)", md_kv(base.metrics)),
        ("Cost stress: spread x1.5, slippage +5 points", md_kv(stressed.metrics)),
        ("Capital feasibility at start equity", md_kv(base.feasibility)),
        ("By session", md_table(base.by_session)), ("By regime", md_table(base.by_regime)),
        ("By direction", md_table(base.by_direction)), ("By exit reason", md_table(base.by_exit)),
        ("By month", md_table(base.by_month)), ("Signals skipped by filters", md_kv(base.skipped or {"none": 0})),
        ("Monte Carlo (bootstrap of trade sequence)", md_kv(mc)),
    ]
    path = save_report("BACKTEST", sid, f"Backtest - {sid} {scfg.version}", sections,
                       {"metrics": base.metrics, "stressed": stressed.metrics, "feasibility": base.feasibility,
                        "by_session": base.by_session, "by_regime": base.by_regime, "monte_carlo": mc,
                        "evidence": str(evidence), "psr": psr, "settings": settings.__dict__}, base.trades, exp_id)
    row = _exp_row("BACKTEST", scfg, None, f"Backtest {sid}", scfg.param_values(), exit_overrides or scfg.exit_model,
                   m15, settings.__dict__, {**base.metrics, "stressed_expectancy_r": stressed.metrics.get("expectancy_r"),
                                            "psr": psr, "feasibility": base.feasibility, "monte_carlo": mc},
                   evidence, path)
    row["id"] = exp_id
    persist(repo, row, base.trades)
    return {"experiment_id": exp_id, "report": str(path), "evidence": str(evidence), "metrics": base.metrics,
            "stressed": stressed.metrics, "feasibility": base.feasibility}


def experiment_cost_stress(cfgm, sid, m15, h1, settings, spec, repo=None) -> dict:
    scfg = cfgm.strategy(sid)
    regimes = regime_series(h1)
    grid = {}
    for sm in (1.0, 1.5, 2.0):
        for extra_slip in (0, 5, 10):
            s = BTSettings(**{**settings.__dict__, "spread_mult": sm,
                              "slippage_points": settings.slippage_points + extra_slip})
            r = run_backtest(scfg, m15, h1, s, spec, cfgm.app.sessions, regimes=regimes)
            grid[f"spread x{sm}, slip +{extra_slip}"] = {k: r.metrics.get(k) for k in
                                                          ("n_trades", "expectancy_r", "profit_factor", "win_rate",
                                                           "max_drawdown_r")}
    exp_id = new_id()
    breakeven = [k for k, v in grid.items() if (v.get("expectancy_r") or 0) <= 0]
    verdict = ("Edge does NOT survive realistic costs" if "spread x1.0, slip +0" in breakeven else
               (f"Edge disappears under: {', '.join(breakeven)}" if breakeven else "Edge survives every tested cost level"))
    path = save_report("COST_STRESS", sid, f"Cost stress - {sid}", [("Verdict", verdict), ("Grid", md_table(grid))],
                       {"grid": grid, "verdict": verdict}, None, exp_id)
    row = _exp_row("COST_STRESS", scfg, None, "Cost stress", scfg.param_values(), scfg.exit_model, m15,
                   settings.__dict__, {"grid": grid, "verdict": verdict}, None, path)
    row["id"] = exp_id
    persist(repo, row, None)
    return {"experiment_id": exp_id, "report": str(path), "verdict": verdict, "grid": grid}


def experiment_exits(cfgm, sid, m15, h1, settings, spec, repo=None) -> dict:
    scfg = cfgm.strategy(sid)
    regimes = regime_series(h1)
    table, runs = {}, {}
    for name, model in EXIT_VARIANTS.items():
        r = run_backtest(scfg, m15, h1, settings, spec, cfgm.app.sessions, exit_overrides=model, regimes=regimes)
        runs[name] = r
        table[name] = {k: r.metrics.get(k) for k in ("n_trades", "expectancy_r", "median_r", "win_rate",
                                                     "profit_factor", "mfe_captured_pct", "avg_mae_r",
                                                     "avg_holding_bars", "avg_giveback_r_winners", "max_giveback_r",
                                                     "tail_win_p95_r", "tail_loss_p05_r", "max_drawdown_r",
                                                     "sortino_per_trade")}
    base = runs["A_fixed_2R"]

    def wins(run):
        return [t["r_multiple"] for t in run.trades if t["r_multiple"] > 0]
    h6 = welch_p_value([t["r_multiple"] for t in runs["F_fixed_2R_time_stop"].trades], [t["r_multiple"] for t in base.trades])
    h7 = welch_p_value(wins(runs["B_structure_trail"]), wins(base))
    h8 = welch_p_value([t["r_multiple"] for t in runs["E_partial_plus_trail"].trades], [t["r_multiple"] for t in base.trades])
    hyp = {"H6 time stop vs baseline (p-value, mean R)": {"p": h6, "variant": table["F_fixed_2R_time_stop"]["expectancy_r"],
                                                          "baseline": table["A_fixed_2R"]["expectancy_r"]},
           "H7 structure trail avg winner (p-value)": {"p": h7,
                                                       "variant_avg_win": statistics.fmean(wins(runs["B_structure_trail"])) if wins(runs["B_structure_trail"]) else None,
                                                       "baseline_avg_win": statistics.fmean(wins(base)) if wins(base) else None},
           "H8 partial+trail vs baseline (p-value, sortino)": {"p": h8,
                                                               "variant": table["E_partial_plus_trail"]["sortino_per_trade"],
                                                               "baseline": table["A_fixed_2R"]["sortino_per_trade"]}}
    exp_id = new_id()
    path = save_report("EXIT_RESEARCH", sid, f"Exit research - {sid}",
                       [("Exit model comparison (same entries)", md_table(table)),
                        ("Hypotheses H6 / H7 / H8", md_table(hyp)),
                        ("Reading this", "MFE captured % = how much of the best open profit each exit kept. "
                                         "A p-value above 0.05 means the difference could easily be luck.")],
                       {"table": table, "hypotheses": hyp}, None, exp_id)
    row = _exp_row("EXIT_RESEARCH", scfg, "H6,H7,H8", "Exit research", scfg.param_values(), None, m15,
                   settings.__dict__, {"table": table, "hypotheses": hyp}, None, path)
    row["id"] = exp_id
    persist(repo, row, None)
    return {"experiment_id": exp_id, "report": str(path), "table": table}


def experiment_sessions(cfgm, sid, m15, h1, settings, spec, repo=None, calendar=None) -> dict:
    scfg = cfgm.strategy(sid)
    s = BTSettings(**{**settings.__dict__, "apply_session_filter": False})
    r = run_backtest(scfg, m15, h1, s, spec, cfgm.app.sessions, calendar)
    tr = r.trades
    by = r.by_session

    def rs(f):
        return [t["r_multiple"] for t in tr if f(t)]
    overlap, asia = rs(lambda t: t["session"] == "LONDON_NY_OVERLAP"), rs(lambda t: t["session"] == "ASIA")
    depths = sorted(t["features"].get("depth", 0) for t in tr if t.get("features"))
    med = depths[len(depths) // 2] if depths else 0
    shallow = rs(lambda t: (t.get("features") or {}).get("depth", 1) <= med)
    deep = rs(lambda t: (t.get("features") or {}).get("depth", 0) > med)
    q = sorted(t.get("quality", 0) for t in tr)
    qmed = q[len(q) // 2] if q else 0
    hi_q, lo_q = rs(lambda t: t.get("quality", 0) > qmed), rs(lambda t: t.get("quality", 0) <= qmed)
    by_vol = {}
    for t in tr:
        by_vol.setdefault(t["volatility"], []).append(t["r_multiple"])
    by_sess_event = {}
    for t in tr:
        by_sess_event.setdefault(f"{t['session']}|{t['event_state']}", []).append(t["r_multiple"])

    def summ(x):
        return {"n": len(x), "expectancy_r": round(statistics.fmean(x), 4) if x else None}
    hyp = {
        "H1 overlap vs Asia": {**{"overlap_" + k: v for k, v in summ(overlap).items()},
                               **{"asia_" + k: v for k, v in summ(asia).items()}, "p": welch_p_value(overlap, asia)},
        "H2 shallow vs deep": {**{"shallow_" + k: v for k, v in summ(shallow).items()},
                               **{"deep_" + k: v for k, v in summ(deep).items()}, "p": welch_p_value(shallow, deep),
                               "median_depth": med},
        "H3 high vs low quality": {**{"high_" + k: v for k, v in summ(hi_q).items()},
                                   **{"low_" + k: v for k, v in summ(lo_q).items()}, "p": welch_p_value(hi_q, lo_q)},
    }
    h4 = {k: summ(v) for k, v in by_vol.items()}
    h5 = {k: summ(v) for k, v in by_sess_event.items()}
    exp_id = new_id()
    path = save_report("SESSION_RESEARCH", sid, f"Session / structure research - {sid}",
                       [("Expectancy by session (no session filter applied)", md_table(by)),
                        ("Hypotheses H1-H3", md_table(hyp)), ("H4 by volatility regime", md_table(h4)),
                        ("H5 session x event state", md_table(h5)),
                        ("Reading this", "Only restrict sessions in strategies.json (allowed_sessions) when the "
                                         "difference is large, stable across years and survives walk-forward.")],
                       {"by_session": by, "hypotheses": hyp, "h4": h4, "h5": h5}, None, exp_id)
    row = _exp_row("SESSION_RESEARCH", scfg, "H1,H2,H3,H4,H5", "Session research", scfg.param_values(), None, m15,
                   s.__dict__, {"by_session": by, "hypotheses": hyp}, None, path)
    row["id"] = exp_id
    persist(repo, row, None)
    return {"experiment_id": exp_id, "report": str(path), "by_session": by, "hypotheses": hyp}


def experiment_walkforward(cfgm, sid, m15, h1, settings, spec, repo=None, train_months=12, test_months=3,
                           workers=1) -> dict:
    scfg = cfgm.strategy(sid)
    w = walk_forward(scfg, m15, h1, settings, spec, train_months, test_months, workers=workers)
    rs = [t["r_multiple"] for t in w.oos_trades]
    stressed = run_backtest(scfg, m15, h1, BTSettings(**{**settings.__dict__, "spread_mult": settings.spread_mult * 1.5,
                                                          "slippage_points": settings.slippage_points + 5}), spec,
                            cfgm.app.sessions)
    full = run_backtest(scfg, m15, h1, settings, spec, cfgm.app.sessions)
    evidence = classify_evidence(full.metrics, stressed.metrics, w.oos_metrics, w.stability.get("ok"), w.dsr)
    mc = monte_carlo(rs, risk_fraction=settings.risk_fraction)
    win_rows = {str(i): {"train_from": datetime.fromtimestamp(x["train"][0], tz=timezone.utc).date().isoformat(),
                         "test_from": datetime.fromtimestamp(x["test"][0], tz=timezone.utc).date().isoformat(),
                         "chosen": x.get("chosen"), "train_exp": x.get("train_expectancy_r"),
                         "test_trades": x.get("test_trades"), "test_exp": x.get("test_expectancy_r"),
                         "neighbours_positive": x.get("neighbour_positive_fraction")} for i, x in enumerate(w.windows)}
    exp_id = new_id()
    stats = {"n_trades": full.metrics.get("n_trades"), "expectancy_r": full.metrics.get("expectancy_r"),
             "win_rate": full.metrics.get("win_rate"), "avg_win_r": full.metrics.get("avg_win_r"),
             "avg_loss_r": full.metrics.get("avg_loss_r"), "oos_expectancy_r": w.oos_metrics.get("expectancy_r"),
             "oos_n_trades": w.oos_metrics.get("n_trades"), "dsr": w.dsr, "evidence_level": str(evidence),
             "experiment_id": exp_id}
    sections = [("Verdict", f"**Evidence level: {evidence}**\n\nOut-of-sample expectancy: "
                            f"{w.oos_metrics.get('expectancy_r')} R over {w.oos_metrics.get('n_trades', 0)} trades. "
                            f"Deflated Sharpe: {None if w.dsr is None else round(w.dsr, 3)} "
                            f"({w.combos_tested} parameter combinations tried). Parameter stability: {w.stability}"),
                ("Out-of-sample metrics (stitched test windows)", md_kv(w.oos_metrics)),
                ("Windows", md_table(win_rows)), ("All combinations (full sample)", md_table(w.per_combo)),
                ("Monte Carlo on OOS trades", md_kv(mc)),
                ("Promotion package", "If you accept this evidence run:\n\n"
                                      f"`python -m app.main promote --strategy {sid} --experiment {exp_id} "
                                      f"--to MICRO_LIVE --reason \"<why>\"`\n\n```json\n{json.dumps(to_jsonable(stats), indent=2)}\n```")]
    path = save_report("WALK_FORWARD", sid, f"Walk-forward - {sid}", sections,
                       {"oos": w.oos_metrics, "windows": w.windows, "dsr": w.dsr, "stability": w.stability,
                        "per_combo": w.per_combo, "monte_carlo": mc, "evidence": str(evidence),
                        "promotion_stats": stats}, w.oos_trades, exp_id)
    row = _exp_row("WALK_FORWARD", scfg, None, "Walk-forward", scfg.research_grid, scfg.exit_model, m15,
                   settings.__dict__, {"oos": w.oos_metrics, "dsr": w.dsr, "stability": w.stability,
                                       "promotion_stats": stats, "monte_carlo": mc}, evidence, path)
    row["id"] = exp_id
    persist(repo, row, w.oos_trades)
    return {"experiment_id": exp_id, "report": str(path), "evidence": str(evidence), "oos": w.oos_metrics,
            "dsr": w.dsr, "stability": w.stability, "promotion_stats": stats}
