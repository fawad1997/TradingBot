"""SimMarket: a market-data-only broker adapter that replays a bar history
(synthetic or loaded from CSV) as a tick stream with a controllable clock.

Used for:
  * unit / integration / chaos tests (no MT5 needed);
  * `python -m app.main run --simulate` so you can watch the full pipeline and
    dashboard work on any PC before connecting MetaTrader 5.
Trading is never done here - wrap it in PaperBroker.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np

from app.broker.base import BrokerAdapter, BrokerError
from app.core.types import AccountInfo, Bar, Deal, OrderRequest, OrderResult, Position, Side, SymbolSpec, Tick, TIMEFRAME_SECONDS


def xau_spec(name: str = "XAUUSD", stops_level: int = 0) -> SymbolSpec:
    return SymbolSpec(name=name, digits=2, point=0.01, tick_size=0.01, tick_value=1.0, tick_value_loss=1.0,
                      tick_value_profit=1.0, contract_size=100.0, volume_min=0.01, volume_max=200.0,
                      volume_step=0.01, stops_level=stops_level, freeze_level=0, filling_mode_flags=1,
                      execution_mode="MARKET", trade_mode="FULL", swap_long=-30.0, swap_short=10.0,
                      currency_base="XAU", currency_profit="USD", currency_margin="XAU",
                      description="Simulated gold")


def synthetic_bars(n: int, base_seconds: int = 300, start_price: float = 2400.0, seed: int = 7,
                   start_epoch: int = 1_704_067_200, spread_points: int = 20) -> List[Bar]:
    """Regime-switching random walk (trend legs, pullbacks, ranges) on a fixed grid.

    Synthetic data is for testing plumbing only. It says NOTHING about whether a
    strategy works on real gold.
    """
    rng = np.random.default_rng(seed)
    bars: List[Bar] = []
    price = start_price
    t = start_epoch - (start_epoch % base_seconds)
    drift, regime_left = 0.0, 0
    vol = 0.9 * math.sqrt(base_seconds / 300.0)
    for _ in range(n):
        if regime_left <= 0:
            choice = rng.choice(["up", "down", "range"], p=[0.38, 0.32, 0.30])
            drift = {"up": 0.18, "down": -0.18, "range": 0.0}[choice] * math.sqrt(base_seconds / 300.0)
            regime_left = int(rng.integers(150, 900))
        regime_left -= 1
        # skip weekends (Fri 21:00 - Sun 22:00 UTC)
        dt = datetime.fromtimestamp(t, tz=timezone.utc)
        while (dt.weekday() == 4 and dt.hour >= 21) or dt.weekday() == 5 or (dt.weekday() == 6 and dt.hour < 22):
            t += base_seconds
            dt = datetime.fromtimestamp(t, tz=timezone.utc)
        o = price
        steps = rng.normal(drift / 4.0, vol / 2.0, 4)
        path = o + np.cumsum(steps)
        c = float(path[-1])
        h = float(max(o, path.max()) + abs(rng.normal(0, vol * 0.2)))
        l = float(min(o, path.min()) - abs(rng.normal(0, vol * 0.2)))
        sp = int(max(5, rng.normal(spread_points, spread_points * 0.15)))
        bars.append(Bar(t, round(o, 2), round(h, 2), round(l, 2), round(c, 2), int(rng.integers(50, 400)), sp, 0))
        price = c
        t += base_seconds
    return bars


def aggregate(bars: List[Bar], seconds: int) -> List[Bar]:
    out: List[Bar] = []
    cur = None
    for b in bars:
        k = b.time - (b.time % seconds)
        if cur is None or cur[0] != k:
            if cur is not None:
                out.append(Bar(cur[0], cur[1], cur[2], cur[3], cur[4], cur[5], cur[6], 0))
            cur = [k, b.open, b.high, b.low, b.close, b.tick_volume, b.spread]
        else:
            cur[2] = max(cur[2], b.high)
            cur[3] = min(cur[3], b.low)
            cur[4] = b.close
            cur[5] += b.tick_volume
    if cur is not None:
        out.append(Bar(cur[0], cur[1], cur[2], cur[3], cur[4], cur[5], cur[6], 0))
    return out


class SimMarket(BrokerAdapter):
    name = "sim"

    def __init__(self, base_bars: List[Bar], base_seconds: int = 300, symbol: str = "XAUUSD",
                 spec: Optional[SymbolSpec] = None, account_mode: str = "DEMO", leverage: int = 500,
                 balance: float = 100.0):
        self.bars = base_bars
        self.base = base_seconds
        self.symbol = symbol
        self.spec = spec or xau_spec(symbol)
        self.account_mode = account_mode
        self.leverage = leverage
        self.balance = balance
        self.idx = 0               # index of the current base bar
        self.sub = 0               # tick index within the bar (0..3)
        self.connected = False
        self.tick_override: Optional[Tick] = None
        self.disconnected = False
        self._times = np.array([b.time for b in base_bars], dtype=np.int64)

    # ---- clock control
    @property
    def now(self) -> float:
        b = self.bars[self.idx]
        return b.time + [1, self.base * 0.33, self.base * 0.66, self.base - 1][self.sub]

    def step(self) -> bool:
        """Advance one tick. Returns False at the end of data."""
        self.tick_override = None
        if self.sub < 3:
            self.sub += 1
            return True
        if self.idx + 1 >= len(self.bars):
            return False
        self.idx += 1
        self.sub = 0
        return True

    def seek(self, idx: int) -> None:
        self.idx, self.sub = idx, 0

    def advance_bars(self, n: int) -> None:
        for _ in range(n * 4):
            if not self.step():
                break

    # ---- connection
    def connect(self) -> None:
        if self.disconnected:
            raise BrokerError("simulated disconnect")
        self.connected = True

    def shutdown(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected and not self.disconnected

    def terminal_info(self) -> dict:
        return {"connected": self.is_connected(), "trade_allowed": True, "ping_ms": 1.0, "company": "SimMarket"}

    def account_info(self) -> AccountInfo:
        return AccountInfo(login=999, server="Sim-Demo", company="SimMarket", currency="USD", leverage=self.leverage,
                           trade_mode=self.account_mode, balance=self.balance, equity=self.balance, margin=0.0,
                           margin_free=self.balance, margin_level=0.0, profit=0.0)

    def find_symbols(self, pattern: str) -> List[str]:
        return [self.symbol]

    def select_symbol(self, symbol: str) -> bool:
        return symbol == self.symbol

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        if symbol != self.symbol:
            raise BrokerError(f"unknown symbol {symbol}")
        return self.spec

    # ---- data
    def tick(self, symbol: str) -> Optional[Tick]:
        if self.disconnected:
            return None
        if self.tick_override is not None:
            return self.tick_override
        b = self.bars[self.idx]
        up = b.close >= b.open
        path = [b.open, b.low if up else b.high, b.high if up else b.low, b.close]
        bid = path[self.sub]
        ask = round(bid + b.spread * self.spec.point, self.spec.digits)
        return Tick(int(self.now * 1000), bid, ask)

    def _forming(self) -> Bar:
        cur = self.bars[self.idx]
        # forming base bar: only what is known up to the current tick
        up = cur.close >= cur.open
        path = [cur.open, cur.low if up else cur.high, cur.high if up else cur.low, cur.close][: self.sub + 1]
        return Bar(cur.time, cur.open, max(path), min(path), path[-1], cur.tick_volume, cur.spread, 0)

    def _upto_now(self) -> List[Bar]:
        return self.bars[:self.idx] + [self._forming()]

    def rates(self, symbol: str, timeframe: str, count: int) -> List[Bar]:
        if self.disconnected:
            return []
        sec = TIMEFRAME_SECONDS[timeframe]
        # only aggregate the tail we need
        need_base = (count + 2) * max(1, sec // self.base)
        start = max(0, self.idx - need_base)
        series = self.bars[start:self.idx] + [self._forming()]
        agg = aggregate(series, sec) if sec != self.base else series
        return agg[-count:]

    def rates_range(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> List[Bar]:
        sec = TIMEFRAME_SECONDS[timeframe]
        s, e = start.timestamp(), end.timestamp()
        series = [b for b in self._upto_now() if s - sec <= b.time <= e]
        agg = aggregate(series, sec) if sec != self.base else series
        return [b for b in agg if s <= b.time <= e]

    def server_time(self) -> float:
        return self.now

    def calc_margin(self, symbol: str, side: Side, volume: float, price: float) -> Optional[float]:
        return volume * self.spec.contract_size * price / self.leverage

    def calc_profit(self, symbol: str, side: Side, volume: float, price_open: float, price_close: float) -> Optional[float]:
        return (price_close - price_open) * side.sign * volume * self.spec.contract_size

    # ---- trading is not supported on a data-only adapter
    def positions(self, symbol=None):
        return []

    def history_deals(self, start, end):
        return []

    def order_check(self, req: OrderRequest) -> OrderResult:
        raise BrokerError("SimMarket is data-only; wrap it in PaperBroker")

    order_send = order_check

    def modify_sltp(self, symbol, ticket, sl, tp) -> OrderResult:
        raise BrokerError("SimMarket is data-only")

    def close_position(self, position, volume, deviation, magic, comment) -> OrderResult:
        raise BrokerError("SimMarket is data-only")
