"""The notifier interface, and the no-op notifier the system falls back to.

Alerts are "wake me up" events plus one daily summary - never per-tick spam. Per-trade OPEN/CLOSE messages are
a separate, opt-in layer (app/notifications/trade_alerts.py).

The no-op notifier is not a stub for tests: it is what runs whenever no channel is configured or a transport
could not be built. Alerts still reach the log, and nothing in the trading loop has to know the difference.
That is the whole contract here - a notifier can fail, be absent, or be switched off, and trading continues.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

LEVELS = {"CRITICAL": logging.CRITICAL, "WARNING": logging.WARNING}


class Notifier:
    """No-op notifier, and the base class every real transport extends."""

    def alert(self, level: str, text: str, key: Optional[str] = None) -> None:
        log.log(LEVELS.get(level, logging.INFO), "ALERT[%s] %s", level, text)

    def send(self, text: str) -> None:
        log.info("NOTIFY %s", text)

    def stop(self) -> None:
        pass


def make_notifier(channel: str, secrets, enabled: bool, min_interval: int) -> Notifier:
    """The configured transport, already started, or the no-op notifier when none can be built.

    Never raises and never returns None: a caller can always just use what it gets back.
    """
    if not enabled:
        return Notifier()
    from app.notifications.channels import build
    built = build(channel, secrets, min_interval)
    if built is None:
        log.warning("alerts are enabled but the %s channel is not configured; alerts will go to the log only",
                    channel)
        return Notifier()
    from app.notifications.routing import build_routed
    routed = build_routed(channel, secrets, min_interval, built)
    routed.start()
    return routed
