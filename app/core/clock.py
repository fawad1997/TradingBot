"""Broker clock: converts broker server epoch -> real UTC."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from app.core.types import epoch_to_utc


class BrokerClock:
    def __init__(self, fixed_offset_hours: Optional[float] = None, wall=time.time):
        self.fixed = fixed_offset_hours
        self.offset_seconds = int(fixed_offset_hours * 3600) if fixed_offset_hours is not None else 0
        self.detected = fixed_offset_hours is not None
        self._wall = wall

    def observe(self, broker_epoch: float, fresh: bool = True) -> None:
        """Auto-detect the broker's UTC offset from a FRESH tick (rounded to 15 min)."""
        if self.fixed is not None or not fresh:
            return
        diff = broker_epoch - self._wall()
        q = round(diff / 900.0) * 900
        if abs(q) <= 14 * 3600:
            self.offset_seconds = int(q)
            self.detected = True

    def to_utc(self, broker_epoch: float) -> datetime:
        return epoch_to_utc(broker_epoch - self.offset_seconds)

    def utc_iso(self, broker_epoch: float) -> str:
        return self.to_utc(broker_epoch).isoformat()

    def from_utc(self, dt: datetime) -> float:
        return dt.astimezone(timezone.utc).timestamp() + self.offset_seconds
