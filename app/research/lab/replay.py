"""The replay report: every strategy over all the history the broker gives, as "how good, how bad".

The lab's verdicts answer one narrow question (did the unseen last 30% hold up?). This answers the one a person
asks first: over the whole replay, what would each strategy have done - total R, the worst run of losses, the
worst month, the account curve - with the unseen stretch and its evidence grade kept separate so the in-sample
part is never passed off as proof.

Everything is in R (multiples of the risk taken per trade) because that is what compares across symbols. A
money figure at the live risk per trade is shown for illustration only: trades on different symbols overlap,
and the live risk limits would have skipped some of them.

Inputs are the backtester's own trades (`research_one(..., keep_trades=True)`) and the lab's trend replay, so
the numbers are the same ones the research verdicts are built from.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from app.research.lab.stats import mean_ci

ROOT = Path(__file__).resolve().parents[3] / "research" / "results" / "backtest"
CURVE_POINTS = 240


def compact(trades: Iterable[dict], symbol: str) -> List[dict]:
    """The few fields the report needs, from the backtester's or the trend replay's trade dicts."""
    out = []
    for t in trades:
        r = t.get("r_multiple")
        if r is None or not math.isfinite(float(r)):
            continue
        side = str(t.get("direction") or t.get("side") or "")
        out.append({"sym": symbol, "t": int(t.get("entry_time") or 0), "x": int(t.get("exit_time") or 0),
                    "s": "BUY" if "BUY" in side else "SELL", "r": round(float(r), 4),
                    "why": str(t.get("exit_reason") or "")})
    return out


def _month(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def _quarter(ts: int) -> str:
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def _r(x: float, n: int = 3) -> float:
    return round(float(x), n)


def summarise(trades: Sequence[dict], split_time: Optional[int] = None, risk_money: Optional[float] = None) -> dict:
    """How good and how bad, for one list of compact trades (one symbol, or several pooled).

    Trades are ordered by exit time, the moment their R is banked, so the curve and drawdown read as an account
    would have experienced them (overlapping trades on several symbols included)."""
    tr = sorted(trades, key=lambda t: (t["x"] or t["t"], t["t"]))
    rs = [t["r"] for t in tr]
    n = len(rs)
    if not n:
        return {"n": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    eq = peak = dd = 0.0
    streak = worst_streak = 0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if r <= 0 else 0
        worst_streak = max(worst_streak, streak)
    months: Dict[str, float] = defaultdict(float)
    quarters: Dict[str, float] = defaultdict(float)
    for t in tr:
        months[_month(t["x"] or t["t"])] += t["r"]
        quarters[_quarter(t["x"] or t["t"])] += t["r"]
    gross_loss = -sum(losses)
    ci = mean_ci(rs)
    out = {
        "n": n, "first": int(tr[0]["t"]), "last": int(tr[-1]["x"] or tr[-1]["t"]),
        "win_rate": _r(len(wins) / n, 4), "avg_r": ci["mean"], "lo": ci["lo"], "hi": ci["hi"],
        "total_r": _r(sum(rs), 2), "profit_factor": _r(sum(wins) / gross_loss, 3) if gross_loss > 0 else None,
        "avg_win_r": _r(sum(wins) / len(wins)) if wins else None,
        "avg_loss_r": _r(sum(losses) / len(losses)) if losses else None,
        "best_trade_r": _r(max(rs)), "worst_trade_r": _r(min(rs)),
        "max_drawdown_r": _r(dd, 2), "worst_losing_streak": worst_streak,
        "months": len(months), "losing_months": sum(1 for v in months.values() if v < 0),
        "best_month": max(months.items(), key=lambda kv: kv[1]) if months else None,
        "worst_month": min(months.items(), key=lambda kv: kv[1]) if months else None,
        "by_quarter": {k: _r(v, 2) for k, v in sorted(quarters.items())},
        "curve": _curve(tr),
    }
    out["best_month"] = [out["best_month"][0], _r(out["best_month"][1], 2)] if out["best_month"] else None
    out["worst_month"] = [out["worst_month"][0], _r(out["worst_month"][1], 2)] if out["worst_month"] else None
    if risk_money:
        out["money"] = {"risk_per_trade": _r(risk_money, 2), "total": _r(sum(rs) * risk_money, 0),
                        "max_drawdown": _r(dd * risk_money, 0)}
    if split_time:
        unseen = [t["r"] for t in tr if t["t"] >= split_time]
        u = mean_ci(unseen)
        out["unseen"] = {"from": int(split_time), "n": len(unseen), "avg_r": u["mean"], "lo": u["lo"], "hi": u["hi"],
                         "total_r": _r(sum(unseen), 2)}
    return out


def _curve(tr: Sequence[dict]) -> List[list]:
    """[exit time, cumulative R] thinned to at most CURVE_POINTS points, always keeping the lowest and highest."""
    pts, eq = [], 0.0
    for t in tr:
        eq += t["r"]
        pts.append([int(t["x"] or t["t"]), round(eq, 2)])
    if len(pts) <= CURVE_POINTS:
        return pts
    step = len(pts) / CURVE_POINTS
    keep = {int(i * step) for i in range(CURVE_POINTS)} | {len(pts) - 1}
    keep |= {min(range(len(pts)), key=lambda i: pts[i][1]), max(range(len(pts)), key=lambda i: pts[i][1])}
    return [pts[i] for i in sorted(keep)]


def build(cells: Dict[str, Dict[str, dict]], risk_money: Optional[float] = None, source: str = "") -> dict:
    """cells = {strategy: {symbol: {"trades": compact list, "split_time", "history_from", "grade", "verdict",
    "note"}}}. Returns the report: per strategy a pooled summary and one per symbol, plus the trades."""
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "source": source,
              "risk_per_trade_money": risk_money, "strategies": {}, "trades": {}}
    for sid, per in cells.items():
        pooled, symbols = [], {}
        splits = []
        for sym, c in per.items():
            tr = c.get("trades") or []
            pooled.extend(tr)
            if c.get("split_time"):
                splits.append(int(c["split_time"]))
            s = summarise(tr, c.get("split_time"), risk_money)
            s.update({k: c.get(k) for k in ("history_from", "grade", "verdict", "note", "bars") if c.get(k) is not None})
            symbols[sym] = s
        # The pooled unseen stretch starts where each symbol's own does; the earliest is a fair label.
        p = summarise(pooled, None, risk_money)
        if splits:
            unseen = [t for sym, c in per.items() for t in (c.get("trades") or [])
                      if c.get("split_time") and t["t"] >= int(c["split_time"])]
            u = mean_ci([t["r"] for t in unseen])
            p["unseen"] = {"from": min(splits), "n": len(unseen), "avg_r": u["mean"], "lo": u["lo"], "hi": u["hi"],
                           "total_r": _r(sum(t["r"] for t in unseen), 2) if unseen else 0.0}
        p["symbols_traded"] = sum(1 for s in symbols.values() if s.get("n"))
        report["strategies"][sid] = {"pooled": p, "symbols": symbols}
        report["trades"][sid] = sorted(pooled, key=lambda t: t["t"])
    return report


def save(report: dict, root: Optional[Path] = None) -> Path:
    root = Path(root or ROOT)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    body = json.dumps(report, separators=(",", ":"), default=str)
    path = root / f"{stamp}.json"
    path.write_text(body, encoding="utf-8")
    (root / "latest.json").write_text(body, encoding="utf-8")
    return path


def load(root: Optional[Path] = None) -> Optional[dict]:
    p = Path(root or ROOT) / "latest.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def without_trades(report: dict) -> dict:
    """The report as the dashboard's overview needs it: summaries only, trades fetched per strategy."""
    out = {k: v for k, v in report.items() if k != "trades"}
    out["trade_counts"] = {sid: len(v) for sid, v in (report.get("trades") or {}).items()}
    return out


