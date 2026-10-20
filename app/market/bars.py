"""Closed-bar store with temporal integrity.

A bar is accepted as FINAL only when all of these hold:
  1. Broker bar identity: a newer bar exists after it in the broker series
     (the broker has opened the next bar).
  2. Broker timestamp: broker server time >= bar open + timeframe + confirm buffer.
  3. Sanity: OHLC consistent, aligned to the timeframe boundary, strictly newer
     than the last accepted bar.
The local PC clock is never used to decide that a bar has closed.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional, Tuple

from app.core.types import Bar, TIMEFRAME_SECONDS

log = logging.getLogger(__name__)


@dataclass
class BarIssue:
    kind: str       # CORRUPT / MISALIGNED / GAP / OUT_OF_ORDER
    timeframe: str
    bar_time: int
    detail: str


def bar_is_sane(b: Bar) -> bool:
    vals = (b.open, b.high, b.low, b.close)
    if any(v is None or v != v or v <= 0 for v in vals):
        return False
    return b.high >= max(b.open, b.close, b.low) and b.low <= min(b.open, b.close, b.high)


class BarStore:
    def __init__(self, symbol: str, timeframe: str, maxlen: int = 2000, confirm_buffer_sec: float = 2.0):
        self.symbol = symbol
        self.timeframe = timeframe
        self.seconds = TIMEFRAME_SECONDS[timeframe]
        self.confirm_buffer = confirm_buffer_sec
        self.bars: Deque[Bar] = deque(maxlen=maxlen)
        self.issues: Deque[BarIssue] = deque(maxlen=200)
        self.version = 0     # increments whenever a new closed bar is accepted

    @property
    def last_time(self) -> Optional[int]:
        return self.bars[-1].time if self.bars else None

    def to_list(self) -> List[Bar]:
        return list(self.bars)

    def _accept(self, b: Bar) -> bool:
        if not bar_is_sane(b):
            self.issues.append(BarIssue("CORRUPT", self.timeframe, b.time, repr(b)))
            log.warning("Rejected corrupt %s bar %s", self.timeframe, b)
            return False
        if b.time % self.seconds != 0 and self.seconds <= 3600:
            self.issues.append(BarIssue("MISALIGNED", self.timeframe, b.time, "not on timeframe boundary"))
            return False
        last = self.last_time
        if last is not None and b.time <= last:
            if b.time < last:
                self.issues.append(BarIssue("OUT_OF_ORDER", self.timeframe, b.time, f"last={last}"))
            return False   # duplicates are silently ignored
        if last is not None and b.time - last > self.seconds:
            gap = (b.time - last) // self.seconds - 1
            # weekend / daily breaks are normal; record anyway for audit
            self.issues.append(BarIssue("GAP", self.timeframe, b.time, f"{gap} missing bars"))
        self.bars.append(b)
        self.version += 1
        return True

    def hydrate(self, series: List[Bar], server_time: float) -> int:
        """Load history. `series` is oldest->newest INCLUDING the forming bar at the end."""
        accepted = 0
        for b in self.closed_candidates(series, server_time):
            if self._accept(b):
                accepted += 1
        return accepted

    def closed_candidates(self, series: List[Bar], server_time: float) -> List[Bar]:
        """Bars from `series` that are final under the rules above."""
        if len(series) < 2:
            return []
        out = []
        for idx in range(len(series) - 1):
            b = series[idx]
            nxt = series[idx + 1]
            if nxt.time <= b.time:
                continue
            if server_time < b.time + self.seconds + self.confirm_buffer:
                continue
            out.append(b)
        return out

    def poll(self, fetch: Callable[[int], List[Bar]], server_time: float) -> List[Bar]:
        """Fetch the latest few bars and accept any newly finalised ones.

        If more than the fetched window was missed (e.g. after a reconnect) the
        window is widened so no bar is silently skipped.
        """
        window = 5
        series = fetch(window)
        if self.bars and series and series[0].time > self.bars[-1].time + self.seconds:
            needed = int((series[-1].time - self.bars[-1].time) // self.seconds) + 3
            series = fetch(min(max(needed, window), 5000))
        new = []
        for b in self.closed_candidates(series, server_time):
            if self.last_time is None or b.time > self.last_time:
                if self._accept(b):
                    new.append(b)
        return new


def required_bars(lookbacks: List[int], warmup: int, safety_margin: float, minimum: int,
                  recursive_multiple: float = 1.0) -> int:
    """Hydration size from the actual feature dependency graph:
    longest lookback x recursive-indicator warm-up multiple + warm-up + safety margin."""
    longest = max(lookbacks) if lookbacks else 0
    n = int((longest * max(1.0, recursive_multiple) + warmup) * (1.0 + safety_margin))
    return max(n, minimum)
