"""Optional economic-calendar sync (free Fair Economy / ForexFactory weekly JSON feed).

Runs in ONE background thread for the whole process (the trading loop is never blocked by HTTP). Events are
upserted into the `economic_events` table; every trading core reloads the calendar from the database.
Disabled by default (config calendar_sync.enabled). If the feed is unreachable the CSV-imported events stay.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, List, Optional

from app.market.events import EconomicEvent

log = logging.getLogger("calendar_sync")


def parse_feed(payload, impacts, currencies) -> List[EconomicEvent]:
    """Items look like {title, country (currency code), date (ISO, with offset or Z), impact, forecast, previous}."""
    wanted_i = {i.upper() for i in impacts}
    wanted_c = {c.upper() for c in currencies}
    out: List[EconomicEvent] = []
    for row in payload if isinstance(payload, list) else []:
        try:
            impact = str(row.get("impact", "")).strip().upper()
            cur = str(row.get("country") or row.get("currency") or "").strip().upper()
            if impact not in wanted_i or cur not in wanted_c:
                continue
            dt = datetime.fromisoformat(str(row["date"]).strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out.append(EconomicEvent(dt.astimezone(timezone.utc), cur, impact, str(row.get("title", "")).strip()))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def default_fetch(url: str, timeout: float):
    import requests
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "quantara/0.1"})
    r.raise_for_status()
    return r.json()


class CalendarSyncWorker:
    def __init__(self, repo, cfg_fn: Callable, fetch: Callable = default_fetch,
                 on_error: Optional[Callable[[str], None]] = None):
        self.repo, self.cfg_fn, self.fetch, self.on_error = repo, cfg_fn, fetch, on_error
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.last_ok: Optional[float] = None

    def sync_once(self) -> int:
        app = self.cfg_fn()
        cfg = app.calendar_sync
        events = parse_feed(self.fetch(cfg.url, cfg.timeout_seconds), app.events.impacts, app.events.currencies)
        for e in events:
            self.repo.upsert("economic_events", {"event_time": e.time, "currency": e.currency, "impact": e.impact,
                                                 "title": e.title, "source": "calendar_sync"},
                             ["event_time", "currency", "title"], ["impact", "source"])
        return len(events)

    def _loop(self) -> None:
        while not self.stop.is_set():
            cfg = self.cfg_fn().calendar_sync
            if cfg.enabled:
                try:
                    n = self.sync_once()
                    self.last_ok = datetime.now(timezone.utc).timestamp()
                    log.info("calendar sync: %d matching events", n)
                except Exception as exc:
                    log.warning("calendar sync failed: %s", exc)
                    if self.on_error:
                        self.on_error(str(exc))
            self.stop.wait(max(900, cfg.refresh_minutes * 60))

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="calendar-sync", daemon=True)
        self.thread.start()
