"""Tick validation and spread statistics (the live execution-cost dataset)."""
from __future__ import annotations

import bisect
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

from app.core.types import Tick


@dataclass
class TickCheck:
    ok: bool
    reason: str = ""


class TickValidator:
    """Rejects corrupt, duplicate, out-of-order and implausible ticks."""

    def __init__(self, max_jump_atr: float = 3.0):
        self.max_jump_atr = max_jump_atr
        self.last: Optional[Tick] = None
        self.rejected: Dict[str, int] = {}

    def check(self, t: Tick, atr: Optional[float]) -> TickCheck:
        reason = ""
        if t is None or not (t.bid > 0 and t.ask > 0):
            reason = "NON_POSITIVE_PRICE"
        elif t.ask < t.bid:
            reason = "CROSSED_QUOTE"
        elif self.last is not None and t.time_msc < self.last.time_msc:
            reason = "OUT_OF_ORDER"
        elif self.last is not None and t.time_msc == self.last.time_msc and t.bid == self.last.bid and t.ask == self.last.ask:
            reason = "DUPLICATE"
        elif self.last is not None and atr and atr > 0 and abs(t.mid - self.last.mid) > self.max_jump_atr * atr:
            reason = "PRICE_JUMP"
        if reason:
            self.rejected[reason] = self.rejected.get(reason, 0) + 1
            if reason == "PRICE_JUMP":
                # accept the new level as reference so one real gap doesn't freeze the feed,
                # but report it so the fast health layer can react
                self.last = t
            return TickCheck(False, reason)
        self.last = t
        return TickCheck(True)


class RollingDistribution:
    """Sorted rolling window for percentile queries in O(log n)."""

    def __init__(self, maxlen: int = 20000):
        self.window: Deque[float] = deque()
        self.sorted: List[float] = []
        self.maxlen = maxlen

    def add(self, v: float) -> None:
        self.window.append(v)
        bisect.insort(self.sorted, v)
        if len(self.window) > self.maxlen:
            old = self.window.popleft()
            i = bisect.bisect_left(self.sorted, old)
            if i < len(self.sorted):
                self.sorted.pop(i)

    def __len__(self) -> int:
        return len(self.sorted)

    def percentile_of(self, v: float) -> float:
        if not self.sorted:
            return float("nan")
        # mid-rank: ties count half. With bisect_right a spread equal to the usual value of a (near) fixed-spread
        # symbol scored 100% and was blocked all day; now it scores ~50% and only genuine outliers approach 100%.
        return (bisect.bisect_left(self.sorted, v) + bisect.bisect_right(self.sorted, v)) / (2.0 * len(self.sorted))

    def quantile(self, q: float) -> float:
        if not self.sorted:
            return float("nan")
        idx = min(len(self.sorted) - 1, max(0, int(q * (len(self.sorted) - 1))))
        return self.sorted[idx]


@dataclass
class SpreadCollector:
    point: float
    sample_interval_ms: int = 1000
    by_session: Dict[str, RollingDistribution] = field(default_factory=dict)
    overall: RollingDistribution = field(default_factory=lambda: RollingDistribution(50000))
    buffer: List[dict] = field(default_factory=list)
    _last_sample_ms: int = 0
    tick_times: Deque[float] = field(default_factory=lambda: deque(maxlen=600))

    def spread_points(self, t: Tick) -> float:
        return (t.ask - t.bid) / self.point if self.point > 0 else 0.0

    def tick_rate(self) -> float:
        """Ticks per second over the last ~60 s (wall clock)."""
        if len(self.tick_times) < 2:
            return 0.0
        now = time.time()
        recent = [x for x in self.tick_times if now - x <= 60]
        return len(recent) / 60.0

    def observe(self, t: Tick, session: str, utc_iso: str, extra: Optional[dict] = None) -> float:
        sp = self.spread_points(t)
        self.tick_times.append(time.time())
        dist = self.by_session.setdefault(session, RollingDistribution(20000))
        pct = dist.percentile_of(sp) if len(dist) else float("nan")
        if t.time_msc - self._last_sample_ms >= self.sample_interval_ms:
            self._last_sample_ms = t.time_msc
            dist.add(sp)
            self.overall.add(sp)
            row = {"time": utc_iso, "time_msc": t.time_msc, "bid": t.bid, "ask": t.ask, "spread_points": sp,
                   "spread_pctile": None if pct != pct else round(pct, 4), "session": session,
                   "tick_rate": round(self.tick_rate(), 3)}
            if extra:
                row.update(extra)
            self.buffer.append(row)
        return sp

    def percentile(self, sp: float, session: str, min_samples: int) -> Optional[float]:
        dist = self.by_session.get(session)
        if dist is not None and len(dist) >= min_samples:
            return dist.percentile_of(sp)
        if len(self.overall) >= min_samples:
            return self.overall.percentile_of(sp)
        return None

    def median(self, session: Optional[str] = None) -> float:
        d = self.by_session.get(session) if session else None
        d = d if d is not None and len(d) else self.overall
        return d.quantile(0.5)

    def drain(self) -> List[dict]:
        out, self.buffer = self.buffer, []
        return out

    def seed(self, samples: List[tuple]) -> None:
        """Seed from history: list of (session, spread_points)."""
        for session, sp in samples:
            self.by_session.setdefault(session, RollingDistribution(20000)).add(sp)
            self.overall.add(sp)
