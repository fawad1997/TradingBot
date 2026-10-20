"""The five candidate rules as live strategies: they signal, they are recorded, and they cannot trade yet."""
import unittest

import numpy as np

from app.config import ConfigManager
from app.core.types import Environment, Side
from app.features.frame import FeatureFrame
from app.regime.router import Eligibility, route
from app.regime.detector import RegimeState
from app.research.lab.data import resample
from app.research.lab.hypotheses import HYPOTHESES
from app.strategies.base import StrategyView
from app.strategies.candidates import CANDIDATE_CLASSES
from app.strategies.registry import build, build_all
from tests.helpers import rows, started_core, trades

CANDIDATES = {"breakout_retest_v1": "breakout_retest", "momentum_trend_v1": "momentum_trend",
              "session_breakout_v1": "session_breakout", "vwap_reversion_v1": "vwap_reversion",
              "htf_continuation_v1": "htf_continuation"}


def market(n=40000, seed=11):
    rng = np.random.default_rng(seed)
    t = 1_700_000_000 // 3600 * 3600 + np.arange(n) * 60
    regime = np.repeat(rng.choice([1, -1, 0, 0], size=n // 1440 + 1), 1440)[:n]
    c = 2000 + np.cumsum(rng.normal(0, 0.5, n) + regime * 0.02)
    o = np.r_[c[0], c[:-1]]
    m1 = FeatureFrame(t, o, np.maximum(o, c) + 0.3, np.minimum(o, c) - 0.3, c, np.full(n, 20.0),
                      rng.integers(50, 200, n).astype(float), "M1")
    return resample(m1, 900)[0], resample(m1, 3600)[0]


def view(m15, i, h1, k, spread=0.24):
    mid = float(m15.c[i])
    return StrategyView(m15=m15, i=i, h1=h1, k=k, symbol="XAUUSDm", point=0.01, spread_price=spread,
                        bid=mid - spread / 2, ask=mid + spread / 2, spread_safe=True, session="LONDON",
                        regime_trend="UNKNOWN")


def h1_index(m15, h1):
    return np.searchsorted(np.asarray(h1.t, np.int64) + 3600, np.asarray(m15.t, np.int64) + 900, side="right") - 1


class TestRegistered(unittest.TestCase):
    def test_all_five_are_configured_and_buildable(self):
        cfgm = ConfigManager()
        built = build_all(cfgm.strategies.strategies)
        for sid in CANDIDATES:
            self.assertIn(sid, built, sid)
        self.assertEqual(set(CANDIDATE_CLASSES), {"BreakoutRetest", "MomentumTrend", "SessionBreakout",
                                                  "VwapReversion", "HtfContinuation"})

    def test_they_trade_on_demo_at_reduced_size(self):
        cfgm = ConfigManager()
        cfgs = [c for c in cfgm.strategies.strategies if c.id in CANDIDATES]
        self.assertEqual(len(cfgs), 5)
        for c in cfgs:
            self.assertEqual(c.status, "DEMO", c.id)
            self.assertTrue(c.enabled, c.id)
            self.assertEqual(c.risk_multiplier, 0.5, c.id)     # half size while they are unproven
        routed = route(cfgs, RegimeState(), Environment.DEMO, {}, True)
        for sid, rr in routed.items():
            self.assertTrue(rr.executable, sid)
            self.assertEqual(rr.risk_multiplier, 0.5, sid)

    def test_shadow_still_stops_them_sending_orders(self):
        """The way back: status SHADOW makes a candidate record-only again, with no other change."""
        cfgm = ConfigManager()
        cfgs = [c.model_copy(update={"status": "SHADOW"}) for c in cfgm.strategies.strategies
                if c.id in CANDIDATES]
        routed = route(cfgs, RegimeState(), Environment.DEMO, {}, True)
        for sid, rr in routed.items():
            self.assertEqual(rr.eligibility, Eligibility.RESEARCH_ONLY, sid)
            self.assertFalse(rr.executable, sid)
            self.assertTrue(rr.shadow, sid)


class TestSignals(unittest.TestCase):
    def test_live_strategy_matches_the_research_rule_bar_for_bar(self):
        """The property that makes the backtest meaningful: one implementation, two callers."""
        m15, h1 = market()
        cfgm = ConfigManager()
        idx = h1_index(m15, h1)
        for sid, rule in CANDIDATES.items():
            s = build(cfgm.strategy(sid))
            lab = HYPOTHESES[rule](m15, h1, {k: s.params[k] for k in s.rule_params if k in s.params})
            checked = 0
            for i in range(600, len(m15), 3):
                k = int(idx[i])
                if k < 31:
                    continue
                res = s.evaluate(view(m15, i, h1, k))
                live = 0 if res.plan is None else (1 if res.plan.direction is Side.BUY else -1)
                want = 1 if lab["long"][i] else (-1 if lab["short"][i] else 0)
                self.assertEqual(live, want, f"{sid} disagrees with its research rule at bar {i}")
                checked += 1
            self.assertGreater(checked, 400, sid)

    def test_each_rule_produces_setups_at_a_plausible_rate(self):
        m15, h1 = market()
        cfgm = ConfigManager()
        idx = h1_index(m15, h1)
        for sid in CANDIDATES:
            s = build(cfgm.strategy(sid))
            setups = 0
            for i in range(600, len(m15)):
                k = int(idx[i])
                if k >= 31 and s.evaluate(view(m15, i, h1, k)).plan is not None:
                    setups += 1
            weeks = (len(m15) - 600) * 900 / 604800.0
            self.assertGreater(setups, 0, f"{sid} never fires")
            self.assertLess(setups / weeks, 400, f"{sid} fires implausibly often")

    def test_plan_geometry_and_the_spread_floor(self):
        m15, h1 = market()
        cfgm = ConfigManager()
        idx = h1_index(m15, h1)
        s = build(cfgm.strategy("vwap_reversion_v1"))
        wide = None
        for i in range(600, len(m15)):
            k = int(idx[i])
            if k < 31:
                continue
            res = s.evaluate(view(m15, i, h1, k))
            if res.plan is None:
                continue
            p = res.plan
            stop = abs(p.entry - p.sl)
            reward = abs(p.tp - p.entry)
            self.assertAlmostEqual(reward / stop, s.params["reward_risk"], places=6)
            if p.direction is Side.BUY:
                self.assertLess(p.sl, p.entry)
                self.assertGreater(p.tp, p.entry)
            else:
                self.assertGreater(p.sl, p.entry)
                self.assertLess(p.tp, p.entry)
            self.assertGreaterEqual(stop, 10.0 * 0.24 - 1e-9)      # never closer than 10 spreads
            if wide is None:
                wide = s.evaluate(view(m15, i, h1, k, spread=5.0))
            if wide is not None and wide.plan is not None:
                self.assertGreaterEqual(abs(wide.plan.entry - wide.plan.sl), 10.0 * 5.0 - 1e-6)
            return
        self.fail("no setup produced")

    def test_a_broken_rule_is_reported_not_raised(self):
        cfgm = ConfigManager()
        s = build(cfgm.strategy("breakout_retest_v1"))
        m15, h1 = market(40000)
        s.rule = lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
        res = s.evaluate(view(m15, 800, h1, 100))
        self.assertIsNone(res.plan)
        self.assertTrue(res.reasons[0].startswith("RULE_ERROR"))

    def test_insufficient_history_is_named(self):
        cfgm = ConfigManager()
        m15, h1 = market(40000)
        self.assertEqual(build(cfgm.strategy("momentum_trend_v1")).evaluate(view(m15, 10, h1, 5)).reasons,
                         ["INSUFFICIENT_HISTORY"])


class TestLiveEngineRecordsButDoesNotTrade(unittest.TestCase):
    def test_a_shadow_candidate_is_recorded_and_sends_no_order(self):
        from tests.helpers import make_signal
        from app.regime.router import RouteResult
        core, *_ = started_core()
        sig = make_signal(core)
        sig.strategy_id = "breakout_retest_v1"
        shadow = RouteResult("breakout_retest_v1", Eligibility.RESEARCH_ONLY, "status SHADOW not executable",
                             False, True)
        before = len(trades(core))
        core._process_signal(sig, shadow, core.cfgm.strategy("breakout_retest_v1"), core.last_tick)
        self.assertEqual(len(trades(core)), before)                       # no order, ever
        recorded = rows(core, "signals", id=sig.id)
        self.assertTrue(recorded and recorded[-1]["shadow"], "the signal must still be recorded")


if __name__ == "__main__":
    unittest.main()


class TestScoreboard(unittest.TestCase):
    def test_shadow_strategies_are_ranked_on_their_followed_signals(self):
        from app.dashboard.analysis import build_scoreboard
        trades = [{"strategy_id": "micro_scalper", "r_multiple": -1.0} for _ in range(40)]
        cfs = ([{"strategy_id": "breakout_retest_v1", "status": "RESOLVED", "hypothetical_r": 0.6}
                for _ in range(35)]
               + [{"strategy_id": "vwap_reversion_v1", "status": "RESOLVED", "hypothetical_r": -0.4}
                  for _ in range(35)]
               + [{"strategy_id": "session_breakout_v1", "status": "OPEN", "hypothetical_r": None}
                  for _ in range(5)])
        rows = build_scoreboard(trades, cfs, {"micro_scalper": "DEMO", "breakout_retest_v1": "SHADOW",
                                              "vwap_reversion_v1": "SHADOW", "session_breakout_v1": "SHADOW"})
        by = {r["strategy"]: r for r in rows}
        self.assertEqual(rows[0]["strategy"], "breakout_retest_v1")       # best measured rule first
        self.assertEqual(by["breakout_retest_v1"]["shadow"]["n"], 35)
        self.assertEqual(by["breakout_retest_v1"]["evidence"], "shadow")
        self.assertEqual(by["micro_scalper"]["live"]["n"], 40)
        self.assertEqual(by["session_breakout_v1"]["evidence"], "too few")  # 5 signals, none resolved
        self.assertEqual(by["session_breakout_v1"]["shadow_signals"], 5)

    def test_endpoint_serves_the_scoreboard(self):
        from starlette.testclient import TestClient
        from app.core.bridge import Bridge
        from app.dashboard.api import create_app
        from app.storage.repo import MemoryRepo
        repo = MemoryRepo()
        r = TestClient(create_app(Bridge(), repo, ConfigManager(), None)).get("/api/analysis/scoreboard")
        self.assertEqual(r.status_code, 200)
        self.assertIn("rows", r.json())

    def test_a_strategy_with_no_data_is_listed_not_hidden(self):
        """The card claims to list every strategy. A rule that has not fired must appear as 'no signals yet'."""
        from app.dashboard.analysis import build_scoreboard
        statuses = {"micro_scalper": "DEMO", "breakout_retest_v1": "DEMO", "vwap_reversion_v1": "DEMO"}
        rows = build_scoreboard([{"strategy_id": "micro_scalper", "r_multiple": -1.0}], [], statuses)
        by = {r["strategy"]: r for r in rows}
        self.assertEqual(set(by), set(statuses), "every configured strategy needs a row")
        self.assertEqual(by["breakout_retest_v1"]["evidence"], "no signals yet")
        self.assertEqual(by["breakout_retest_v1"]["live"]["n"], 0)
        self.assertEqual(by["breakout_retest_v1"]["shadow_signals"], 0)
        self.assertEqual(by["micro_scalper"]["evidence"], "too few")   # has 1 trade: too few, not absent
