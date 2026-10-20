"""Order-book (depth of market) analytics: imbalance, liquidity walls, pulled walls, refill (iceberg) hints.

Only meaningful when the broker really publishes depth. Most retail CFD servers do NOT (market_book_get returns
nothing); then status is UNAVAILABLE and no number is invented. The logic is exercised by synthetic books in the
unit tests; it has not been validated against a live depth feed.
"""
from __future__ import annotations

import statistics
from typing import Dict, List, Optional, Tuple

Level = Tuple[float, float, str]       # (price, volume, "BID"|"ASK")


class BookTracker:
    def __init__(self, top_levels: int = 10, wall_multiple: float = 3.0, pull_fraction: float = 0.7,
                 refill_alert: int = 3):
        self.top = top_levels
        self.wall_multiple = wall_multiple
        self.pull_fraction = pull_fraction
        self.refill_alert = refill_alert
        self.prev: Dict[Tuple[str, float], float] = {}
        self._dropped: Dict[Tuple[str, float], float] = {}
        self.refills: Dict[Tuple[str, float], int] = {}
        self.unavailable_polls = 0
        self.last: dict = {"status": "UNAVAILABLE"}

    def update(self, levels: Optional[List[Level]]) -> dict:
        if not levels:
            self.unavailable_polls += 1
            self.last = {"status": "UNAVAILABLE", "note": "broker publishes no market depth for this symbol"}
            return self.last
        self.unavailable_polls = 0
        bids = sorted((l for l in levels if l[2] == "BID"), key=lambda x: -x[0])[: self.top]
        asks = sorted((l for l in levels if l[2] == "ASK"), key=lambda x: x[0])[: self.top]
        bv, av = sum(l[1] for l in bids), sum(l[1] for l in asks)
        cur = {(l[2], l[0]): l[1] for l in bids + asks}
        vols = [v for v in cur.values() if v > 0]
        med = statistics.median(vols) if vols else 0.0
        walls = [{"side": k[0], "price": k[1], "volume": v} for k, v in cur.items()
                 if med > 0 and v >= self.wall_multiple * med]
        pulled = []
        for k, old in self.prev.items():
            new = cur.get(k, 0.0)
            if med > 0 and old >= self.wall_multiple * med and new <= old * (1 - self.pull_fraction):
                pulled.append({"side": k[0], "price": k[1], "was": old, "now": new})
                self._dropped[k] = old
        for k, old in list(self._dropped.items()):          # a level that dropped and came back = refill
            if cur.get(k, 0.0) >= 0.8 * old:
                self.refills[k] = self.refills.get(k, 0) + 1
                self._dropped.pop(k)
        iceberg = [{"side": k[0], "price": k[1], "refills": n} for k, n in self.refills.items() if n >= self.refill_alert]
        self.prev = cur
        tot = bv + av
        self.last = {"status": "OK", "imbalance": round((bv - av) / tot, 3) if tot else None, "bid_volume": bv,
                     "ask_volume": av, "walls": walls, "pulled_walls": pulled, "possible_icebergs": iceberg}
        return self.last
