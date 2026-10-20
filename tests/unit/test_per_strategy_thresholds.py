"""Each strategy is judged on its own bar, not a shared one.

The flaw this file defends against: a single `gate_min_trades` and a single `gate_block_below_r` applied
to all eight strategies. They are not comparable. On identical history a breakout strategy produced 34
training trades while mean reversion produced 5, so one bar of 30 judges the first and leaves the second
permanently unjudged. And -0.3R is a catastrophe for a scalper taking many small trades while being
ordinary variation for a strategy taking few large ones.
"""
import datetime as dt
import unittest
from datetime import datetime, timedelta, timezone

from app.config import ResearchLabCfg, StrategyGateCfg
from app.research.lab.gate import Thresholds, evaluate
from app.research.lab.strategy_research import min_trades_for
from tools.gate_thresholds import collect, recommend

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(hours=1)).isoformat()


def entry(sid, tn, tr, vn, vr, verdict="NOT_ROBUST_ON_TEST"):
    return {"strategy_id": sid, "verdict": verdict,
            "train": {"n_trades": tn, "expectancy_r": tr},
            "test": {"n_trades": vn, "expectancy_r": vr}}


class TestThresholdResolution(unittest.TestCase):
    def test_no_override_means_the_global_value(self):
        t = Thresholds(ResearchLabCfg(), "pullback_v1")
        self.assertEqual(t.min_trades, ResearchLabCfg().gate_min_trades)
        self.assertEqual(t.block_below_r, ResearchLabCfg().gate_block_below_r)
        self.assertEqual(t.source, "global default")

    def test_an_override_wins(self):
        cfg = ResearchLabCfg(gate_overrides={"x": StrategyGateCfg(min_trades=12, block_below_r=-0.8)})
        t = Thresholds(cfg, "x")
        self.assertEqual(t.min_trades, 12)
        self.assertEqual(t.block_below_r, -0.8)
        self.assertEqual(t.source, "per-strategy override")

    def test_a_partial_override_falls_back_field_by_field(self):
        """Setting one field must not silently reset the others."""
        cfg = ResearchLabCfg(gate_overrides={"x": StrategyGateCfg(min_trades=12)})
        t = Thresholds(cfg, "x")
        self.assertEqual(t.min_trades, 12)
        self.assertEqual(t.block_below_r, ResearchLabCfg().gate_block_below_r)

    def test_an_override_for_another_strategy_does_not_leak(self):
        cfg = ResearchLabCfg(gate_overrides={"a": StrategyGateCfg(min_trades=5)})
        self.assertEqual(Thresholds(cfg, "b").min_trades, ResearchLabCfg().gate_min_trades)

    def test_no_strategy_id_falls_back_to_globals(self):
        cfg = ResearchLabCfg(gate_overrides={"a": StrategyGateCfg(min_trades=5)})
        self.assertEqual(Thresholds(cfg, None).min_trades, ResearchLabCfg().gate_min_trades)

    def test_require_evidence_can_be_set_per_strategy(self):
        """A strategy you trust less can fail closed while the rest stay open."""
        cfg = ResearchLabCfg(gate_overrides={"risky": StrategyGateCfg(require_evidence=True)})
        self.assertTrue(Thresholds(cfg, "risky").require_evidence)
        self.assertFalse(Thresholds(cfg, "other").require_evidence)


class TestTheSameNumbersAreJudgedDifferently(unittest.TestCase):
    """The whole point: identical research, different verdicts, because the strategies differ."""

    def setUp(self):
        self.cfg = ResearchLabCfg(gate_overrides={
            "scalper": StrategyGateCfg(min_trades=100, block_below_r=-0.15),
            "slow": StrategyGateCfg(min_trades=12, block_below_r=-0.8),
        })

    def test_three_strategies_three_outcomes_from_one_result(self):
        made = {}
        for sid in ("scalper", "slow", "ordinary"):
            made[sid] = evaluate(entry(sid, 25, -0.5, 15, -0.5), FRESH, self.cfg, NOW)
        self.assertEqual(made["scalper"]["evidence"], "THIN",
                         "40 trades is below the scalper's own 100-trade bar")
        self.assertFalse(made["slow"]["blocked"],
                         "-0.5R is inside the slow strategy's own -0.8R tolerance")
        self.assertTrue(made["ordinary"]["blocked"],
                        "-0.5R is beyond the global -0.3R limit")

    def test_every_result_reports_the_bar_that_produced_it(self):
        out = evaluate(entry("slow", 25, -0.5, 15, -0.5), FRESH, self.cfg, NOW)
        self.assertEqual(out["thresholds"]["block_below_r"], -0.8)
        self.assertEqual(out["thresholds"]["source"], "per-strategy override")

    def test_the_explanation_quotes_the_strategys_own_limit(self):
        out = evaluate(entry("slow", 25, -0.5, 15, -0.5), FRESH, self.cfg, NOW)
        self.assertIn("-0.80R", out["reason"])

    def test_strategy_id_argument_overrides_the_entrys_own(self):
        out = evaluate(entry("ordinary", 25, -0.5, 15, -0.5), FRESH, self.cfg, NOW, strategy_id="slow")
        self.assertEqual(out["thresholds"]["strategy_id"], "slow")
        self.assertFalse(out["blocked"])


