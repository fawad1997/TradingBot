"""Shared test helpers: a simulated core and hand-made signals for deterministic tests."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.types import Side, Signal, TradePlan, new_id, utcnow  # noqa: E402
from app.regime.router import RouteResult  # noqa: E402
from app.core.types import Eligibility  # noqa: E402
from app.simulation import make_sim_core, run_sim  # noqa: E402

FIXED_EXIT = {"type": "fixed", "target_r": 2.0, "partial": {"enabled": False}, "time_stop": {"enabled": False},
              "thesis_invalidation": False}


def started_core(env="DEMO", balance=10000.0, **kw):
    core, sim, broker, cfg_dir = make_sim_core(env, n_bars=9000, warmup_bars=7000, balance=balance, **kw)
    core.start()
    core.step()          # process the first live tick (the core never trades on stale data)
    return core, sim, broker, cfg_dir


def make_signal(core, side: Side = Side.BUY, stop_atr: float = 1.5, exit_model=None) -> Signal:
    tick = core.last_tick or core.data.tick(core.symbol)
    atr = float(core.ff["M15"].atr(14)[core.ff["M15"].last])
    entry = tick.ask if side is Side.BUY else tick.bid
    sl = entry - side.sign * stop_atr * atr
    tp = entry + side.sign * 2 * stop_atr * atr
    plan = TradePlan(direction=side, entry=entry, sl=sl, tp=tp, exit_model=exit_model or dict(FIXED_EXIT),
                     setup_key=f"test:{new_id()}", quality=0.5, ttl_seconds=600)
    now_b = core.server_now()
    return Signal(id=new_id(), strategy_id="pullback_v1", strategy_version="1.0.0", symbol=core.symbol,
                  created_at=utcnow(), created_broker_time=now_b, valid_until_broker=now_b + 600,
                  snapshot_id=new_id(), plan=plan, regime="TREND_UP", session="LONDON", event_state="NORMAL",
                  meta={"bar_time": int(core.ff["M15"].t[core.ff["M15"].last]), "vol": "NORMAL"})


def eligible_route(sid="pullback_v1"):
    return RouteResult(sid, Eligibility.ELIGIBLE, "ok", True, False)


def submit(core, side=Side.BUY, **kw):
    """Push a hand-made signal through the full gate pipeline."""
    sig = make_signal(core, side, **kw)
    core._process_signal(sig, eligible_route(), core.cfgm.strategy("pullback_v1"), core.last_tick)
    return sig


def trades(core):
    return core.repo.tables["trades"]


def rows(core, table, **where):
    return [r for r in core.repo.tables[table] if all(r.get(k) == v for k, v in where.items())]
