"""Order-flow / microstructure analytics from the live quote stream (RESEARCH-ONLY, never gates a trade).

HONEST LIMITS - read before trusting any number here:
  * MT5 CFD feeds (Exness etc.) carry bid/ask quotes, not an exchange trade tape. There is no aggressor flag and
    no real traded volume. "Volume" below is quote-update (tick) count and buy/sell is inferred with the tick rule
    (mid up = buyer-initiated, mid down = seller-initiated, unchanged = previous direction). CVD, delta, VPIN,
    volume profile and VWAP are therefore PROXIES, useful as features to test, not ground truth.
  * Open interest, liquidations and exchange leverage do not exist for these instruments - reported UNAVAILABLE.
  * Per the project rules (tick volume stays RESEARCH_ONLY until ablation + out-of-sample prove value) nothing
    here influences entries; the snapshot is displayed and stored with each signal for later analysis.
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, Optional, Tuple


class FlowAnalyzer:
    def __init__(self, point: float, bucket_ticks: int = 50, vpin_buckets: int = 20, window_ticks: int = 200,
                 burst_z: float = 2.5, bin_points: float = 20.0, value_area: float = 0.70,
                 poc_history_minutes: int = 15):
        self.point = point or 1e-5
        self.bucket_ticks = bucket_ticks
        self.vpin_n = vpin_buckets
        self.window = window_ticks
        self.burst_z = burst_z
        self.bin = max(self.point, bin_points * self.point)
        self.value_area = value_area
        self.poc_history_minutes = poc_history_minutes
        self.day: Optional[str] = None
        self._reset()

    def _reset(self) -> None:
        self.prev_mid: Optional[float] = None
        self.direction = 0
        self.cvd = 0
        self.recent: Deque[int] = deque(maxlen=self.window)
        self.burst_hist: Deque[int] = deque(maxlen=300)       # rolling 20-tick deltas for the burst z-score
        self._last20: Deque[int] = deque(maxlen=20)
        self.n_ticks = 0
        # VWAP (tick-volume weighted mid) with running variance (Welford, weight 1 per tick)
        self.vw_sum = 0.0
        self.vw_mean = 0.0
        self.vw_m2 = 0.0
        # volume profile
        self.profile: Dict[int, int] = {}
        self._poc_hist: Deque[Tuple[int, float]] = deque(maxlen=self.poc_history_minutes + 2)
        self._last_poc_minute = -1
        # VPIN buckets
        self._b_buy = 0
        self._b_sell = 0
        self.imbalances: Deque[float] = deque(maxlen=self.vpin_n)
        self.last_mid = 0.0

    # ------------------------------------------------------------------ input
    def on_tick(self, bid: float, ask: float, when_utc: datetime) -> None:
        day = when_utc.astimezone(timezone.utc).date().isoformat()
        if day != self.day:               # session = UTC day
            self.day = day
            self._reset()
        mid = (bid + ask) / 2.0
        if self.prev_mid is not None:
            if mid > self.prev_mid:
                self.direction = 1
            elif mid < self.prev_mid:
                self.direction = -1
        self.prev_mid = mid
        self.last_mid = mid
        d = self.direction or 1
        self.n_ticks += 1
        self.cvd += d
        self.recent.append(d)
        self._last20.append(d)
        if len(self._last20) == 20:
            self.burst_hist.append(sum(self._last20))
        # VWAP / variance
        self.vw_sum += mid
        delta = mid - self.vw_mean
        self.vw_mean += delta / self.n_ticks
        self.vw_m2 += delta * (mid - self.vw_mean)
        # profile
        b = int(round(mid / self.bin))
        self.profile[b] = self.profile.get(b, 0) + 1
        # VPIN bucket
        if d > 0:
            self._b_buy += 1
        else:
            self._b_sell += 1
        if self._b_buy + self._b_sell >= self.bucket_ticks:
            self.imbalances.append(abs(self._b_buy - self._b_sell) / (self._b_buy + self._b_sell))
            self._b_buy = self._b_sell = 0
        minute = int(when_utc.timestamp() // 60)
        if minute != self._last_poc_minute and self.profile:
            self._last_poc_minute = minute
            self._poc_hist.append((minute, self._poc()[0]))

    # ------------------------------------------------------------------ derived
    def _poc(self) -> Tuple[float, float, float]:
        """POC and value area (default 70% of volume), expanding from the POC toward the heavier side.
        Bounded: it can never take more steps than the width of the profile (gaps are fine)."""
        if not self.profile:
            return 0.0, 0.0, 0.0
        prof = self.profile
        poc_bin = max(prof, key=prof.get)
        lo_b, hi_b = min(prof), max(prof)
        target = sum(prof.values()) * self.value_area
        lo = hi = poc_bin
        acc = prof[poc_bin]
        while acc < target and (lo > lo_b or hi < hi_b):
            up = prof.get(hi + 1, 0) if hi < hi_b else -1
            dn = prof.get(lo - 1, 0) if lo > lo_b else -1
            if up >= dn:
                hi += 1
                acc += max(up, 0)
            else:
                lo -= 1
                acc += max(dn, 0)
        return poc_bin * self.bin, lo * self.bin, hi * self.bin

    def vpin(self) -> Optional[float]:
        if len(self.imbalances) < max(5, self.vpin_n // 2):
            return None
        return statistics.fmean(self.imbalances)

    @staticmethod
    def vpin_label(v: Optional[float]) -> str:
        # uncalibrated bands - research display only
        return "WARMING_UP" if v is None else "LOW" if v < 0.35 else "ELEVATED" if v < 0.55 else "HIGH"

    def burst(self) -> str:
        if len(self.burst_hist) < 60 or len(self._last20) < 20:
            return "NONE"
        sd = statistics.pstdev(self.burst_hist) or 1.0
        z = (sum(self._last20) - statistics.fmean(self.burst_hist)) / sd
        return "BUY_BURST" if z >= self.burst_z else "SELL_BURST" if z <= -self.burst_z else "NONE"

    def snapshot(self) -> dict:
        if self.n_ticks == 0:
            return {"status": "NO_DATA"}
        n = len(self.recent)
        buys = sum(1 for x in self.recent if x > 0)
        sd = math.sqrt(self.vw_m2 / self.n_ticks) if self.n_ticks > 1 else 0.0
        poc, val, vah = self._poc()
        shift = None
        if len(self._poc_hist) >= 2:
            shift = round((self._poc_hist[-1][1] - self._poc_hist[0][1]) / self.point, 1)
        v = self.vpin()
        return {
            "status": "OK", "basis": "quote-rule proxy (no real tape)", "ticks": self.n_ticks,
            "cvd": self.cvd, "delta_window": sum(self.recent), "aggression_buy_pct": round(100.0 * buys / n, 1) if n else None,
            "burst": self.burst(), "vwap": round(self.vw_mean, 6), "vwap_dev_sigma": round((self.last_mid - self.vw_mean) / sd, 2) if sd > 0 else None,
            "poc": round(poc, 6), "val": round(val, 6), "vah": round(vah, 6),
            "in_value_area": bool(val <= self.last_mid <= vah), "poc_shift_points": shift,
            "vpin": None if v is None else round(v, 3), "vpin_label": self.vpin_label(v),
            "open_interest": "UNAVAILABLE_FOR_CFD", "liquidations": "UNAVAILABLE_FOR_CFD",
        }

    def compact(self) -> dict:
        s = self.snapshot()
        keep = ("status", "cvd", "delta_window", "aggression_buy_pct", "burst", "vwap_dev_sigma", "poc", "val", "vah",
                "in_value_area", "poc_shift_points", "vpin")
        return {k: s.get(k) for k in keep}
