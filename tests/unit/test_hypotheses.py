"""The five candidate entry rules: each fires on the shape it claims, never looks ahead, and never trades live."""
import unittest
from types import SimpleNamespace

import numpy as np

from app.config import ResearchLabCfg
from app.features.frame import FeatureFrame
from app.research.lab.data import resample
from app.research.lab.hypotheses import (HYPOTHESES, breakout_retest, htf_continuation, momentum_trend,
                                         prior_high, prior_low, session_breakout, vwap_reversion)


def frame(o, h, l, c, start=1_700_000_000 // 86400 * 86400, step=60, vol=None):
    n = len(c)
    t = start + np.arange(n) * step
    return FeatureFrame(t, o, h, l, c, np.full(n, 20.0),
                        np.full(n, 100.0) if vol is None else np.asarray(vol, float), f"{step // 60}m")


def from_closes(c, start=1_700_000_000 // 86400 * 86400, step=60, wick=0.2, vol=None):
    c = np.asarray(c, float)
    o = np.r_[c[0], c[:-1]]
    return frame(o, np.maximum(o, c) + wick, np.minimum(o, c) - wick, c, start, step, vol)


def noise(n=6000, seed=1, step=60):
    rng = np.random.default_rng(seed)
    return from_closes(2000 + np.cumsum(rng.normal(0, 0.5, n)), step=step)


class TestHelpers(unittest.TestCase):
    def test_prior_window_excludes_the_current_bar(self):
        f = from_closes([1, 2, 3, 4, 5], wick=0.0)
        ph, pl = prior_high(f, 2), prior_low(f, 2)
        self.assertTrue(np.isnan(ph[1]))
        self.assertEqual(ph[2], max(f.h[0], f.h[1]))       # never includes bar 2 itself
        self.assertEqual(pl[4], min(f.l[2], f.l[3]))


class TestRulesFireOnTheirShape(unittest.TestCase):
    def test_breakout_retest_needs_a_break_then_a_hold(self):
        base = [2000 + (i % 2) * 0.5 for i in range(60)]          # tight range
        broke = base + [2006] + [2004.2] * 3 + [2005.5]           # break up, pull back, close back up
        sig = breakout_retest(from_closes(broke), None, {"lookback": 40, "wait": 12, "tol_atr": 3.0})
        self.assertTrue(sig["long"].any())
        self.assertFalse(sig["short"].any())
        self.assertFalse(breakout_retest(from_closes(base + [2000.4] * 5), None, {"lookback": 40})["long"].any())

    def test_breakout_retest_gives_up_when_the_break_fails(self):
        base = [2000 + (i % 2) * 0.5 for i in range(60)]
        failed = base + [2006] + [1990] * 6 + [2005]              # breaks, then collapses through the level
        sig = breakout_retest(from_closes(failed), None, {"lookback": 40, "wait": 12, "tol_atr": 0.25})
        self.assertFalse(sig["long"].any())

    def test_momentum_trend_fires_on_a_pullback_inside_a_trend(self):
        up = list(2000 + np.arange(200) * 0.8)
        up[-2] = up[-3] - 12.0                                     # dip below the fast EMA, then close back above
        sig = momentum_trend(from_closes(up), None, {"min_mom_atr": 0.1})
        self.assertTrue(sig["long"].any())
        self.assertFalse(momentum_trend(noise(2000), None, {"min_mom_atr": 6.0})["long"].any())

    def test_session_breakout_trades_once_a_side_a_day_inside_its_window(self):
        n = 1440                                                   # one UTC day of M1
        c = np.full(n, 2000.0)
        c[:420] += np.tile([0.0, 1.0], 210)                        # 00:00-07:00 range of about 1.0
        c[430:] = 2050.0                                           # breaks far above during London
        f = from_closes(c)
        sig = session_breakout(f, None, {"min_range_atr": 0.0})
        hours = (np.asarray(f.t) % 86400) / 3600
        self.assertEqual(int(sig["long"].sum()), 1)
        self.assertTrue(7.0 <= hours[int(np.flatnonzero(sig["long"])[0])] < 13.0)
        self.assertFalse(sig["short"].any())

    def test_session_breakout_ignores_a_range_that_is_too_tight(self):
        c = np.full(1440, 2000.0)
        c[430:] = 2050.0
        self.assertFalse(session_breakout(from_closes(c), None, {"min_range_atr": 50.0})["long"].any())

    def test_vwap_reversion_fades_a_stretch_that_turns(self):
        c = np.r_[np.full(200, 2000.0), np.linspace(2000, 1960, 60), [1962.0]]
        sig = vwap_reversion(from_closes(c), None, {"min_dev_atr": 1.0})
        self.assertTrue(sig["long"][len(c) - 1])                   # far below VWAP and closing back up
        self.assertFalse(sig["short"].any())

    def test_htf_continuation_follows_the_higher_timeframe(self):
        rng = np.random.default_rng(3)
        n = 6000
        c = 2000 + np.arange(n) * 0.05 + rng.normal(0, 0.6, n)     # steady uptrend with noise
        m1 = from_closes(c)
        htf = resample(m1, 3600)[0]
        sig = htf_continuation(m1, htf, {"min_slope_atr": 0.0})
        self.assertGreater(int(sig["long"].sum()), int(sig["short"].sum()))
        self.assertFalse(htf_continuation(m1, None, None)["long"].any())      # no higher timeframe: no signals


class TestNoLookahead(unittest.TestCase):
    def test_future_bars_cannot_change_an_earlier_signal(self):
        rng = np.random.default_rng(5)
        c = 2000 + np.cumsum(rng.normal(0, 0.5, 4000))
        full = from_closes(c)
        cut = 3000
        part = from_closes(c[:cut])
        for name, fn in HYPOTHESES.items():
            htf_full = resample(full, 3600)[0]
            htf_part = resample(part, 3600)[0]
            a = fn(full, htf_full, None)
            b = fn(part, htf_part, None)
            # the last higher-timeframe bar of the cut series is still forming in the full one, so compare before it
            end = cut - 120
            for side in ("long", "short"):
                self.assertTrue(np.array_equal(a[side][:end], b[side][:end]), f"{name} {side} looks ahead")


class TestLabIntegration(unittest.TestCase):
    def _frames(self, n=20000, seed=2):
        m1 = noise(n, seed)
        m15 = resample(m1, 900)[0]
        frames, spreads = {"M1": m1, "HTF_M1": m15}, {"M1": np.full(n, 0.2)}
        for name, secs in (("M5", 300), ("M15", 900)):
            trig, sp = resample(m1, secs, np.full(n, 0.2))
            frames[name], spreads[name] = trig, sp
            frames["HTF_" + name] = resample(m15, 3600)[0]
        return frames, spreads

    def test_every_rule_is_scored_on_every_configured_timeframe(self):
        from app.research.lab.worker import _hypotheses
        frames, spreads = self._frames()
        cfg = ResearchLabCfg(optuna_enabled=False, hypothesis_timeframes=["M5", "M15"])
        out = _hypotheses(frames, cfg, spreads, 0.0, 10.0)
        self.assertEqual(set(out), set(HYPOTHESES))
        for name, per in out.items():
            self.assertEqual(set(per), {"M5", "M15"}, name)
            for tf, e in per.items():
                self.assertIn("verdict", e, f"{name} {tf}")

    def test_an_unknown_rule_is_reported_not_raised(self):
        from app.research.lab.worker import _hypotheses
        frames, spreads = self._frames(6000)
        out = _hypotheses(frames, ResearchLabCfg(optuna_enabled=False, hypotheses=["nope"]), spreads, 0.0, 10.0)
        self.assertIn("error", out["nope"])

    def test_results_survive_the_compact_summary(self):
        from app.research.lab.store import _compact_symbol
        from app.research.lab.worker import _hypotheses
        frames, spreads = self._frames(6000)
        cfg = ResearchLabCfg(optuna_enabled=False)
        compact = _compact_symbol({"verdict": "X", "hypotheses": _hypotheses(frames, cfg, spreads, 0.0, 10.0)})
        self.assertEqual(set(compact["hypotheses"]), set(HYPOTHESES))

    def test_candidate_rules_are_not_live_strategies(self):
        from app.config import ConfigManager
        live = {s.id for s in ConfigManager().strategies.strategies}
        self.assertFalse(live & set(HYPOTHESES))


if __name__ == "__main__":
    unittest.main()


class TestStrategyFunnel(unittest.TestCase):
    """The funnel must reproduce the live strategy's own verdicts, bar for bar."""

    def _frames(self, n=40000, seed=11):
        rng = np.random.default_rng(seed)
        t = 1_700_000_000 // 3600 * 3600 + np.arange(n) * 60
        regime = np.repeat(rng.choice([1, -1, 0, 0], size=n // 1440 + 1), 1440)[:n]
        c = 2000 + np.cumsum(rng.normal(0, 0.5, n) + regime * 0.02)
        o = np.r_[c[0], c[:-1]]
        m1 = FeatureFrame(t, o, np.maximum(o, c) + 0.3, np.minimum(o, c) - 0.3, c, np.full(n, 20.0),
                          rng.integers(50, 200, n).astype(float), "M1")
        return resample(m1, 900)[0], resample(m1, 3600)[0]

    def test_funnel_counts_match_the_strategy_itself(self):
        from app.config import ConfigManager
        from app.research.lab.funnel import ORDER, replay
        from app.strategies.registry import build
        m15, h1 = self._frames()
        cfgm = ConfigManager()
        for sid in ("pullback_v1", "mean_reversion_v1"):
            cfg = cfgm.strategy(sid)
            out = replay(build(cfg), m15, h1, 0.01, 0.24, "XAUUSDm")
            total = sum(out["reasons"].values())
            if sid == "mean_reversion_v1":
                self.assertEqual(total, out["evaluated"])        # one reason per bar: the funnel loses nothing
            else:
                self.assertGreaterEqual(total, out["evaluated"])  # pullback can report several reasons at once
            self.assertLessEqual(out["setups"], out["evaluated"])
            self.assertTrue(set(out["reasons"]) <= set(ORDER[sid]) | {"ERROR", "NO_REASON"}, out["reasons"])
            self.assertEqual(out["reasons"].get("SETUP", 0), out["setups"])

    def test_clause_rates_explain_the_conjunction(self):
        from app.config import ConfigManager
        from app.research.lab.funnel import h1_conditions
        _, h1 = self._frames()
        cfgm = ConfigManager()
        cond = h1_conditions("pullback_v1", cfgm.strategy("pullback_v1").param_values(), h1)
        self.assertEqual(set(cond) - {"all_clauses"},
                         {"ema_aligned", "close_beyond_ema", "slope>=min", "adx>=min", "di_aligned"})
        for name, c in cond.items():
            if name != "all_clauses":
                # the conjunction can never pass more often than its weakest clause
                self.assertLessEqual(cond["all_clauses"]["rate"], c["rate"] + 1e-9, name)

    def test_diagnose_covers_every_m15_strategy_and_skips_the_scalper(self):
        """Every configured strategy except micro_scalper must appear.

        This previously asserted exactly {pullback_v1, mean_reversion_v1}, which locked in a bug: diagnose()
        skips any strategy absent from funnel.ORDER, and the five candidate rules were absent, so the card that
        explains why a strategy does not fire excluded the five strategies most in need of explaining.
        micro_scalper is M1 and is covered by the timeframe study instead.
        """
        from app.config import ConfigManager
        from app.research.lab.funnel import diagnose
        cfgm = ConfigManager()
        m15, h1 = self._frames(20000)
        out = diagnose(cfgm, m15, h1, 0.01, 0.24, "XAUUSDm")
        expected = {s.id for s in cfgm.strategies.strategies} - {"micro_scalper"}
        self.assertEqual(set(out), expected)
        for sid, e in out.items():
            self.assertNotIn("error", e, f"{sid} raised during replay: {e.get('error')}")
            self.assertIn("biggest_blocker", e, sid)
            self.assertIn("setups_per_week", e, sid)


class TestRegimeGateAndThresholds(unittest.TestCase):
    def _frames(self, n=40000, seed=11):
        rng = np.random.default_rng(seed)
        t = 1_700_000_000 // 3600 * 3600 + np.arange(n) * 60
        regime = np.repeat(rng.choice([1, -1, 0, 0], size=n // 1440 + 1), 1440)[:n]
        c = 2000 + np.cumsum(rng.normal(0, 0.5, n) + regime * 0.02)
        o = np.r_[c[0], c[:-1]]
        m1 = FeatureFrame(t, o, np.maximum(o, c) + 0.3, np.minimum(o, c) - 0.3, c, np.full(n, 20.0),
                          rng.integers(50, 200, n).astype(float), "M1")
        return resample(m1, 900)[0], resample(m1, 3600)[0]

    def test_regime_mix_sums_to_one_and_scores_the_allow_list(self):
        from app.config import ConfigManager
        from app.research.lab.funnel import diagnose, regime_mix
        m15, h1 = self._frames()
        mix = regime_mix(h1)
        self.assertAlmostEqual(sum(mix.values()), 1.0, places=2)
        out = diagnose(ConfigManager(), m15, h1, 0.01, 0.24, "XAUUSDm")
        pb = out["pullback_v1"]
        self.assertEqual(pb["allowed_regimes"], ["TREND_UP", "TREND_DOWN"])
        # the share the router allows is exactly the mix restricted to the allow-list
        self.assertAlmostEqual(pb["regime_allowed_share"],
                               mix.get("TREND_UP", 0) + mix.get("TREND_DOWN", 0), places=4)
        self.assertAlmostEqual(out["mean_reversion_v1"]["regime_allowed_share"], mix.get("RANGE", 0), places=4)

    def test_threshold_sweep_reports_setups_and_edge_for_each_value(self):
        from app.config import ConfigManager
        from app.research.lab.thresholds import SWEEPS, sweep
        m15, h1 = self._frames()
        out = sweep(ConfigManager(), m15, h1, 0.01, 0.24, 0.0, "XAUUSDm", 0.7, ["mean_reversion_v1"])
        self.assertEqual(set(out), {"mean_reversion_v1"})
        per = out["mean_reversion_v1"]["h1_adx_max"]
        self.assertEqual(per["configured"], 18)
        values = [r["value"] for r in per["values"]]
        self.assertEqual(values, SWEEPS["mean_reversion_v1"]["h1_adx_max"])
        counts = {r["value"]: r["setups"] for r in per["values"]}
        # a looser ADX ceiling can only admit more setups, never fewer
        self.assertLessEqual(counts[14], counts[18])
        self.assertLessEqual(counts[18], counts[30])
        for r in per["values"]:
            self.assertIn("expectancy_r", r["test"])
            self.assertGreaterEqual(r["setups_per_week"], 0)

    def test_sweep_never_changes_the_live_config(self):
        from app.config import ConfigManager
        from app.research.lab.thresholds import sweep
        m15, h1 = self._frames(20000)
        cfgm = ConfigManager()
        before = cfgm.strategy("mean_reversion_v1").param_values()["h1_adx_max"]
        sweep(cfgm, m15, h1, 0.01, 0.24, 0.0, "X", 0.7, ["mean_reversion_v1"])
        self.assertEqual(cfgm.strategy("mean_reversion_v1").param_values()["h1_adx_max"], before)


class TestPerStrategyRisk(unittest.TestCase):
    def test_candidates_trade_at_reduced_size(self):
        from app.config import ConfigManager
        from app.core.types import Environment
        from app.regime.detector import RegimeState, TrendRegime
        from app.regime.router import Eligibility, route
        cfgm = ConfigManager()
        routed = route(cfgm.strategies.strategies, RegimeState(trend=TrendRegime.TREND_UP),
                       Environment.DEMO, {}, True)
        # micro_scalper was 1.0 here until 15 Oct. It is 38 of 45 losses and -$378 of -$424 over seven days,
        # so full size on the measured worst performer was the opposite of what the evidence supports. The
        # router must carry whatever the config says, so this asserts the routed value tracks the config
        # rather than a number frozen into the test.
        by_cfg = {s.id: s for s in cfgm.strategies.strategies}
        # micro_scalper was DISABLED on 19 Oct 2026 (198 live trades at -0.472R, 2,642 shadow signals at
        # -1.023R). A disabled strategy routes as DISABLED and carries no size at all, so the
        # multiplier-tracks-config property below applies only while it is enabled. Assert the thing that
        # actually matters either way: it cannot execute.
        if not by_cfg["micro_scalper"].enabled:
            self.assertEqual(routed["micro_scalper"].eligibility, Eligibility.DISABLED)
            self.assertFalse(routed["micro_scalper"].executable,
                             "a disabled strategy must not be able to send orders")
            self.assertFalse(routed["micro_scalper"].shadow,
                             "nor be evaluated as a counterfactual")
        else:
            self.assertEqual(routed["micro_scalper"].risk_multiplier,
                             by_cfg["micro_scalper"].risk_multiplier)
            self.assertLessEqual(routed["micro_scalper"].risk_multiplier, 0.5)
        for sid in ("breakout_retest_v1", "momentum_trend_v1", "session_breakout_v1", "vwap_reversion_v1",
                    "htf_continuation_v1"):
            self.assertEqual(routed[sid].eligibility, Eligibility.ELIGIBLE, sid)
            self.assertTrue(routed[sid].executable, sid)
            self.assertEqual(routed[sid].risk_multiplier, 0.5, sid)