class TestResearchUsesTheStrategysOwnBar(unittest.TestCase):
    def test_min_trades_for_reads_the_override(self):
        cfg = ResearchLabCfg(gate_overrides={"slow": StrategyGateCfg(min_trades=8)})
        self.assertEqual(min_trades_for(cfg, "slow"), 8)
        self.assertEqual(min_trades_for(cfg, "fast"), cfg.gate_min_trades)

    def test_it_survives_a_missing_config(self):
        self.assertEqual(min_trades_for(None, "any", default=30), 30)


class TestRecommender(unittest.TestCase):
    def _rows(self):
        base = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
        rows = []

        def mk(sid, n, spread, mean):
            for i in range(n):
                rows.append({"state": "CLOSED", "strategy_id": sid, "symbol": "X",
                             "r_multiple": mean + spread * ((-1) ** i) * (0.5 + (i % 5) / 10),
                             "closed_at": base + dt.timedelta(days=30 * i / max(1, n - 1))})
        mk("fast", 60, 0.4, -0.2)       # frequent, tight outcomes
        mk("slow", 10, 2.0, 0.1)        # rare, wide outcomes
        mk("barely", 3, 1.0, 0.0)       # not enough to say anything
        return rows

    def test_a_rare_strategy_gets_a_lower_trade_bar(self):
        recs = recommend(collect(self._rows()), 30, -0.3)
        self.assertLess(recs["slow"]["min_trades"], recs["fast"]["min_trades"])

    def test_a_wider_strategy_gets_a_wider_no_edge_line(self):
        recs = recommend(collect(self._rows()), 30, -0.3)
        self.assertLess(recs["slow"]["block_below_r"], recs["fast"]["block_below_r"])

    def test_a_barely_traded_strategy_gets_no_recommendation_at_all(self):
        """Inventing a threshold from 3 trades would be worse than leaving the default."""
        recs = recommend(collect(self._rows()), 30, -0.3)
        self.assertIsNone(recs["barely"]["min_trades"])
        self.assertEqual(recs["barely"]["confidence"], "NONE")

    def test_the_trade_bar_never_drops_below_something_meaningful(self):
        recs = recommend(collect(self._rows()), 30, -0.3)
        for sid, r in recs.items():
            if r.get("min_trades") is not None:
                self.assertGreaterEqual(r["min_trades"], 10, sid)

    def test_every_recommendation_explains_itself(self):
        for sid, r in recommend(collect(self._rows()), 30, -0.3).items():
            self.assertTrue(r["why"], f"{sid} recommended a threshold with no stated reason")

    def test_confidence_tracks_sample_size(self):
        recs = recommend(collect(self._rows()), 30, -0.3)
        self.assertEqual(recs["slow"]["confidence"], "LOW")
        self.assertEqual(recs["fast"]["confidence"], "MEDIUM")

    def test_empty_input_does_not_explode(self):
        self.assertEqual(recommend(collect([]), 30, -0.3), {})


class TestFallbacksDoNotDrift(unittest.TestCase):
    """gate.FALLBACKS is a second copy of the config's defaults, for configs missing a field entirely.

    Two sources of truth drift. This test makes the drift fail here rather than show up as a gate quietly
    judging against a threshold nobody configured."""

    def test_every_fallback_matches_the_config_default(self):
        from app.research.lab.gate import FALLBACKS
        cfg = ResearchLabCfg()
        for key, value in FALLBACKS.items():
            with self.subTest(key):
                self.assertEqual(getattr(cfg, key), value,
                                 f"gate.FALLBACKS[{key!r}] no longer matches ResearchLabCfg")

    def test_a_config_missing_the_fields_entirely_does_not_raise(self):
        """An exception inside the gate is caught upstream and becomes 'not blocked', so a crash here
        would be a silent fail-open. Resolving thresholds must always succeed."""
        class Bare:
            gate_live = True

        t = Thresholds(Bare(), "anything")
        self.assertEqual(t.min_trades, ResearchLabCfg().gate_min_trades)


class TestNothingChangesWithoutOverrides(unittest.TestCase):
    """Shipping this must not alter a single live decision until the operator sets an override."""

    def test_the_override_map_is_empty_by_default(self):
        self.assertEqual(ResearchLabCfg().gate_overrides, {})

    def test_verdicts_are_identical_to_the_global_path(self):
        cfg = ResearchLabCfg()
        for tn, tr, vn, vr in [(200, -0.9, 100, -1.2), (200, 0.3, 100, 0.25), (4, -2.0, 3, -2.0)]:
            with self.subTest(tn=tn):
                with_id = evaluate(entry("anything", tn, tr, vn, vr), FRESH, cfg, NOW)
                without = evaluate({k: v for k, v in entry("x", tn, tr, vn, vr).items()
                                    if k != "strategy_id"}, FRESH, cfg, NOW)
                self.assertEqual(with_id["blocked"], without["blocked"])
                self.assertEqual(with_id["evidence"], without["evidence"])


if __name__ == "__main__":
    unittest.main()
