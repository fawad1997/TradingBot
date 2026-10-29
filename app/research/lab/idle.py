"""Deeper research while the markets are shut.

The research lab wakes every few hours and spends a fixed, deliberately small budget, because during
trading hours it competes with the thing that has a deadline. Over a weekend there is nothing to
compete with: the FX week closes Friday around 21:00 UTC and does not reopen until Sunday around
22:00 UTC, which is roughly 49 hours of a machine sitting idle with four years of history on disk.

That window is where the expensive work belongs - the searches that are too slow to run beside live
trading, and the audits nobody remembers to run by hand.

WHY THIS IS SAFE

    The lab already runs in its own low-priority process (spawn, daemon) and shares nothing with the
    trading process except the database. Everything here is research and measurement: it fits models,
    writes reports and records verdicts. It does not place orders, change configuration, promote a
    strategy or touch a risk limit. A research process that could retune production while reporting
    on it would make every number it produced untrustworthy.

WHAT "CLOSED" MEANS HERE

    Judged the same way the trading core judges it: by whether the broker is still quoting. A market
    that is shut stops quoting, and that is ground truth, whereas a calendar is a guess that is wrong
    on every holiday. The weekend estimate is used only when no quote is available at all.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

# Friday close and Sunday open for FX/metals, UTC. Used only when no broker quote is available.
FX_CLOSE_WEEKDAY, FX_CLOSE_HOUR = 4, 21          # Friday 21:00
FX_OPEN_WEEKDAY, FX_OPEN_HOUR = 6, 22            # Sunday 22:00

MIN_IDLE_MINUTES_FOR_DEEP_WORK = 90


def is_weekend_closed(now: Optional[datetime] = None) -> bool:
    """Calendar estimate of the FX weekend. A fallback, not the primary signal."""
    now = now or datetime.now(timezone.utc)
    wd, hour = now.weekday(), now.hour
    if wd == FX_CLOSE_WEEKDAY and hour >= FX_CLOSE_HOUR:
        return True
    if wd == 5:                                   # all Saturday
        return True
    if wd == FX_OPEN_WEEKDAY and hour < FX_OPEN_HOUR:
        return True
    return False


def minutes_until_open(now: Optional[datetime] = None) -> float:
    """Roughly how long the weekend has left, in minutes. 0.0 when the market should be open."""
    now = now or datetime.now(timezone.utc)
    if not is_weekend_closed(now):
        return 0.0
    days_ahead = (FX_OPEN_WEEKDAY - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(hour=FX_OPEN_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=7)
    return max(0.0, (target - now).total_seconds() / 60.0)


def market_is_idle(repo=None, now: Optional[datetime] = None, quote_age_limit_sec: float = 900.0) -> Dict[str, Any]:
    """Are the markets shut right now, and for how much longer?

    Prefers the broker's own quote age, recorded by the trading process in `system_health`, over the
    calendar. Falls back to the weekend estimate when no recent health row is available - which is
    also the case when the trading process is not running at all, and a stopped bot is itself an idle
    window worth using.
    """
    now = now or datetime.now(timezone.utc)
    estimate = {"idle": is_weekend_closed(now), "basis": "weekend estimate",
                "minutes_until_open": minutes_until_open(now)}
    if repo is None:
        return estimate
    try:
        rows = repo.select("system_health", {"component": "DATA"}, order_by="created_at", desc=True, limit=5) or []
    except Exception:
        return estimate
    if not rows:
        return estimate
    newest = rows[0]
    age = None
    details = newest.get("details")
    if isinstance(details, dict):
        age = details.get("quote_age_sec")
    try:
        age = float(age) if age is not None else None
    except (TypeError, ValueError):
        age = None
    if age is None:
        return estimate
    idle = age > quote_age_limit_sec
    return {"idle": idle,
            "basis": f"broker quote age {age:.0f}s",
            "minutes_until_open": minutes_until_open(now) if idle else 0.0,
            "quote_age_sec": age}


# --------------------------------------------------------------------------------------- the work
def deep_research_plan(minutes_available: float) -> List[str]:
    """Which jobs fit in the window, cheapest and most decisive first.

    Ordered so that a window cut short still produces the most valuable results. The audits come
    before the expensive searches because they answer questions about work already done, and a
    negative answer there changes what the searches are worth.
    """
    plan = ["data_quality", "cost_model_audit", "strategy_risk", "parameter_audit"]
    if minutes_available >= 120:
        # Research for the seven strategies that have none. Placed before the ml retrain because a
        # strategy with no evidence at all is a bigger gap than a filter that is already record-only,
        # and before the searches because it needs no searching - it replays live settings once.
        plan.append("all_strategy_research")
    if minutes_available >= 180:
        plan.append("ml_retrain")
    if minutes_available >= 360:
        plan.append("extended_walk_forward")
    if minutes_available >= 600:
        plan.append("full_symbol_sweep")
    return plan


def run_deep_research(cfgm, repo, adapter=None, now: Optional[datetime] = None,
                      jobs: Optional[Dict[str, Callable]] = None) -> Dict[str, Any]:
    """Run whatever fits in the idle window. Never raises; a failed job is recorded and skipped.

    `jobs` lets tests inject fakes. In production the defaults below are used.
    """
    state = market_is_idle(repo, now)
    if not state.get("idle"):
        return {"ran": False, "reason": "markets are open", **state}
    minutes = float(state.get("minutes_until_open") or 0.0)
    if minutes < MIN_IDLE_MINUTES_FOR_DEEP_WORK:
        return {"ran": False, "reason": f"only {minutes:.0f} min left before open", **state}

    plan = deep_research_plan(minutes)
    jobs = jobs or _default_jobs()
    results: Dict[str, Any] = {}
    for name in plan:
        fn = jobs.get(name)
        if fn is None:
            results[name] = {"skipped": "not available"}
            continue
        try:
            results[name] = fn(cfgm, repo, adapter)
        except Exception as exc:
            log.exception("idle research job %s failed", name)
            results[name] = {"error": str(exc)}
    out = {"ran": True, "minutes_available": round(minutes, 1), "plan": plan,
           "results": results, "at": (now or datetime.now(timezone.utc)).isoformat(), **state}
    _save(repo, out)
    return out


def _save(repo, payload: Dict[str, Any]) -> None:
    if repo is None:
        return
    # The table's own columns. This wrote "kind" and "results", which research_lab_runs does not have, and no
    # symbol (NOT NULL), so every idle run was refused - and the refusal was logged at DEBUG, so nobody saw it.
    try:
        repo.insert("research_lab_runs", {
            "created_at": datetime.now(timezone.utc),
            "symbol": "*",                        # an idle run covers every symbol
            "verdict": "IDLE_DEEP_RESEARCH",
            "best": payload,
            "note": "idle deep research: " + ", ".join(sorted((payload.get("results") or {}).keys())),
        })
    except Exception:
        log.warning("could not record idle research run", exc_info=True)


# --------------------------------------------------------------------------------------- jobs
def _job_data_quality(cfgm, repo, adapter) -> Dict[str, Any]:
    """Bad prints inflate mean-reversion results specifically, so measure before trusting any of them."""
    return {"note": "reported per symbol by the main cycle as data_quality"}


def _job_cost_model_audit(cfgm, repo, adapter) -> Dict[str, Any]:
    """Kissell ch.3. The one audit that can justify LOOSENING a gate on evidence."""
    from tools.cost_model_audit import attach_empirical_timing_risk, build_rows
    from app.execution.cost_audit import audit_cost_model
    if repo is None:
        return {"skipped": "no database"}
    rows = build_rows(repo.select("signals") or [], repo.select("trades") or [])
    return audit_cost_model(attach_empirical_timing_risk(rows))


def _job_strategy_risk(cfgm, repo, adapter) -> Dict[str, Any]:
    """Lopez de Prado ch.15. Implied Sharpe and how fragile it is to a slip in precision."""
    from tools.strategy_risk import assess
    if repo is None:
        return {"skipped": "no database"}
    trades = [t for t in (repo.select("trades") or []) if t.get("closed_at")]
    return assess(trades)


def _job_parameter_audit(cfgm, repo, adapter) -> Dict[str, Any]:
    """Chan ch.3. Degrees of freedom against the data available to support them."""
    from tools.parameter_audit import audit
    st = [{"id": s.id, "status": s.status, "params": dict(s.params or {}),
           "exit_model": dict(s.exit_model or {})} for s in cfgm.strategies.strategies]
    return audit(st, 0)


def _job_ml_retrain(cfgm, repo, adapter) -> Dict[str, Any]:
    """Refit the learned filter with uniqueness weights and purged splits. Records; never promotes."""
    from app.ml.filter import train_all
    if repo is None:
        return {"skipped": "no database"}
    return train_all(cfgm, repo)


def _job_extended_walk_forward(cfgm, repo, adapter) -> Dict[str, Any]:
    return {"note": "reserved for a longer Optuna budget than the trading-hours cycle allows"}


def _job_full_symbol_sweep(cfgm, repo, adapter) -> Dict[str, Any]:
    return {"note": "reserved for sweeping every symbol rather than the configured subset"}


def _job_all_strategy_research(cfgm, repo, adapter) -> Dict[str, Any]:
    """Research every configured strategy, not just micro_scalper.

    Until this existed, seven of eight strategies traded with no research behind them. This runs the full
    lab cycle with per-strategy research switched on, which replays each strategy at its LIVE parameters
    through the generic backtester. It measures and records; it promotes nothing and blocks nothing on its
    own. A strategy only becomes blockable when the operator adds it to research_lab.gate_strategies."""
    if adapter is None:
        return {"skipped": "no broker adapter - per-strategy research needs price history"}
    from app.research.lab.worker import run_cycle
    res = run_cycle(cfgm, repo, adapter, all_strategies=True)
    counts = res.get("strategy_verdict_counts") or {}
    return {"strategies_researched": len(counts), "verdict_counts": counts,
            "symbols": len(res.get("symbols") or {}),
            "note": "recorded only; gate_strategies decides what may block"}


def _default_jobs() -> Dict[str, Callable]:
    return {
        "data_quality": _job_data_quality,
        "cost_model_audit": _job_cost_model_audit,
        "strategy_risk": _job_strategy_risk,
        "parameter_audit": _job_parameter_audit,
        "all_strategy_research": _job_all_strategy_research,
        "ml_retrain": _job_ml_retrain,
        "extended_walk_forward": _job_extended_walk_forward,
        "full_symbol_sweep": _job_full_symbol_sweep,
    }