def cells_from_lab(per_symbol_strategies: Dict[str, Dict[str, dict]], trend_by_symbol: Dict[str, dict],
                   history: Dict[str, dict]) -> Dict[str, Dict[str, dict]]:
    """Report cells from what a lab cycle holds: `research_one` entries (with keep_trades) per symbol, the trend
    replay per symbol and each symbol's history dates. Pops the trades off the lab entries so the lab's own
    results file stays small."""
    cells: Dict[str, Dict[str, dict]] = defaultdict(dict)
    for sym, per in (per_symbol_strategies or {}).items():
        frm = ((history.get(sym) or {}).get("M15") or {}).get("from")
        for sid, e in (per or {}).items():
            if not isinstance(e, dict):
                continue
            tr = [dict(t, sym=sym) for t in (e.pop("trades", None) or [])]
            cells[sid][sym] = {"trades": tr, "split_time": e.get("split_time"), "history_from": frm,
                               "bars": e.get("bars"), "verdict": e.get("verdict"), "note": e.get("note"),
                               "grade": (e.get("stats") or {}).get("grade") or e.get("evidence_grade")}
    for sym, tr in (trend_by_symbol or {}).items():
        frm = ((history.get(sym) or {}).get("H4") or {}).get("from")
        # Pre-registered rule, nothing tuned on any of this history: there is no in-sample stretch to hold out.
        cells["trend_daily_v1"][sym] = {"trades": compact(tr.get("trades") or [], sym), "history_from": frm,
                                        "verdict": tr.get("verdict"), "grade": (tr.get("stats") or {}).get("grade"),
                                        "note": "daily candles from all H4 history; pre-registered parameters"}
    return dict(cells)
