"""Unit tests: indicators, temporal integrity, sizing, cost/edge gate, exits, config safety."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from tests import helpers  # noqa: F401  (sets sys.path)
from app.alpha.exits import OpenTradeState, SimulatedTrade, improves, on_bar_close, simulate_bar
from app.broker.sim import synthetic_bars, xau_spec
from app.config import ConfigError, ConfigManager, CostCfg, RiskCfg
from app.core.types import Bar, Environment, EvidenceLevel, Side, TradePlan, Tick
from app.edge.cost import CostModel
from app.edge.expectancy import estimate_from_stats
from app.edge.gate import edge_gate
from app.features import indicators as ind
from app.features.frame import FeatureFrame
from app.governance.health import BotStatusMonitor
from app.market.bars import BarStore
from app.market.ticks import TickValidator
from app.risk.drawdown import DrawdownTracker
from app.risk.sizing import SizingInputs, size_position
from app.simulation import sim_config_dir


class TestBotStatusMonitor(unittest.TestCase):
    def test_tracks_pause_and_resume_durations(self):
        now = [100.0]
        monitor = BotStatusMonitor(wall=lambda: now[0])

        self.assertEqual(monitor.status()["downtime_seconds"], 0.0)
        monitor.update(True)
        now[0] += 10
        monitor.trigger_halt("MARGIN_LEVEL_LOW")
        now[0] += 15
        self.assertEqual(monitor.status()["downtime_seconds"], 15.0)

        transition = monitor.update(True)

        self.assertEqual(transition["duration_seconds"], 15.0)
        self.assertEqual(transition["halt_reason"], "MARGIN_LEVEL_LOW")
        self.assertEqual(monitor.status()["last_downtime_seconds"], 15.0)
        self.assertEqual(monitor.status()["total_downtime_seconds"], 15.0)
        self.assertIsNone(monitor.status()["halt_timestamp"])


class TestIndicators(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(1)
        self.c = 2000 + np.cumsum(rng.normal(0, 1, 600))
        self.h = self.c + rng.uniform(0, 1, 600)
        self.l = self.c - rng.uniform(0, 1, 600)

    def test_ema_matches_pandas_after_warmup(self):
        e = ind.ema(self.c, 20)
        ref = pd.Series(self.c).ewm(span=20, adjust=False).mean().values
        self.assertTrue(np.isnan(e[18]))
        self.assertAlmostEqual(e[-1], ref[-1], delta=1e-6 * abs(ref[-1]) + 1e-3)

    def test_rsi_bounds(self):
        r = ind.rsi(self.c, 14)
        v = r[~np.isnan(r)]
        self.assertTrue(((v >= 0) & (v <= 100)).all())

    def test_causality(self):
        """Changing FUTURE data must never change past indicator values."""
        c2 = self.c.copy()
        c2[400:] += 50
        for f in (lambda c: ind.ema(c, 50), lambda c: ind.rsi(c, 14), lambda c: ind.atr(self.h, self.l, c, 14)):
            a, b = f(self.c), f(c2)
            np.testing.assert_allclose(a[:400], b[:400], equal_nan=True)

    def test_live_window_matches_full_history(self):
        """With 5x warm-up, indicators on a live-sized window equal the backtest's full-history values."""
        rng = np.random.default_rng(9)
        c = 2000 + np.cumsum(rng.normal(0, 1, 5000))
        h, l = c + 0.5, c - 0.5
        n = 200
        tail = 5 * n + 100
        full, part = ind.ema(c, n)[-1], ind.ema(c[-tail:], n)[-1]
        self.assertLess(abs(full - part) / abs(full), 1e-4)
        a_full, a_part = ind.adx(h, l, c, 14)[0][-1], ind.adx(h[-tail:], l[-tail:], c[-tail:], 14)[0][-1]
        self.assertLess(abs(a_full - a_part), 1e-3)

    def test_adx_range(self):
        a, p, m = ind.adx(self.h, self.l, self.c, 14)
        v = a[~np.isnan(a)]
        self.assertTrue(len(v) > 100 and ((v >= 0) & (v <= 100)).all())

    def test_pivots_only_confirmed(self):
        ph, pl = ind.pivots(self.h, self.l, 3)
        j = ind.last_confirmed_pivot(pl, 300, 3)
        self.assertTrue(j <= 297)


