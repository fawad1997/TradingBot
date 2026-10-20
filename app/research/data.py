"""Historical data loading for research.

Supported inputs
  1. Our export (`python -m app.main export-history`): CSV with header
     time,open,high,low,close,tick_volume,spread,real_volume   (time = UTC epoch seconds)
  2. MetaTrader 5 "Export Bars" CSV (tab separated):
     <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>
     (times are broker server time -> pass --utc-offset if your broker is not UTC)
  3. Exness historical tick CSV (columns include Timestamp, Bid, Ask) -> resampled
     to bid-based bars whose spread column is the MEAN observed spread in the bar.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

from app.core.types import Bar, SymbolSpec, TIMEFRAME_SECONDS
from app.features.frame import FeatureFrame, resample


def save_bars_csv(path: Path, bars: List[Bar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"])
        for b in bars:
            w.writerow([b.time, b.open, b.high, b.low, b.close, b.tick_volume, b.spread, b.real_volume])


def _frame_from_df(df: pd.DataFrame, timeframe: str) -> FeatureFrame:
    df = df.sort_values("time").drop_duplicates("time")
    df = df[(df["high"] >= df["low"]) & (df["open"] > 0) & (df["close"] > 0)]
    return FeatureFrame(df["time"].astype("int64").values, df["open"].values, df["high"].values, df["low"].values,
                        df["close"].values, df.get("spread", pd.Series(np.zeros(len(df)))).values,
                        df.get("tick_volume", pd.Series(np.zeros(len(df)))).values, timeframe)


def load_bars(path: Path, timeframe: str, utc_offset_hours: float = 0.0) -> FeatureFrame:
    path = Path(path)
    with open(path, "r", encoding="utf-8-sig") as f:
        head = f.readline()
    if "<DATE>" in head.upper():
        df = pd.read_csv(path, sep="\t")
        df.columns = [c.strip("<>").lower() for c in df.columns]
        ts = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str), format="%Y.%m.%d %H:%M:%S",
                            errors="coerce")
        if ts.isna().all():
            ts = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str), errors="coerce")
        df = df.assign(time=(ts.astype("int64") // 10**9) - int(utc_offset_hours * 3600))
        df = df.rename(columns={"tickvol": "tick_volume"})
    else:
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]
        if not np.issubdtype(df["time"].dtype, np.number):
            df["time"] = pd.to_datetime(df["time"], utc=True).astype("int64") // 10**9
        df["time"] = df["time"] - int(utc_offset_hours * 3600)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    if "spread" not in df:
        df["spread"] = 0
    return _frame_from_df(df, timeframe)


def ticks_to_bars(path: Path, timeframe: str, point: float, chunksize: int = 2_000_000) -> FeatureFrame:
    """Resample an Exness tick CSV to bid bars with the mean spread (in points) per bar."""
    sec = TIMEFRAME_SECONDS[timeframe]
    parts = []
    for chunk in pd.read_csv(path, chunksize=chunksize):
        cols = {c.lower().strip(): c for c in chunk.columns}
        tcol = next((cols[c] for c in ("timestamp", "time", "datetime", "date") if c in cols), None)
        if tcol is None or "bid" not in cols or "ask" not in cols:
            raise ValueError(f"tick file needs Timestamp/Bid/Ask columns, found {list(chunk.columns)}")
        t = pd.to_datetime(chunk[tcol], utc=True, errors="coerce")
        bid = chunk[cols["bid"]].astype(float)
        ask = chunk[cols["ask"]].astype(float)
        df = pd.DataFrame({"t": t, "bid": bid, "spread": (ask - bid) / point}).dropna()
        df["bucket"] = (df["t"].astype("int64") // 10**9 // sec) * sec
        g = df.groupby("bucket")
        parts.append(pd.DataFrame({"open": g["bid"].first(), "high": g["bid"].max(), "low": g["bid"].min(),
                                   "close": g["bid"].last(), "tick_volume": g["bid"].size(),
                                   "spread": g["spread"].mean(), "first": g["t"].min(), "last": g["t"].max()}))
    if not parts:
        raise ValueError("no ticks found")
    allp = pd.concat(parts)
    # merge buckets split across chunks
    agg = allp.groupby(level=0).agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                     "tick_volume": "sum", "spread": "mean"})
    agg = agg.reset_index().rename(columns={"bucket": "time", "index": "time"})
    if "time" not in agg.columns:
        agg = agg.rename(columns={agg.columns[0]: "time"})
    return _frame_from_df(agg, timeframe)


def frame_slice(ff: FeatureFrame, start: Optional[int] = None, end: Optional[int] = None) -> FeatureFrame:
    mask = np.ones(len(ff), dtype=bool)
    if start is not None:
        mask &= ff.t >= start
    if end is not None:
        mask &= ff.t < end
    return FeatureFrame(ff.t[mask], ff.o[mask], ff.h[mask], ff.l[mask], ff.c[mask], ff.spread[mask],
                        ff.tick_volume[mask], ff.timeframe)


def load_spec(path: Optional[Path]) -> Optional[SymbolSpec]:
    if not path or not Path(path).exists():
        return None
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    d.pop("raw", None)
    return SymbolSpec(**{k: v for k, v in d.items() if k in SymbolSpec.__dataclass_fields__})


def load_dataset(m15_path: Path, h1_path: Optional[Path] = None, utc_offset_hours: float = 0.0):
    m15 = load_bars(m15_path, "M15", utc_offset_hours)
    h1 = load_bars(h1_path, "H1", utc_offset_hours) if h1_path else resample(m15, 3600, "H1")
    return m15, h1


def parse_date(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
