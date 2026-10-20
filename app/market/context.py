"""Related-market context: what the US dollar, US stocks, volatility and bond yields are doing right now.

Gold tends to move against the US dollar and US real yields; stocks move with the S&P 500 and against the VIX. These
are attached to every signal as plain numbers (recent % change and how stretched the market is versus its last 20
M15 candles) so the learned trade filter can find out whether they matter. They never block a trade by themselves.

Each group lists candidate broker names; the first one this account has is used (any account suffix works). If the
broker has no dollar index, one is built from the six ICE DXY currency pairs that ARE available (EURUSD, USDJPY,
GBPUSD, USDCAD, USDSEK, USDCHF), re-weighted over the ones found.

One feed is shared by every symbol's trading core (they share one broker connection) and refreshes at most once a
minute, so it costs a handful of history calls per minute in total.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Dict, List, Optional

import numpy as np

from app.market.symbols import resolve

log = logging.getLogger("market.context")

# ICE U.S. Dollar Index weights (exponents) - EUR, JPY, GBP, CAD, SEK, CHF
DXY_WEIGHTS = {"EURUSD": -0.576, "USDJPY": 0.136, "GBPUSD": -0.119, "USDCAD": 0.091, "USDSEK": 0.042, "USDCHF": 0.036}

_FEEDS: Dict[int, "ContextFeed"] = {}
_FEEDS_LOCK = threading.Lock()


def shared_feed(adapter, cfg) -> "ContextFeed":
    with _FEEDS_LOCK:
        feed = _FEEDS.get(id(adapter))
        if feed is None:
            feed = _FEEDS[id(adapter)] = ContextFeed(adapter, cfg)
        feed.cfg = cfg
        return feed


def _stats(closes: np.ndarray) -> Dict[str, Optional[float]]:
    c = np.asarray(closes, float)
    c = c[np.isfinite(c) & (c > 0)]
    if len(c) < 21:
        return {}

    def ret(k):
        return round(100.0 * (c[-1] / c[-1 - k] - 1.0), 4) if len(c) > k else None
    window = c[-20:]
    sd = float(np.std(window))
    return {"ret_1h_pct": ret(4), "ret_4h_pct": ret(16),
            "z20": round(float((c[-1] - np.mean(window)) / sd), 3) if sd > 0 else 0.0}


class ContextFeed:
    def __init__(self, adapter, cfg):
        self.adapter = adapter
        self.cfg = cfg
        self._resolved: Dict[str, Optional[str]] = {}
        self._dxy_parts: Optional[Dict[str, str]] = None
        self._cache: Dict[str, float] = {}
        self._at = 0.0
        self._lock = threading.Lock()

    # -------------------------------------------------------------- symbol discovery
    def _resolve_group(self, group: str, candidates: List[str]) -> Optional[str]:
        if group not in self._resolved:
            name = None
            for cand in candidates:
                try:
                    name = resolve(self.adapter, cand)
                except Exception:
                    name = None
                if name:
                    break
            self._resolved[group] = name
            log.info("context %s -> %s", group, name or "not available on this account")
        return self._resolved[group]

    def _dxy_components(self) -> Dict[str, str]:
        if self._dxy_parts is None:
            parts = {}
            for pair in DXY_WEIGHTS:
                try:
                    name = resolve(self.adapter, pair)
                except Exception:
                    name = None
                if name:
                    parts[pair] = name
            self._dxy_parts = parts
            log.info("synthetic dollar index from %s", ", ".join(parts) or "nothing")
        return self._dxy_parts

    def _closes(self, symbol: str, n: int = 40) -> Optional[np.ndarray]:
        bars = self.adapter.rates(symbol, "M15", n + 1)
        if not bars or len(bars) < 22:
            return None
        return np.array([b.close for b in bars[:-1]], float)          # the last bar may still be forming

    def _synthetic_dxy(self) -> Optional[np.ndarray]:
        parts = self._dxy_components()
        if not {"EURUSD", "USDJPY", "GBPUSD"} <= set(parts):
            return None
        series = {}
        for pair, name in parts.items():
            c = self._closes(name)
            if c is not None:
                series[pair] = c
        if not {"EURUSD", "USDJPY", "GBPUSD"} <= set(series):
            return None
        n = min(len(v) for v in series.values())
        log_index = np.zeros(n)
        for pair, c in series.items():
            log_index += DXY_WEIGHTS[pair] * np.log(c[-n:])
        return np.exp(log_index)          # the constant factor does not change % moves or z-scores

    # -------------------------------------------------------------- public
    def features(self) -> Dict[str, float]:
        cfg = getattr(self.cfg, "context", None)
        if cfg is None or not cfg.enabled:
            return {}
        with self._lock:
            if time.time() - self._at < cfg.refresh_seconds:
                return dict(self._cache)
            self._at = time.time()
            out: Dict[str, float] = {}
            for group, candidates in cfg.groups.items():
                try:
                    name = self._resolve_group(group, candidates)
                    closes = self._closes(name) if name else None
                    source = name
                    if closes is None and group == "usd_index" and cfg.synthetic_dollar_index:
                        closes, source = self._synthetic_dxy(), "synthetic"
                    if closes is None:
                        continue
                    for k, v in _stats(closes).items():
                        if v is not None and math.isfinite(v):
                            out[f"{group}_{k}"] = v
                    out[f"{group}_source"] = source
                except Exception as exc:
                    log.debug("context %s failed: %s", group, exc)
            self._cache = out
            return dict(out)