class TestTemporal(unittest.TestCase):
    def bars(self, n, start=1_700_000_000 - 1_700_000_000 % 900):
        return [Bar(start + i * 900, 100, 101, 99, 100.5, 10, 20) for i in range(n)]

    def test_forming_bar_never_accepted(self):
        s = BarStore("X", "M15", confirm_buffer_sec=2)
        series = self.bars(5)
        server_now = series[-1].time + 300          # last bar is forming
        s.hydrate(series, server_now)
        self.assertEqual(s.last_time, series[-2].time)

    def test_confirmation_buffer(self):
        s = BarStore("X", "M15", confirm_buffer_sec=2)
        series = self.bars(3)
        # broker opened the next bar but server time is still inside the buffer
        s.hydrate(series, series[1].time + 900 + 1)
        self.assertEqual(s.last_time, series[0].time)
        s.poll(lambda n: series, series[1].time + 900 + 3)
        self.assertEqual(s.last_time, series[1].time)

    def test_corrupt_and_duplicate(self):
        s = BarStore("X", "M15")
        good = self.bars(4)
        bad = Bar(good[1].time, 100, 90, 99, 100, 1, 1)       # high < low
        s.hydrate([good[0], bad, good[2], good[3]], good[3].time + 2000)
        self.assertTrue(any(i.kind == "CORRUPT" for i in s.issues))
        before = len(s.bars)
        s.hydrate(good, good[3].time + 2000)                     # duplicates ignored
        self.assertGreaterEqual(len(s.bars), before)
        self.assertEqual(len({b.time for b in s.bars}), len(s.bars))

    def test_gap_widens_fetch(self):
        s = BarStore("X", "M15")
        allb = self.bars(40)
        s.hydrate(allb[:10], allb[9].time + 2000)
        calls = []

        def fetch(n):
            calls.append(n)
            return allb[-n:]
        s.poll(fetch, allb[-1].time + 300)
        self.assertGreater(max(calls), 5)
        self.assertEqual(s.last_time, allb[-2].time)


class TestTicks(unittest.TestCase):
    def test_validator(self):
        v = TickValidator(3.0)
        self.assertTrue(v.check(Tick(1000, 2000.0, 2000.2), 2.0).ok)
        self.assertEqual(v.check(Tick(1000, 2000.0, 2000.2), 2.0).reason, "DUPLICATE")
        self.assertEqual(v.check(Tick(900, 2000.0, 2000.2), 2.0).reason, "OUT_OF_ORDER")
        self.assertEqual(v.check(Tick(2000, 2000.3, 2000.1), 2.0).reason, "CROSSED_QUOTE")
        self.assertEqual(v.check(Tick(3000, 0.0, 1.0), 2.0).reason, "NON_POSITIVE_PRICE")
        self.assertEqual(v.check(Tick(4000, 2050.0, 2050.2), 2.0).reason, "PRICE_JUMP")


class TestSizing(unittest.TestCase):
    spec = xau_spec()

    def inp(self, equity, stop, mult=1.0, frac=0.01):
        return SizingInputs(equity=equity, free_margin=equity, used_margin=0.0, side=Side.BUY, entry=2400.0,
                            sl=2400.0 - stop, edge_risk_fraction=frac, risk_multiplier=mult, open_risk_money=0.0,
                            daily_loss_remaining=equity * 0.05)

    def margin(self, side, v, p):
        return v * 100 * p / 500

    def test_100_dollars_is_infeasible_and_never_rounds_up(self):
        r = size_position(self.spec, self.inp(100.0, 5.0), RiskCfg(), calc_margin=self.margin)
        self.assertFalse(r.feasible)
        self.assertEqual(r.reason, "POSITION_SIZE_INFEASIBLE")
        self.assertEqual(r.volume, 0.0)
        self.assertAlmostEqual(r.min_volume_risk, 5.0, places=6)   # 0.01 lot x 100 oz x $5

    def test_feasible_at_10k(self):
        r = size_position(self.spec, self.inp(10000.0, 5.0), RiskCfg(), calc_margin=self.margin)
        self.assertTrue(r.feasible)
        self.assertEqual(r.volume, 0.2)                          # $100 / ($5 x 100) = 0.2
        self.assertLessEqual(r.risk_money, 100.0 + 1e-9)

    def test_floor_to_step(self):
        r = size_position(self.spec, self.inp(10000.0, 3.0), RiskCfg(), calc_margin=self.margin)
        self.assertEqual(r.volume, 0.33)                         # 0.3333 -> 0.33, never 0.34

    def test_drawdown_multiplier_zero_blocks(self):
        r = size_position(self.spec, self.inp(10000.0, 5.0, mult=0.0), RiskCfg(), calc_margin=self.margin)
        self.assertFalse(r.feasible)
        self.assertEqual(r.reason, "RISK_BUDGET_EXHAUSTED")

    def test_margin_binding(self):
        r = size_position(self.spec, self.inp(10000.0, 0.5, frac=0.02), RiskCfg(max_risk_fraction_per_trade=0.015),
                          calc_margin=lambda s, v, p: v * 100 * p / 2)    # 1:2 leverage
        self.assertTrue(r.budgets["vol_by_margin"] is not None)
        self.assertLessEqual(r.volume, r.budgets["vol_by_margin"] + 1e-9)
        self.assertEqual(r.budgets["hard_cap"], 150.0)


