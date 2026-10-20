"""Session classification (UTC). Sessions are measured, never assumed good or bad."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import SessionsCfg
from app.core.types import Session


class SessionClassifier:
    def __init__(self, cfg: SessionsCfg, always_open: bool = False):
        self.cfg = cfg
        # True for symbols that trade through the weekend (config broker.always_open_symbols)
        self.always_open = always_open

    @staticmethod
    def market_closed(dt: datetime) -> bool:
        """Spot gold CFD weekend: roughly Fri 21:00 UTC -> Sun 22:00 UTC."""
        wd, hour = dt.weekday(), dt.hour + dt.minute / 60.0
        if wd == 5:
            return True
        if wd == 4 and hour >= 21.0:
            return True
        if wd == 6 and hour < 22.0:
            return True
        return False

    @classmethod
    def market_status(cls, dt: datetime, always_open: bool = False) -> dict:
        """Return the estimated weekend status and next open time for a symbol."""
        dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        if always_open or not cls.market_closed(dt):
            return {"status": "OPEN", "opens_at_utc": None, "seconds_to_open": None}

        days_until_sunday = (6 - dt.weekday()) % 7
        opens_at = (dt + timedelta(days=days_until_sunday)).replace(
            hour=22, minute=0, second=0, microsecond=0
        )
        if opens_at <= dt:
            opens_at += timedelta(days=7)
        return {
            "status": "MARKET_CLOSED",
            "opens_at_utc": opens_at.isoformat(),
            "seconds_to_open": int((opens_at - dt).total_seconds()),
        }

    def classify(self, dt: datetime) -> Session:
        dt = dt.astimezone(timezone.utc)
        if self.market_closed(dt):
            # always-open symbols keep trading, but weekend quotes get their own bucket so wider weekend
            # spreads never contaminate the weekday ASIA/LONDON/NEW_YORK spread statistics
            return Session.WEEKEND if self.always_open else Session.CLOSED
        if dt.weekday() == 6 and not self.always_open:   # Sunday after the weekly open
            open_dt = dt.replace(hour=22, minute=0, second=0, microsecond=0)
            if dt - open_dt < timedelta(minutes=self.cfg.weekend_open_minutes):
                return Session.WEEKEND_OPEN
        h = dt.hour + dt.minute / 60.0
        for w in self.cfg.windows:
            if w.start_utc <= w.end_utc:
                inside = w.start_utc <= h < w.end_utc
            else:
                inside = h >= w.start_utc or h < w.end_utc
            if inside:
                try:
                    return Session(w.name)
                except ValueError:
                    continue
        return Session.ASIA
