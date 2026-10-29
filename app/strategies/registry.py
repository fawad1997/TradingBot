"""Builds strategy objects from config/strategies.json."""
from __future__ import annotations

from typing import Dict, List, Optional

from app.config import StrategyCfg
from app.strategies.base import Strategy
from app.strategies.candidates import CANDIDATE_CLASSES
from app.strategies.mean_reversion_v1 import MeanReversionV1
from app.strategies.micro_scalper import MicroScalper
from app.strategies.pullback_v1 import PullbackV1
from app.strategies.trend_daily import TrendDaily
from app.strategies.trend_ensemble import TrendEnsemble
from app.strategies.turn_of_month import TurnOfMonth

CLASSES = {"PullbackV1": PullbackV1, "MeanReversionV1": MeanReversionV1, "MicroScalper": MicroScalper,
           "TrendDaily": TrendDaily, "TrendEnsemble": TrendEnsemble, "TurnOfMonth": TurnOfMonth,
           **CANDIDATE_CLASSES}


def build(cfg: StrategyCfg, param_overrides: Optional[dict] = None, exit_overrides: Optional[dict] = None) -> Strategy:
    cls = CLASSES.get(cfg.class_name)
    if cls is None:
        raise ValueError(f"Unknown strategy class {cfg.class_name}")
    params = cfg.param_values()
    if param_overrides:
        params.update(param_overrides)
    exit_model = dict(cfg.exit_model)
    if exit_overrides:
        exit_model = _deep_merge(exit_model, exit_overrides)
    return cls(cfg.id, cfg.version, params, exit_model)


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def build_all(cfgs: List[StrategyCfg]) -> Dict[str, Strategy]:
    return {c.id: build(c) for c in cfgs}


def uninstalled_classes(cfgs: List[StrategyCfg]) -> List[str]:
    """Strategy classes that EXIST in code but have no entry in config/strategies.json.

    A strategy only runs if it has a config entry, so a class shipped in a new package reaches a user who keeps
    their own `config/` as dead code: no diagnostics row, no scoreboard row, nothing in the logs. That is
    indistinguishable from a strategy that is broken, and it hid five working strategies for a week. Startup says
    so out loud instead.
    """
    configured = {c.class_name for c in cfgs}
    return sorted(name for name in CLASSES if name not in configured)