class TestEdgeGate(unittest.TestCase):
    spec = xau_spec()

    def plan(self, stop):
        return TradePlan(Side.BUY, 2400.0, 2400.0 - stop, 2400 + 2 * stop, {}, "k")

    def test_unvalidated_blocked_in_real_allowed_in_demo(self):
        cost = CostModel(CostCfg()).estimate(self.plan(5.0), self.spec, 0.2)
        est = estimate_from_stats({}, 100)
        self.assertFalse(edge_gate(est, cost, CostCfg(), Environment.REAL).passed)
        g = edge_gate(est, cost, CostCfg(), Environment.DEMO)
        self.assertTrue(g.passed)
        self.assertEqual(g.reason, "UNVALIDATED_RESEARCH_TRADE")

    def test_costs_exceed_fraction(self):
        cost = CostModel(CostCfg(default_slippage_points=50)).estimate(self.plan(1.0), self.spec, 0.3)
        self.assertGreater(cost.cost_r, 0.25)
        self.assertFalse(edge_gate(estimate_from_stats({}, 100), cost, CostCfg(), Environment.DEMO).passed)

    def test_validated_below_buffer(self):
        cost = CostModel(CostCfg()).estimate(self.plan(5.0), self.spec, 0.2)
        est = estimate_from_stats({"n_trades": 20, "expectancy_r": 0.05, "oos_expectancy_r": 0.05, "win_rate": 0.4,
                                   "avg_win_r": 2, "avg_loss_r": -1, "evidence_level": "ROBUST"}, 100)
        g = edge_gate(est, cost, CostCfg(), Environment.REAL)
        self.assertFalse(g.passed)
        self.assertEqual(g.reason, "NET_EDGE_BELOW_BUFFER")

    def test_kelly_shrinks(self):
        small = estimate_from_stats({"n_trades": 30, "expectancy_r": 0.3, "win_rate": 0.45, "avg_win_r": 2,
                                     "avg_loss_r": -1, "evidence_level": "ROBUST"}, 100)
        big = estimate_from_stats({"n_trades": 3000, "expectancy_r": 0.3, "win_rate": 0.45, "avg_win_r": 2,
                                   "avg_loss_r": -1, "evidence_level": "ROBUST"}, 100)
        self.assertLess(small.kelly_fraction(), big.kelly_fraction())


class TestExits(unittest.TestCase):
    def st(self, side=Side.BUY):
        return OpenTradeState(side, 100.0, 99.0, 99.0, 102.0, 1.0, 1.0, 0)

    def test_stop_first_when_both_in_bar(self):
        st = self.st()
        ev = simulate_bar(st, {"type": "fixed"}, 100.0, 103.0, 98.5, 101.0, 0.1, 0.0)
        self.assertEqual(ev[0].kind, "SL")

    def test_gap_through_stop_fills_at_open(self):
        st = self.st()
        ev = simulate_bar(st, {"type": "fixed"}, 98.0, 98.5, 97.0, 98.2, 0.1, 0.0)
        self.assertEqual(ev[0].kind, "GAP_SL")
        self.assertEqual(ev[0].price, 98.0)

    def test_short_stop_uses_ask(self):
        st = OpenTradeState(Side.SELL, 100.0, 101.0, 101.0, 98.0, 1.0, 1.0, 0)
        # bid high 100.95 + spread 0.1 -> ask 101.05 >= stop
        ev = simulate_bar(st, {"type": "fixed"}, 100.0, 100.95, 99.5, 100.2, 0.1, 0.0)
        self.assertEqual(ev[0].kind, "SL")

    def test_trailing_only_tightens(self):
        st = self.st()
        self.assertTrue(improves(st, 99.5))
        self.assertFalse(improves(st, 98.5))

    def test_partial_then_runner(self):
        model = {"type": "fixed", "target_r": None, "partial": {"enabled": True, "at_r": 1.0, "fraction": 0.5,
                                                               "move_sl_to_be": True}}
        t = SimulatedTrade(Side.BUY, 100.0, 99.0, None, model, 0, 0.0, 0.0, max_bars=5)
        self.assertIsNone(t.step(100.0, 101.2, 99.8, 101.0, 0.0))      # partial at 101 -> SL to BE
        self.assertTrue(t.st.partial_done)
        self.assertEqual(t.st.sl, 100.0)
        res = t.step(101.0, 101.1, 99.9, 100.0, 0.0)                    # runner stopped at break-even
        self.assertAlmostEqual(res.r_multiple, 0.5)                     # 0.5 x 1R banked + 0.5 x 0R

    def test_time_stop(self):
        st = self.st()
        st.bars_held = 50
        ff = FeatureFrame([0, 900], [100, 100], [100, 100], [100, 100], [100, 100])
        d = on_bar_close(st, {"type": "fixed", "time_stop": {"enabled": True, "max_bars": 48, "min_r": 0.25}}, ff, 1,
                         "TREND_UP", 100.1)
        self.assertEqual(d.reason, "TIME_STOP")

    def test_thesis_invalidation(self):
        st = self.st()
        ff = FeatureFrame([0, 900], [100, 100], [100, 100], [100, 100], [100, 100])
        d = on_bar_close(st, {"type": "fixed", "thesis_invalidation": True}, ff, 1, "TREND_DOWN", 100.0)
        self.assertEqual(d.reason, "THESIS_INVALIDATED")


