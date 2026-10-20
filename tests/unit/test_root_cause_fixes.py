import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import ConfigManager
from app.core.types import RiskState
from app.risk.drawdown import DrawdownTracker
from app.storage.writer import FallbackJournal, Ledger


class BrokenRepo:
    def insert(self, table, row):
        raise RuntimeError(f"table '{table}' does not exist")


class TestRootCauseFixes(unittest.TestCase):
    def test_missing_analytics_table_never_halts_trading(self):
        with TemporaryDirectory() as d:
            halts = []
            led = Ledger(BrokenRepo(), FallbackJournal(Path(d) / "j.jsonl"), halts.append)
            led.insert("candidate_evaluations", {"id": 1})
            self.assertEqual(halts, [])
            led.insert("trades", {"id": 1})          # a real audit table still halts
            self.assertEqual(len(halts), 1)

    def test_drawdown_tier_does_not_flap(self):
        cfg = ConfigManager().app.risk
        now = datetime(2026, 10, 8, 14, tzinfo=timezone.utc)
        t = DrawdownTracker(cfg, peak_equity=1000, day="2026-10-08", day_start_equity=905)
        self.assertIs(t.update(899, 899, now).state, RiskState.REDUCED)      # 10.1% drawdown
        self.assertIs(t.update(901, 901, now).state, RiskState.REDUCED)      # 9.9%: inside the band, stays
        self.assertIs(t.update(930, 930, now).state, RiskState.NORMAL)       # 7%: recovered

    def test_ensure_schema_expected_tables_listed(self):
        import re
        sql = (Path(__file__).parents[2] / "database" / "01_schema.sql").read_text(encoding="utf-8")
        tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql)
        for t in ("candidate_evaluations", "risk_reservations", "adaptive_param_history"):
            self.assertIn(t, tables)


if __name__ == "__main__":
    unittest.main()


class TestSpreadFixes(unittest.TestCase):
    def test_fixed_spread_symbol_is_not_ranked_100_percent(self):
        from app.market.ticks import RollingDistribution
        d = RollingDistribution(1000)
        for _ in range(500):
            d.add(100.0)
        self.assertAlmostEqual(d.percentile_of(100.0), 0.5)          # was 1.0 -> blocked all day
        for _ in range(5):
            d.add(129.0)
        self.assertGreater(d.percentile_of(129.0), 0.95)             # a real outlier is still caught

    def test_per_symbol_limits_from_config(self):
        cfg = ConfigManager().app.spread_gate
        self.assertEqual(cfg.symbol_max_spread_points["BTCUSDm"], 1000)
        self.assertEqual(cfg.symbol_max_spread_points["EURUSDm"], 20)
        self.assertNotIn("XAUUSDm", cfg.symbol_max_spread_points)

    def test_unsafe_spread_reason_names_the_cause(self):
        from tests.helpers import started_core
        core, *_ = started_core(overrides={"spread_gate.symbol_max_spread_points": {"XAUUSD": 0.001}})
        ok, why = core._spread_ok(core.last_tick, "LONDON")
        self.assertFalse(ok)
        self.assertIn("SPREAD_ABOVE_MAX", why)
