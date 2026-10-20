"""Backtrader: independent bar-by-bar event-driven replay of ONE parameter set, as a cross-check of the VectorBT result."""
from __future__ import annotations

import warnings
from typing import Optional

import numpy as np
import pandas as pd

from app.research.metrics import trade_metrics


def validate(m1, sig: dict, point: float, sl_atr: float, rr: float, start_index: int = 0, spread_price=None,
             commission_price: float = 0.0, min_stop_spread_multiple: float = 0.0) -> dict:
    import backtrader as bt
    warnings.filterwarnings("ignore")
    sl = slice(start_index, None)
    idx = pd.to_datetime(m1.t[sl], unit="s")                      # backtrader wants naive timestamps
    df = pd.DataFrame({"open": m1.o[sl], "high": m1.h[sl], "low": m1.l[sl], "close": m1.c[sl],
                       "volume": m1.tick_volume[sl], "openinterest": 0.0}, index=idx)
    longs, shorts, atr = sig["long"][sl], sig["short"][sl], sig["atr"][sl]
    # one fixed cost per fill (Backtrader slippage is a constant): the median of the real per-bar spread when known
    spreads = (m1.spread[sl] * point) if spread_price is None else np.asarray(spread_price, float)[sl]
    bar_spreads = np.nan_to_num(np.asarray(spreads, float), nan=0.0)
    spreads = spreads[np.isfinite(spreads)]
    half_spread = float(np.nanmedian(spreads[spreads > 0]) / 2.0) if np.any(spreads > 0) else 0.0
    half_spread += commission_price / 2.0
    results = []

    class Replay(bt.Strategy):
        def __init__(self):
            self.i = -1
            self.risk = None

        def next(self):
            self.i += 1
            if self.position or self.i >= len(longs) - 1:
                return
            is_long, is_short = bool(longs[self.i]), bool(shorts[self.i])
            if not (is_long or is_short) or not np.isfinite(atr[self.i]):
                return
            nxt = min(self.i + 1, len(bar_spreads) - 1)
            risk = max(float(atr[self.i]) * sl_atr, min_stop_spread_multiple * float(bar_spreads[nxt]))
            px = float(self.data.close[0])
            self.risk = risk
            if is_long:
                self.buy_bracket(exectype=bt.Order.Market, stopprice=px - risk, limitprice=px + risk * rr, size=1)
            else:
                self.sell_bracket(exectype=bt.Order.Market, stopprice=px + risk, limitprice=px - risk * rr, size=1)

        def notify_trade(self, trade):
            if trade.isclosed and self.risk:
                results.append(trade.pnlcomm / self.risk)

    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(Replay)
    cerebro.broker.setcash(1_000_000.0)
    if half_spread > 0:
        cerebro.broker.set_slippage_fixed(half_spread)             # pay half the spread on every fill
    cerebro.run(runonce=False)
    out = trade_metrics(results)
    out["engine"] = "backtrader"
    out["half_spread_price"] = half_spread
    return out