class TestDrawdown(unittest.TestCase):
    def test_states(self):
        from datetime import datetime, timezone
        d = DrawdownTracker(RiskCfg())
        now = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
        self.assertEqual(str(d.update(1000, 1000, now).state), "NORMAL")
        self.assertEqual(str(d.update(890, 890, now).state), "NO_NEW_TRADES")      # daily loss limit 5%
        d2 = DrawdownTracker(RiskCfg(), peak_equity=1000, day="2026-01-04", day_start_equity=1000)
        self.assertEqual(str(d2.update(880, 880, now).state), "REDUCED")              # 12% DD, new day
        self.assertEqual(str(d2.update(690, 690, now).state), "EMERGENCY")


class TestConfigSafety(unittest.TestCase):
    def setUp(self):
        self.d = sim_config_dir("DEMO")
        self.m = ConfigManager(self.d / "config.json", self.d / "strategies.json")

    def test_ceiling(self):
        with self.assertRaises(ConfigError):
            self.m.apply_change("risk.max_risk_fraction_per_trade", 0.5, "t", "too much")
        with self.assertRaises(ConfigError):
            self.m.apply_change("risk.max_risk_fraction_per_trade", 0.02, "t", "over the cap")
        self.assertEqual(self.m.app.risk.max_risk_fraction_per_trade, 0.015)

    def test_startup_rejects_risk_cap_above_one_point_five_percent(self):
        data = json.loads(self.m.config_path.read_text(encoding="utf-8"))
        data["risk"]["max_risk_fraction_per_trade"] = 0.02
        self.m.config_path.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaises(ConfigError):
            ConfigManager(self.m.config_path, self.m.strategies_path)

    def test_ten_concurrent_position_limit_is_runtime_editable(self):
        self.m.apply_change("risk.max_concurrent_positions", 10, "t", "multi asset portfolio")
        self.assertEqual(self.m.app.risk.max_concurrent_positions, 10)

    def test_not_runtime_editable(self):
        with self.assertRaises(ConfigError):
            self.m.apply_change("environment", "REAL", "t", "no")

    def test_config_rejects_real_mode_even_when_loaded_from_file(self):
        data = json.loads(self.m.config_path.read_text(encoding="utf-8"))
        data["environment"] = "REAL"
        self.m.config_path.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "REAL execution is disabled"):
            ConfigManager(self.m.config_path, self.m.strategies_path)

    def test_reason_required(self):
        with self.assertRaises(ConfigError):
            self.m.apply_change("risk.kelly_fraction", 0.2, "t", "")

    def test_versioned_and_persisted(self):
        rec = self.m.apply_change("risk.kelly_fraction", 0.2, "t", "research says so")
        self.assertEqual(rec["old_value"], 0.25)
        self.assertEqual(json.loads((self.d / "config.json").read_text(encoding="utf-8"))["risk"]["kelly_fraction"], 0.2)
        self.assertEqual(rec["config_version"], 2)

    def test_param_registry_violation(self):
        data = json.loads((self.d / "strategies.json").read_text(encoding="utf-8"))
        data["strategies"][0]["params"]["h1_adx_min"]["value"] = 99
        (self.d / "strategies.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ConfigError):
            ConfigManager(self.d / "config.json", self.d / "strategies.json")


if __name__ == "__main__":
    unittest.main()
