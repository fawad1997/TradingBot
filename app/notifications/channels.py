"""Alert transports.

Everything that makes a transport safe is shared here: a bounded queue, a daemon thread, per-key throttling,
retries with backoff, and the rule that a network failure can never reach the trading loop.

Channels, in the order worth trying:

  ntfy      Free, open source, no account. You pick an unguessable topic and POST plain text to
            https://ntfy.sh/<topic>; the ntfy app on your phone subscribes to it. Self-hostable if you would
            rather not use the public server. Simplest thing that gives real push notifications.
  discord   A channel webhook URL. Free, good mobile app, one POST with a JSON body.
  email     SMTP. Slower than push and easy to ignore, but it is never blocked and needs no third party
            beyond a mail provider. Gmail works with an app password.
  webhook   Anything else that accepts a POST - your own endpoint, a Slack incoming webhook, an SMS gateway.

The topic URL and webhook URLs are SECRETS: anyone holding one can read your alerts, and for Discord can post
into your channel. They live in .env like every other credential.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Dict, Optional

from app.notifications.base import Notifier

log = logging.getLogger(__name__)

MAX_LEN = 3900
RETRIES = 3


class QueuedNotifier(Notifier, threading.Thread):
    """Shared machinery: throttle, queue, daemon thread, retry. Subclasses implement `_deliver` only."""

    def __init__(self, name: str, min_interval: int = 300):
        threading.Thread.__init__(self, name=name, daemon=True)
        self.min_interval = int(min_interval)
        self.q: "queue.Queue[str]" = queue.Queue(maxsize=200)
        self._last: Dict[str, float] = {}
        self._stop_evt = threading.Event()
        self.failures = 0

    def alert(self, level: str, text: str, key: Optional[str] = None) -> None:
        k = key or text[:60]
        now = time.time()
        if now - self._last.get(k, 0) < self.min_interval:
            return
        self._last[k] = now
        Notifier.alert(self, level, text)
        icon = {"CRITICAL": "[CRITICAL]", "WARNING": "[WARNING]"}.get(level, "[INFO]")
        self.send(f"{icon} {text}")

    def send(self, text: str) -> None:
        try:
            self.q.put_nowait(text[:MAX_LEN])
        except queue.Full:          # dropping an alert is always better than blocking the caller
            pass

    def stop(self) -> None:
        self._stop_evt.set()

    def _deliver(self, text: str) -> bool:
        raise NotImplementedError

    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                text = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            for attempt in range(RETRIES):
                try:
                    if self._deliver(text):
                        break
                except Exception as exc:        # network trouble must never affect trading
                    log.warning("%s send failed: %s", self.name, exc)
                time.sleep(2 ** attempt)
            else:
                self.failures += 1


class NtfyNotifier(QueuedNotifier):
    """POST plain text to an ntfy topic. `url` is the full topic URL, e.g. https://ntfy.sh/<unguessable>."""

    def __init__(self, url: str, min_interval: int = 300):
        super().__init__("ntfy", min_interval)
        self.url = url.rstrip("/")

    def _deliver(self, text: str) -> bool:
        import requests
        r = requests.post(self.url, data=text.encode("utf-8"),
                          headers={"Title": "Quantara", "Priority": "default"}, timeout=10)
        if r.status_code < 300:
            return True
        log.warning("ntfy HTTP %s: %s", r.status_code, r.text[:200])
        return False


class DiscordNotifier(QueuedNotifier):
    """POST to a Discord channel webhook URL."""

    def __init__(self, url: str, min_interval: int = 300):
        super().__init__("discord", min_interval)
        self.url = url

    def _deliver(self, text: str) -> bool:
        import requests
        r = requests.post(self.url, json={"content": f"**Quantara**\n{text[:1900]}"}, timeout=10)
        if r.status_code < 300:
            return True
        log.warning("discord HTTP %s: %s", r.status_code, r.text[:200])
        return False


class WebhookNotifier(QueuedNotifier):
    """POST {"text": ...} to any endpoint - your own service, Slack, an SMS gateway."""

    def __init__(self, url: str, min_interval: int = 300):
        super().__init__("webhook", min_interval)
        self.url = url

    def _deliver(self, text: str) -> bool:
        import requests
        r = requests.post(self.url, json={"text": f"Quantara\n{text}"}, timeout=10)
        if r.status_code < 300:
            return True
        log.warning("webhook HTTP %s: %s", r.status_code, r.text[:200])
        return False


class EmailNotifier(QueuedNotifier):
    """SMTP. Never blocked, needs no third-party service beyond a mail provider."""

    def __init__(self, host: str, port: int, user: str, password: str, to: str, min_interval: int = 300):
        super().__init__("email", min_interval)
        self.host, self.port, self.user, self.password, self.to = host, int(port), user, password, to

    def _deliver(self, text: str) -> bool:
        import smtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = f"Quantara - {text.splitlines()[0][:70]}"
        msg["From"] = self.user
        msg["To"] = self.to
        msg.set_content(text)
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=20) as s:
                s.login(self.user, self.password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=20) as s:
                s.starttls()
                s.login(self.user, self.password)
                s.send_message(msg)
        return True


def build(channel: str, secrets, min_interval: int) -> Optional[QueuedNotifier]:
    """The notifier for `channel`, or None when it is not configured. Never raises."""
    c = (channel or "none").strip().lower()
    try:
        if c == "ntfy" and getattr(secrets, "notify_url", None):
            return NtfyNotifier(secrets.notify_url, min_interval)
        if c == "discord" and getattr(secrets, "notify_url", None):
            return DiscordNotifier(secrets.notify_url, min_interval)
        if c == "webhook" and getattr(secrets, "notify_url", None):
            return WebhookNotifier(secrets.notify_url, min_interval)
        if c == "email" and all(getattr(secrets, k, None) for k in
                                ("smtp_host", "smtp_user", "smtp_password", "smtp_to")):
            return EmailNotifier(secrets.smtp_host, getattr(secrets, "smtp_port", 587) or 587,
                                 secrets.smtp_user, secrets.smtp_password, secrets.smtp_to, min_interval)
    except Exception:
        log.exception("could not build the %s notifier; alerts will go to the log only", c)
    return None
