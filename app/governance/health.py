"""Two-speed governance (section 25).

FastHealth  - reacts within seconds: stale data, spread shock, abnormal ticks,
              broker disconnects, latency, reconciliation uncertainty, storage.
StrategyHealthMonitor - slow statistics: rolling vs long-term expectancy from
              closed trades, plus counterfactual opportunity cost.
"""
from __future__ import annotations

import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, List, Optional

from app.config import HealthCfg
from app.core.types import ComponentStatus, RiskState, StrategyHealth

COMPONENTS = ["MT5", "DATABASE", "DATA", "EXECUTION", "RECONCILIATION", "STRATEGY", "RISK"]


def strategy_health_key(strategy_id: str, version: str) -> str:
    return f"{strategy_id}@{version}"


@dataclass
class HealthReport:
    components: Dict[str, str]
    trading_allowed: bool
    blockers: List[str]
    forced_risk_state: Optional[RiskState]
    spread_shock: bool
    details: dict = field(default_factory=dict)


@dataclass
class BotStatusMonitor:
    """Tracks trading pause transitions and accumulated downtime."""

    wall: Callable[[], float] = time.time
    is_trading_allowed: bool = False
    halt_reason: str = "INITIALIZING"
    halt_timestamp: Optional[float] = None
    total_downtime_seconds: float = 0.0
    last_downtime_seconds: Optional[float] = None
    last_resumed_at: Optional[float] = None

    def __post_init__(self) -> None:
        if self.halt_timestamp is None:
            self.halt_timestamp = self.wall()

    def update(self, is_trading_allowed: bool, reason: str = "RUNNING") -> Optional[dict]:
        now = self.wall()
        if not is_trading_allowed:
            if self.halt_timestamp is None:
                self.halt_timestamp = now
            self.is_trading_allowed = False
            self.halt_reason = reason
            return None

        resumed = None
        if not self.is_trading_allowed and self.halt_timestamp is not None:
            duration = max(0.0, now - self.halt_timestamp)
            self.last_downtime_seconds = duration
            self.total_downtime_seconds += duration
            self.last_resumed_at = now
            resumed = {"duration_seconds": duration, "halt_reason": self.halt_reason,
                       "resumed_at": now}
        self.is_trading_allowed = True
        self.halt_reason = "RUNNING"
        self.halt_timestamp = None
        return resumed

    def trigger_halt(self, reason: str) -> None:
        self.update(False, reason)

    def status(self) -> dict:
        now = self.wall()
        elapsed = (max(0.0, now - self.halt_timestamp)
                   if not self.is_trading_allowed and self.halt_timestamp is not None else 0.0)
        return {
            "is_trading_allowed": self.is_trading_allowed,
            "halt_reason": self.halt_reason,
            "halt_timestamp": self.halt_timestamp,
            "downtime_seconds": elapsed,
            "total_downtime_seconds": self.total_downtime_seconds,
            "last_downtime_seconds": self.last_downtime_seconds,
            "last_resumed_at": self.last_resumed_at,
        }


