"""Economic event calendar -> EVENT_STATE.

The calendar is a CSV you maintain (config/economic_events.csv) and import with
`python -m app.main import-events`. No paid data feed is assumed. If the calendar
is missing or stale, the state is UNKNOWN (never silently NORMAL).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from app.config import EventsCfg
from app.core.types import EventState


@dataclass
class EconomicEvent:
    time: datetime
    currency: str
    impact: str
    title: str


def parse_dt(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_events_csv(path: Path) -> List[EconomicEvent]:
    """CSV columns: datetime_utc,currency,impact,title"""
    out: List[EconomicEvent] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("datetime_utc") or row["datetime_utc"].startswith("#"):
                continue
            out.append(EconomicEvent(parse_dt(row["datetime_utc"]), row.get("currency", "USD").strip().upper(),
                                     row.get("impact", "HIGH").strip().upper(), row.get("title", "").strip()))
    return out


def symbol_currencies(spec) -> Optional[set]:
    """The currencies a symbol moves on, from the broker's own spec (EURUSD -> EUR, USD; NVDA -> USD; gold -> XAU,
    USD). None when the spec names none, which keeps the calendar unscoped."""
    got = {str(c).strip().upper() for c in (getattr(spec, "currency_base", ""), getattr(spec, "currency_profit", ""))
           if c and str(c).strip()}
    return got or None


class EventCalendar:
    """`symbol_currencies` scopes the calendar to one symbol: a UK CPI release puts GBPUSD into PRE_EVENT and leaves
    NVDA alone. Unscoped (None), every watched event applies, as before."""

    def __init__(self, cfg: EventsCfg, events: Optional[List[EconomicEvent]] = None,
                 symbol_currencies: Optional[Iterable[str]] = None):
        self.cfg = cfg
        self.symbol_currencies = {c.upper() for c in symbol_currencies} if symbol_currencies else None
        self.all_events: List[EconomicEvent] = []      # every watched event, whatever the symbol
        self.events: List[EconomicEvent] = []          # the ones that apply to this symbol
        self.loaded_at: Optional[datetime] = None
        if events is not None:
            self.set_events(events)

    def set_events(self, events: List[EconomicEvent]) -> None:
        self.all_events = sorted([e for e in events if e.impact in self.cfg.impacts
                                  and e.currency in self.cfg.currencies], key=lambda e: e.time)
        self._scope()
        self.loaded_at = datetime.now(timezone.utc)

    def scope_to(self, currencies: Optional[Iterable[str]]) -> None:
        self.symbol_currencies = {c.upper() for c in currencies} if currencies else None
        self._scope()

    def _scope(self) -> None:
        cur = self.symbol_currencies
        self.events = [e for e in self.all_events if cur is None or e.currency in cur]

    def coverage_ok(self, now: datetime) -> bool:
        """Calendar must contain at least one event in the future or within stale window.

        Judged on every watched event, not only this symbol's: a quiet fortnight for JPY does not mean the
        calendar feed is stale."""
        feed = self.all_events or self.events          # `events` alone when a caller set it directly
        if not feed:
            return False
        latest = feed[-1].time
        return latest >= now - timedelta(days=self.cfg.stale_after_days)

    def state(self, now: datetime) -> EventState:
        if not self.coverage_ok(now):
            return EventState.UNKNOWN
        pre = timedelta(minutes=self.cfg.pre_minutes)
        during = timedelta(minutes=self.cfg.event_minutes)
        post = timedelta(minutes=self.cfg.post_minutes)
        st = EventState.NORMAL
        for e in self.events:
            if e.time - pre <= now < e.time:
                st = EventState.PRE_EVENT
            elif e.time <= now < e.time + during:
                return EventState.EVENT
            elif e.time + during <= now < e.time + during + post and st == EventState.NORMAL:
                st = EventState.POST_EVENT
            if e.time - pre > now:
                break
        return st

    def next_event(self, now: datetime, currencies: Optional[Iterable[str]] = None) -> Optional[EconomicEvent]:
        """The next event for this symbol, optionally narrowed further to `currencies`."""
        want = {c.upper() for c in currencies} if currencies else None
        for e in self.events:
            if e.time >= now and (want is None or e.currency in want):
                return e
        return None
