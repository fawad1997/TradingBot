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
from typing import List, Optional

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


class EventCalendar:
    def __init__(self, cfg: EventsCfg, events: Optional[List[EconomicEvent]] = None):
        self.cfg = cfg
        self.events: List[EconomicEvent] = []
        self.loaded_at: Optional[datetime] = None
        if events is not None:
            self.set_events(events)

    def set_events(self, events: List[EconomicEvent]) -> None:
        self.events = sorted([e for e in events if e.impact in self.cfg.impacts and e.currency in self.cfg.currencies],
                             key=lambda e: e.time)
        self.loaded_at = datetime.now(timezone.utc)

    def coverage_ok(self, now: datetime) -> bool:
        """Calendar must contain at least one event in the future or within stale window."""
        if not self.events:
            return False
        latest = self.events[-1].time
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

    def next_event(self, now: datetime) -> Optional[EconomicEvent]:
        for e in self.events:
            if e.time >= now:
                return e
        return None
