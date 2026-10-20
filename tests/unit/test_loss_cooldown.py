import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.config import RiskCfg
from app.core.engine import TradingCore
from app.storage.repo import MemoryRepo


class TestSymbolLossCooldown(unittest.TestCase):
    def make_core(self, now):
        core = TradingCore.__new__(TradingCore)
        core.cfg = SimpleNamespace(risk=RiskCfg())
        core.repo = MemoryRepo()
        core.symbol = "BTCUSDm"
        core.now_utc = lambda: now
        return core

    def add_trade(self, core, closed_at, pnl):
        core.repo.insert("trades", {"symbol": core.symbol, "state": "CLOSED", "closed_at": closed_at,
                                    "net_pnl": pnl})

    def test_three_recent_consecutive_losses_start_symbol_cooldown(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        core = self.make_core(now)
        for minutes_ago in (40, 25, 10):
            self.add_trade(core, now - timedelta(minutes=minutes_ago), -1.0)
        self.assertEqual(core._loss_guard(), (3, True))

    def test_two_losses_reduce_risk_without_triggering_cooldown(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        core = self.make_core(now)
        for minutes_ago in (25, 10):
            self.add_trade(core, now - timedelta(minutes=minutes_ago), -1.0)
        self.assertEqual(core._loss_guard(), (2, False))

    def test_cooldown_expires_and_non_loss_resets_streak(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        core = self.make_core(now)
        for minutes_ago in (110, 100, 90):
            self.add_trade(core, now - timedelta(minutes=minutes_ago), -1.0)
        self.assertEqual(core._loss_guard(), (3, False))
        self.add_trade(core, now - timedelta(minutes=5), 0.0)
        self.assertEqual(core._loss_guard(), (0, False))

    def test_zero_cooldown_allows_entries_but_keeps_loss_streak(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        core = self.make_core(now)
        core.cfg.risk.loss_circuit_breaker_cooldown_minutes = 0
        for minutes_ago in (40, 25, 10):
            self.add_trade(core, now - timedelta(minutes=minutes_ago), -1.0)
        self.assertEqual(core._loss_guard(), (3, False))

        streak, _ = core._loss_guard()
        risk_multiplier = 1.0
        if streak >= core.cfg.risk.loss_reduction_after:
            risk_multiplier *= core.cfg.risk.loss_reduction_multiplier
        self.assertEqual(risk_multiplier, 0.5)

    def test_other_symbols_do_not_share_the_loss_streak(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        core = self.make_core(now)
        for minutes_ago in (40, 25, 10):
            core.repo.insert("trades", {"symbol": "EURUSDm", "state": "CLOSED",
                                        "closed_at": now - timedelta(minutes=minutes_ago), "net_pnl": -1.0})
        self.assertEqual(core._loss_guard(), (0, False))


if __name__ == "__main__":
    unittest.main()
