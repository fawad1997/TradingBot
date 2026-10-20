"""Research for all eight strategies, not just micro_scalper.

Before this, `gate_strategies` was `["micro_scalper"]` and the lab only knew how to study that one
strategy, so seven of eight traded with no research behind them. These tests pin three things:

  1. the generic per-strategy research produces what `lab/gate.py` already knows how to read
  2. micro_scalper's path is UNCHANGED - its verdict must still come from the sweep, not the new code
  3. nothing newly blocks: research is recorded for all eight, but only gate_strategies may refuse a trade
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.config import ResearchLabCfg
from app.research.lab.gate import evaluate
from app.research.lab.idle import deep_research_plan
from app.research.lab.strategy_research import (DEFAULT_EMBARGO_SECONDS, research_all, research_one,
                                                split_trades, summarise, _verdict)

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(hours=1)).isoformat()
HOUR = 3600


def trade(entry_h, exit_h, r):
    return {"entry_time": entry_h * HOUR, "exit_time": exit_h * HOUR, "r_multiple": r}


# ------------------------------------------------------------------ the purged time split
class TestSplitTrades(unittest.TestCase):
    def test_trades_land_in_the_right_window(self):
        trades = [trade(1, 2, 1.0), trade(2, 3, -1.0), trade(100, 101, 0.5)]
        out = split_trades(trades, split_time=50 * HOUR, embargo_seconds=0)
        self.assertEqual(len(out["train"]), 2)
        self.assertEqual(len(out["test"]), 1)

    def test_a_trade_straddling_the_split_is_purged_from_both(self):
        """It carries information across the boundary, so it belongs to neither side."""
        trades = [trade(40, 60, 2.0)]
        out = split_trades(trades, split_time=50 * HOUR, embargo_seconds=0)
        self.assertEqual(out["train"], [])
        self.assertEqual(out["test"], [])
        self.assertEqual(out["purged"], 1)

    def test_the_embargo_drops_trades_opened_just_after_the_split(self):
        trades = [trade(51, 52, 1.0), trade(200, 201, 1.0)]
        out = split_trades(trades, split_time=50 * HOUR, embargo_seconds=DEFAULT_EMBARGO_SECONDS)
        self.assertEqual(len(out["test"]), 1, "only the trade well past the embargo belongs to test")
        self.assertEqual(out["purged"], 1)

    def test_a_trade_with_no_entry_time_is_purged_not_guessed(self):
        out = split_trades([{"r_multiple": 1.0}], split_time=50 * HOUR)
        self.assertEqual(out["purged"], 1)
        self.assertEqual(out["train"] + out["test"], [])

    def test_no_trade_is_ever_counted_twice(self):
        trades = [trade(i, i + 1, 0.1) for i in range(1, 100)]
        out = split_trades(trades, split_time=50 * HOUR)
        self.assertEqual(len(out["train"]) + len(out["test"]) + out["purged"], len(trades))


# ------------------------------------------------------------------ the verdict
class TestVerdict(unittest.TestCase):
    def test_too_few_training_trades_is_not_a_judgement(self):
        self.assertEqual(_verdict({"n_trades": 5}, {"n_trades": 50}, 30), "TOO_FEW_TRADES")

    def test_too_few_test_trades_is_not_a_judgement_either(self):
        self.assertEqual(_verdict({"n_trades": 50}, {"n_trades": 2}, 30), "TOO_FEW_TEST_TRADES")

    def test_positive_and_profitable_on_test_is_robust(self):
        v = _verdict({"n_trades": 50}, {"n_trades": 20, "expectancy_r": 0.2, "profit_factor": 1.4}, 30)
        self.assertEqual(v, "ROBUST_ON_TEST")

    def test_negative_on_test_is_not_robust(self):
        v = _verdict({"n_trades": 50}, {"n_trades": 20, "expectancy_r": -0.2, "profit_factor": 0.7}, 30)
        self.assertEqual(v, "NOT_ROBUST_ON_TEST")

    def test_positive_expectancy_but_profit_factor_below_one_is_not_robust(self):
        """Guards the inconsistent case rather than trusting one metric alone."""
        v = _verdict({"n_trades": 50}, {"n_trades": 20, "expectancy_r": 0.1, "profit_factor": 0.9}, 30)
        self.assertEqual(v, "NOT_ROBUST_ON_TEST")


# ------------------------------------------------------------------ the gate can read the new shape
class TestGateReadsPerStrategyEntries(unittest.TestCase):
    """The whole point of matching the schema: one gate, two sources of evidence."""

    def _entry(self, verdict, tn, tr, vn, vr):
        return {"strategy_id": "pullback_v1", "verdict": verdict,
                "train": {"n_trades": tn, "expectancy_r": tr},
                "test": {"n_trades": vn, "expectancy_r": vr}}

    def test_a_losing_strategy_is_blocked(self):
        e = self._entry("NOT_ROBUST_ON_TEST", 200, -0.9, 100, -1.2)
        out = evaluate(e, FRESH, ResearchLabCfg(), NOW)
        self.assertTrue(out["blocked"])
        self.assertIn("RESEARCH_LAB_NO_EDGE", out["reason"])

    def test_a_robust_strategy_passes(self):
        e = self._entry("ROBUST_ON_TEST", 200, 0.3, 100, 0.25)
        self.assertFalse(evaluate(e, FRESH, ResearchLabCfg(), NOW)["blocked"])

    def test_thin_research_is_evidence_free_not_approval(self):
        e = self._entry("NOT_ROBUST_ON_TEST", 4, -2.0, 3, -2.0)
        out = evaluate(e, FRESH, ResearchLabCfg(gate_require_evidence=True), NOW)
        self.assertTrue(out["blocked"])
        self.assertEqual(out["evidence"], "THIN")


# ------------------------------------------------------------------ shaping the report
class TestSummarise(unittest.TestCase):
    def test_it_flips_symbol_major_into_strategy_major(self):
        per_symbol = {
            "XAUUSDm": {"a": {"verdict": "ROBUST_ON_TEST"}, "b": {"verdict": "NOT_ROBUST_ON_TEST"}},
            "EURUSDm": {"a": {"verdict": "NOT_ROBUST_ON_TEST"}},
        }
        s = summarise(per_symbol)
        self.assertEqual(set(s["by_strategy"]), {"a", "b"})
        self.assertEqual(set(s["by_strategy"]["a"]), {"XAUUSDm", "EURUSDm"})
        self.assertEqual(s["verdict_counts"]["a"],
                         {"ROBUST_ON_TEST": 1, "NOT_ROBUST_ON_TEST": 1})

    def test_empty_input_does_not_explode(self):
        self.assertEqual(summarise({}), {"by_strategy": {}, "verdict_counts": {}})


# ------------------------------------------------------------------ safety: nothing starts blocking
class TestRecordOnlyByDefault(unittest.TestCase):
    def test_research_runs_for_all_but_only_micro_scalper_can_block(self):
        cfg = ResearchLabCfg()
        self.assertTrue(cfg.research_all_strategies, "research should cover every strategy")
        self.assertEqual(list(cfg.gate_strategies), ["micro_scalper"],
                         "adding a strategy here changes live behaviour - it must stay an operator decision")

    def test_the_slow_path_is_off_in_a_normal_cycle(self):
        """Full replay per strategy per symbol belongs in the weekend window, not the hourly cycle."""
        self.assertFalse(ResearchLabCfg().research_all_in_normal_cycle)

    def test_the_idle_plan_includes_it_once_there_is_time(self):
        self.assertNotIn("all_strategy_research", deep_research_plan(60))
        self.assertIn("all_strategy_research", deep_research_plan(180))

    def test_it_runs_before_the_expensive_searches(self):
        plan = deep_research_plan(900)
        self.assertLess(plan.index("all_strategy_research"), plan.index("extended_walk_forward"))
        self.assertLess(plan.index("all_strategy_research"), plan.index("full_symbol_sweep"))


# ------------------------------------------------------------------ no sweep, by construction
class TestNoParameterSearch(unittest.TestCase):
    """8 strategies x 13 symbols of parameter searching would manufacture findings. It must not happen."""

    def test_research_one_reports_that_nothing_was_tuned(self):
        class Frame:
            t = [0]

            def __len__(self):
                return 3

        out = research_one(type("C", (), {"id": "x", "version": "1"})(), Frame(), None, None)
        self.assertEqual(out["verdict"], "INSUFFICIENT_DATA")

    def test_the_module_never_imports_a_tuner(self):
        import pathlib
        src = pathlib.Path("app/research/lab/strategy_research.py").read_text()
        for banned in ("optuna", "run_sweep", "vbt_sweep", "walk_forward"):
            self.assertNotIn(banned, src,
                             f"{banned} would turn the veto into a selector across 104 cells")


class TestEngineRoutesEachStrategyToItsOwnEvidence(unittest.TestCase):
    """research_gate(sid) must read per-strategy research for everything except micro_scalper,
    which keeps using the sweep result it has always used."""

    def _core(self, **over):
        from tests.helpers import started_core
        core, *_ = started_core(overrides=over)
        return core

    def test_micro_scalper_still_reads_lab_hint(self):
        core = self._core()
        core.lab_hint = {"verdict": "NOT_ROBUST_ON_TEST",
                         "train": {"n_trades": 200, "expectancy_r": -0.9},
                         "test": {"n_trades": 100, "expectancy_r": -1.2}}
        core.lab_hint_created_at = FRESH
        core.strategy_lab_hints = {}
        state = core.research_gate("micro_scalper")
        self.assertTrue(state["blocked"], "micro_scalper's own path must be unchanged")
        self.assertEqual(state["strategy_id"], "micro_scalper")

    def test_another_strategy_reads_its_own_entry_not_micro_scalpers(self):
        core = self._core()
        core.lab_hint = {"verdict": "NOT_ROBUST_ON_TEST",       # micro_scalper is damned...
                         "train": {"n_trades": 200, "expectancy_r": -0.9},
                         "test": {"n_trades": 100, "expectancy_r": -1.2}}
        core.lab_hint_created_at = FRESH
        core.strategy_lab_hints = {"pullback_v1": {                # ...but pullback is fine
            "verdict": "ROBUST_ON_TEST",
            "train": {"n_trades": 200, "expectancy_r": 0.3},
            "test": {"n_trades": 100, "expectancy_r": 0.25}}}
        state = core.research_gate("pullback_v1")
        self.assertFalse(state["blocked"],
                         "pullback_v1 must not inherit micro_scalper's verdict")
        self.assertEqual(state["strategy_id"], "pullback_v1")

    def test_a_strategy_with_no_research_is_missing_evidence_not_approved_silently(self):
        core = self._core()
        core.lab_hint_created_at = FRESH
        core.strategy_lab_hints = {}
        state = core.research_gate("momentum_trend_v1")
        self.assertEqual(state["evidence"], "MISSING")
        self.assertFalse(state["blocked"], "default policy is still fail-open")

    def test_requiring_evidence_blocks_a_strategy_with_no_research(self):
        core = self._core(**{"research_lab.gate_require_evidence": True})
        core.lab_hint_created_at = FRESH
        core.strategy_lab_hints = {}
        self.assertTrue(core.research_gate("momentum_trend_v1")["blocked"])


class TestResearchAllIsFaultTolerant(unittest.TestCase):
    def test_one_broken_strategy_does_not_stop_the_others(self):
        class Boom:
            id = "boom"
            version = "1"

        class Fine:
            id = "fine"
            version = "1"

        class Cfgm:
            class strategies:
                strategies = [Boom(), Fine()]

        out = research_all(Cfgm(), None, None, None)
        self.assertEqual(set(out), {"boom", "fine"})
        for sid in ("boom", "fine"):
            self.assertIn(out[sid]["verdict"], ("INSUFFICIENT_DATA", "ERROR"))


if __name__ == "__main__":
    unittest.main()
