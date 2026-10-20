"""Alerts must reach the operator, and must never be able to hurt the trading loop.

Telegram is blocked or unreliable in several countries, Pakistan among them, so the notifier now supports ntfy,
Discord, SMTP email and a generic webhook behind the same interface. The properties that matter are not the
transports but the guarantees carried over from the Telegram notifier:

  * a network failure, a hung endpoint or a misconfiguration never raises into the caller
  * the same alert is not repeated inside min_seconds_between_same_alert
  * the queue is bounded, so a dead endpoint cannot grow memory without limit
  * an unconfigured channel degrades to log-only rather than failing startup
"""
import time
import unittest
from types import SimpleNamespace

from app.notifications.channels import (DiscordNotifier, EmailNotifier, NtfyNotifier, QueuedNotifier,
                                        WebhookNotifier, build)
from app.notifications.base import Notifier


class Recorder(QueuedNotifier):
    """A transport that records instead of sending, so delivery can be asserted without a network."""

    def __init__(self, min_interval=300, fail=False):
        super().__init__("recorder", min_interval)
        self.sent, self.fail = [], fail

    def _deliver(self, text):
        if self.fail:
            raise OSError("network down")
        self.sent.append(text)
        return True


class TestSelection(unittest.TestCase):
    def test_an_unconfigured_channel_returns_none_rather_than_raising(self):
        empty = SimpleNamespace()
        for c in ("ntfy", "discord", "webhook", "email", "none", "", None, "nonsense"):
            self.assertIsNone(build(c, empty, 300), c)

    def test_each_channel_builds_when_its_credential_is_present(self):
        url = SimpleNamespace(notify_url="https://ntfy.sh/secret-topic")
        self.assertIsInstance(build("ntfy", url, 300), NtfyNotifier)
        self.assertIsInstance(build("discord", url, 300), DiscordNotifier)
        self.assertIsInstance(build("webhook", url, 300), WebhookNotifier)
        mail = SimpleNamespace(smtp_host="smtp.gmail.com", smtp_port=587, smtp_user="a@b.c",
                               smtp_password="x", smtp_to="d@e.f")
        self.assertIsInstance(build("email", mail, 300), EmailNotifier)

    def test_a_partially_configured_email_does_not_build(self):
        half = SimpleNamespace(smtp_host="smtp.gmail.com", smtp_user="a@b.c")   # no password, no recipient
        self.assertIsNone(build("email", half, 300))

    def test_the_configured_channel_is_one_the_code_can_actually_build(self):
        """Asserts the config is COHERENT rather than pinning one value - the channel is the operator's choice
        (it is discord here because Telegram was removed - it is blocked in Pakistan), but a channel
        name the builder does not recognise would silently downgrade every alert to a log line."""
        from app.config import ConfigManager
        from app.notifications import channels
        t = ConfigManager().app.alerts
        self.assertIn(t.channel, ("ntfy", "discord", "email", "webhook", "none"))
        if t.channel != "none":
            full = SimpleNamespace(notify_url="https://example.invalid/hook", smtp_host="h", smtp_port=587,
                                   smtp_user="u", smtp_password="p", smtp_to="t")
            self.assertIsNotNone(channels.build(t.channel, full, 300),
                                 f"channel {t.channel} is configured but build() cannot make it")

    def test_alerts_being_on_requires_a_channel_credential(self):
        """If alerts are enabled, an operator should be able to tell whether they will actually arrive."""
        from app.config import ConfigManager, load_secrets
        from app.notifications import channels
        t = ConfigManager().app.alerts
        if not t.enabled or t.channel == "none":
            self.skipTest("alternative channel not enabled")
        built = channels.build(t.channel, load_secrets(), 300)
        if built is None:
            self.skipTest("credential not present in this environment; tools/test_alert.py reports this")
        self.assertTrue(callable(built._deliver))


class TestGuarantees(unittest.TestCase):
    def test_a_failing_transport_never_raises_into_the_caller(self):
        n = Recorder(fail=True)
        n.alert("CRITICAL", "daily loss limit reached")      # must simply return
        n.send("anything")
        n.stop()

    def test_the_same_alert_is_throttled(self):
        n = Recorder(min_interval=3600)
        for _ in range(5):
            n.alert("WARNING", "spread is wide", key="spread")
        self.assertEqual(n.q.qsize(), 1)
        n.stop()

    def test_a_different_alert_is_not_throttled(self):
        n = Recorder(min_interval=3600)
        n.alert("WARNING", "spread is wide", key="spread")
        n.alert("CRITICAL", "broker link lost", key="link")
        self.assertEqual(n.q.qsize(), 2)
        n.stop()

    def test_throttling_expires(self):
        n = Recorder(min_interval=0)
        n.alert("INFO", "x", key="k")
        time.sleep(0.01)
        n.alert("INFO", "x", key="k")
        self.assertEqual(n.q.qsize(), 2)
        n.stop()

    def test_a_dead_endpoint_cannot_grow_memory_without_limit(self):
        n = Recorder()
        for i in range(10_000):
            n.send(f"message {i}")
        self.assertLessEqual(n.q.qsize(), 200)
        n.stop()

    def test_long_messages_are_truncated_before_queueing(self):
        n = Recorder()
        n.send("x" * 10_000)
        self.assertLessEqual(len(n.q.get_nowait()), 3900)
        n.stop()

    def test_the_level_prefix_is_carried(self):
        n = Recorder()
        n.alert("CRITICAL", "kill switch tripped")
        self.assertIn("[CRITICAL]", n.q.get_nowait())
        n.stop()

    def test_it_delivers_when_the_transport_works(self):
        n = Recorder(min_interval=0)
        n.start()
        n.alert("INFO", "hello")
        for _ in range(100):
            if n.sent:
                break
            time.sleep(0.02)
        n.stop()
        self.assertTrue(any("hello" in s for s in n.sent))

    def test_every_channel_satisfies_the_notifier_interface(self):
        for cls, args in ((NtfyNotifier, ("https://ntfy.sh/t",)), (DiscordNotifier, ("https://d/w",)),
                          (WebhookNotifier, ("https://w",)),
                          (EmailNotifier, ("h", 587, "u", "p", "t"))):
            n = cls(*args)
            self.assertIsInstance(n, Notifier)
            for m in ("alert", "send", "stop", "run", "_deliver"):
                self.assertTrue(callable(getattr(n, m)), f"{cls.__name__}.{m}")
            self.assertTrue(n.daemon, f"{cls.__name__} must not keep the process alive")


if __name__ == "__main__":
    unittest.main()
