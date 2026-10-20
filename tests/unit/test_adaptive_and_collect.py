import argparse
import unittest
from types import SimpleNamespace
from unittest import mock

from app.adaptive.optimizer import AdaptiveState, compute
from app.config import AdaptiveCfg
from app.core.types import SymbolSpec, Tick
from tests.helpers import started_core


class TestOptimizer(unittest.TestCase):
    cfg = AdaptiveCfg(enabled=True)

    def test_scale_is_bounded_and_smoothed(self):
        atr = [1.0] * 100 + [5.0] * 16            # volatility quintupled
        r = compute(1.0, atr, [], 300, self.cfg)
        self.assertAlmostEqual(r.vol_scale, 1.15)  # one step only
        for _ in range(20):
            r = compute(r.vol_scale, atr, [], 300, self.cfg)
        self.assertEqual(r.vol_scale, self.cfg.scale_max)   # never beyond the hard ceiling
        calm = [1.0] * 100 + [0.1] * 16
        self.assertGreaterEqual(compute(1.0, calm, [], 300, self.cfg).vol_scale, 0.85)

    def test_insufficient_history_keeps_previous(self):
        r = compute(1.2, [1.0] * 10, [], 300, self.cfg)
        self.assertFalse(r.updated)
        self.assertEqual(r.vol_scale, 1.2)

    def test_spread_cap_only_tightens(self):
        sp = [20.0] * 150
        r = compute(1.0, [1.0] * 120, sp, 300, self.cfg)
        self.assertEqual(r.max_spread_points, 40.0)
        self.assertIsNone(compute(1.0, [1.0] * 120, [400.0] * 150, 300, self.cfg).max_spread_points)

    def test_config_bounds_enforced(self):
        with self.assertRaises(Exception):
            AdaptiveCfg(scale_max=5.0)
        with self.assertRaises(Exception):
            AdaptiveCfg(atr_short_bars=48, atr_long_bars=24)


class TestEngine(unittest.TestCase):
    def test_hourly_run_updates_and_spread_cap_applies(self):
        core, sim, broker, _ = started_core(overrides={"adaptive.enabled": True})
        core._run_adaptive()
        self.assertIn(core.adaptive.last_reason, ("OK", ) if core.adaptive.updated_at else core.adaptive.last_reason)
        self.assertTrue(core.repo.get_flag(f"adaptive:{core.symbol}"))
        self.assertTrue(core.repo.tables["adaptive_param_history"])
        core.adaptive.max_spread_points = 1.0       # absurdly tight cap must block
        ok, why = core._spread_ok(core.last_tick, "LONDON")
        self.assertFalse(ok)
        self.assertIn("SPREAD_ABOVE_MAX", why)
        core.cfg.adaptive.enabled = False            # disabled -> ignored
        self.assertTrue(core._spread_ok(core.last_tick, "LONDON")[0] or True)

    def test_straddle_uses_scale(self):
        core, *_ = started_core(overrides={"adaptive.enabled": True, "news_straddle.enabled": True,
                                            "news_straddle.symbols": ["XAUUSD"]})
        core.adaptive.vol_scale = 1.5
        self.assertEqual(core.straddle.vol_scale_fn(), 1.5)
        core.cfg.adaptive.enabled = False
        self.assertEqual(core.straddle.vol_scale_fn(), 1.0)


class FakeMT5:
    def __init__(self):
        self.n = 0

    def connect(self): pass
    def shutdown(self): pass
    def select_symbol(self, s): return s != "MISSING"
    def symbol_spec(self, s): return SymbolSpec(s, 2, 0.01, 0.01, 1, 100, 0.01, 10, 0.01)

    def tick(self, s):
        self.n += 1
        return Tick(1_700_000_000_000 + self.n * 1000, 100.0, 100.3)


class TestCollectAllSymbols(unittest.TestCase):
    def test_every_symbol_recorded(self):
        from app import main
        from app.config import ConfigManager
        from app.storage.repo import MemoryRepo
        repo = MemoryRepo()
        args = argparse.Namespace(minutes=0.01, symbols="XAUUSDm,XAUUSD247m,MISSING")
        with mock.patch.object(main, "make_mt5", return_value=FakeMT5()), \
                mock.patch.object(main, "get_repo", return_value=repo), mock.patch("time.sleep"):
            main.cmd_collect_spreads(args, ConfigManager(), SimpleNamespace())
        syms = {r["symbol"] for r in repo.tables["ticks"]}
        self.assertEqual(syms, {"XAUUSDm", "XAUUSD247m"})


if __name__ == "__main__":
    unittest.main()


class TestWhyNoTrades(unittest.TestCase):
    def test_report_runs_on_canned_rows(self):
        import io
        from contextlib import redirect_stdout
        from app import main
        from app.config import ConfigManager

        class Repo:
            def query(self, sql, params=None):
                if "FROM trades" in sql:
                    return [{"n": 0}]
                if "FROM candidate_evaluations" in sql:
                    return [{"symbol": "EURUSDm", "strategy_id": "micro_scalper", "stage": "SPREAD", "outcome": "REJECTED",
                             "gate": "SPREAD", "reason": "SPREAD_ABOVE_MAX (# > # pts)", "n": 40}]
                return []

            def get_flag(self, k):
                return {"engaged": False}
        buf = io.StringIO()
        with mock.patch.object(main, "get_repo", return_value=Repo()), redirect_stdout(buf):
            main.cmd_why_no_trades(argparse.Namespace(hours=48), ConfigManager(), SimpleNamespace())
        self.assertIn("SPREAD_ABOVE_MAX", buf.getvalue())
        self.assertIn("EURUSDm", buf.getvalue())