class FastHealth:
    def __init__(self, cfg: HealthCfg, wall=time.time):
        self.cfg = cfg
        self.wall = wall
        self.last_tick_wall: Optional[float] = None
        self.last_tick_msc: Optional[int] = None
        self.broker_connected = False
        self.db_ok = True
        self.telemetry_ok = True
        self.recon_uncertain_tickets: set = set()
        self.portfolio_uncertain = False
        self.latencies: Deque[float] = deque(maxlen=50)
        self.exec_failures: Deque[float] = deque(maxlen=20)
        self.bad_ticks: Deque[float] = deque(maxlen=100)
        self.data_issues: Deque[float] = deque(maxlen=100)
        self.spread_shock = False
        self.market_closed = False
        self.weekend_open = False      # always-open symbol currently in its weekend session
        self.unexpected_positions: set = set()

    def on_tick(self, tick_msc: int, ok: bool) -> None:
        if ok:
            if self.last_tick_msc != tick_msc:
                self.last_tick_wall = self.wall()
                self.last_tick_msc = tick_msc
        else:
            self.bad_ticks.append(self.wall())

    def on_spread(self, spread_points: float, median_points: float) -> None:
        self.spread_shock = bool(median_points and median_points == median_points and
                                 spread_points > self.cfg.spread_shock_multiple * median_points)

    def on_latency(self, ms: float) -> None:
        self.latencies.append(ms)

    def on_exec_failure(self) -> None:
        self.exec_failures.append(self.wall())

    def on_data_issue(self) -> None:
        self.data_issues.append(self.wall())

    def _recent(self, dq: Deque[float], seconds: float) -> int:
        now = self.wall()
        return sum(1 for x in dq if now - x <= seconds)

    def report(self, now_utc: datetime) -> HealthReport:
        c: Dict[str, str] = {}
        blockers: List[str] = []
        forced: Optional[RiskState] = None
        # MT5
        c["MT5"] = str(ComponentStatus.HEALTHY if self.broker_connected else ComponentStatus.DOWN)
        if not self.broker_connected:
            blockers.append("BROKER_DISCONNECTED")
        # DATA
        age = (self.wall() - self.last_tick_wall) if self.last_tick_wall else None
        if self.market_closed:
            c["DATA"] = str(ComponentStatus.MARKET_CLOSED)
            blockers.append("MARKET_CLOSED")
        elif age is None or age > (self.cfg.weekend_stale_tick_sec if self.weekend_open else self.cfg.stale_tick_sec):
            c["DATA"] = str(ComponentStatus.DEGRADED)
            blockers.append("STALE_DATA")
        elif self._recent(self.bad_ticks, 60) >= 5 or self._recent(self.data_issues, 3600) >= 3:
            c["DATA"] = str(ComponentStatus.WATCH)
            blockers.append("ABNORMAL_TICKS")
        else:
            c["DATA"] = str(ComponentStatus.HEALTHY)
        if self.spread_shock:
            blockers.append("SPREAD_SHOCK")
        # DATABASE
        c["DATABASE"] = str(ComponentStatus.HEALTHY if self.db_ok else ComponentStatus.DOWN)
        if not self.db_ok:
            blockers.append("DATABASE_DOWN")
            forced = RiskState.NO_NEW_TRADES
        # EXECUTION
        p90 = None
        if self.latencies:
            s = sorted(self.latencies)
            p90 = s[int(0.9 * (len(s) - 1))]
        fails = self._recent(self.exec_failures, 3600)
        if fails >= self.cfg.exec_failure_cluster:
            c["EXECUTION"] = str(ComponentStatus.DEGRADED)
            blockers.append("EXECUTION_FAILURE_CLUSTER")
        elif p90 is not None and p90 > self.cfg.latency_warn_ms:
            c["EXECUTION"] = str(ComponentStatus.WATCH)
        else:
            c["EXECUTION"] = str(ComponentStatus.HEALTHY)
        # RECONCILIATION
        if self.portfolio_uncertain:
            c["RECONCILIATION"] = str(ComponentStatus.DOWN)
            blockers.append("PORTFOLIO_STATE_UNCERTAIN")
            forced = RiskState.NO_NEW_TRADES
        elif self.recon_uncertain_tickets or self.unexpected_positions:
            c["RECONCILIATION"] = str(ComponentStatus.WATCH)
        else:
            c["RECONCILIATION"] = str(ComponentStatus.HEALTHY)
        if self.unexpected_positions:
            blockers.append("UNEXPECTED_POSITION")
        allowed = not blockers
        return HealthReport(c, allowed, blockers, forced, self.spread_shock,
                            {"tick_age_sec": None if age is None else round(age, 2), "latency_p90_ms": p90,
                             "exec_failures_1h": fails, "uncertain_tickets": sorted(self.recon_uncertain_tickets)})


class StrategyHealthMonitor:
    """Slow layer: classifies each strategy from its closed trade R-multiples."""

    def __init__(self, recent_n: int = 30, min_n: int = 20):
        self.recent_n = recent_n
        self.min_n = min_n
        self.state: Dict[str, StrategyHealth] = {}
        self.details: Dict[str, dict] = {}

    def evaluate(self, sid: str, r_multiples: List[float], cf_opportunity_r: float = 0.0,
                 oos_expectancy_r: Optional[float] = None) -> StrategyHealth:
        n = len(r_multiples)
        if n < self.min_n:
            h = StrategyHealth.HEALTHY
            d = {"n": n, "note": "insufficient live sample - health not statistically assessed"}
        else:
            recent = r_multiples[-self.recent_n:]
            mean_all = statistics.fmean(r_multiples)
            mean_recent = statistics.fmean(recent)
            sd = statistics.pstdev(r_multiples) or 1.0
            upper = mean_all + 1.64 * sd / math.sqrt(n)      # one-sided 95% upper bound
            h = StrategyHealth.HEALTHY
            if mean_recent < -0.3:
                h = StrategyHealth.WATCH
            if mean_recent < -0.5 and n >= 30:
                h = StrategyHealth.DEGRADED
            if n >= 50 and upper < 0:
                h = StrategyHealth.RESTRICTED     # statistically negative: stop new trades, human review
            if oos_expectancy_r is not None and n >= 50 and mean_all < oos_expectancy_r - 2 * sd / math.sqrt(n):
                h = max(h, StrategyHealth.WATCH, key=list(StrategyHealth).index)
            d = {"n": n, "mean_all_r": mean_all, "mean_recent_r": mean_recent, "upper95_r": upper}
        d["counterfactual_opportunity_r"] = cf_opportunity_r
        self.state[sid] = h
        self.details[sid] = d
        return h
