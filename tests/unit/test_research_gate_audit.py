"""What the research gate actually enforces - written down so it cannot drift silently.

These pin the gate's real behaviour against the five explanations for why micro_scalper kept trading while
losing: stale evidence, verdict semantics, timeframe mismatch, a routing bypass, or an enforcement bug.

The finding that mattered: evaluate() reads NO numbers unless the stored verdict is exactly
NOT_ROBUST_ON_TEST, and the verdicts that mean the lab could not measure anything - NO_QUALIFYING_PARAMETERS,
TOO_FEW_SIGNALS, INSUFFICIENT_DATA, ERROR - used to come back as evidence "OK", so they permitted even with
gate_require_evidence on. Since 23 Oct 2026 they come back as THIN / INCOMPLETE: still permitted by default
(the gate fails open by design), blocked when evidence is required.
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.config import ConfigManager
from app.research.lab.gate import evaluate

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(hours=1)).isoformat()
# numbers so bad that any sane gate would block: -0.83R pooled over 600 backtest trades
AWFUL = {"train": {"n_trades": 400, "expectancy_r": -0.80}, "test": {"n_trades": 200, "expectancy_r": -0.90}}
ALL_VERDICTS = ("NOT_ROBUST_ON_TEST", "ROBUST_ON_TEST", "NO_QUALIFYING_PARAMETERS", "TOO_FEW_SIGNALS",
                "INSUFFICIENT_DATA", "ERROR", "SYMBOL_UNAVAILABLE")


def cfg():
    return ConfigManager().app.research_lab


class TestVerdictSemantics(unittest.TestCase):
    """Hypothesis: threshold semantics / enforcement bug."""

    def test_only_one_verdict_can_ever_block(self):
        blocking = [v for v in ALL_VERDICTS
                    if evaluate({"verdict": v, **AWFUL}, FRESH, cfg(), NOW)["blocked"]]
        self.assertEqual(blocking, ["NOT_ROBUST_ON_TEST"])

    def test_unmeasurable_verdicts_are_no_evidence_not_a_pass(self):
        """'Could not measure' is not 'passed': it is labelled as missing evidence, which permits by default."""
        for v, state_name in (("NO_QUALIFYING_PARAMETERS", "THIN"), ("TOO_FEW_SIGNALS", "THIN"),
                              ("INSUFFICIENT_DATA", "THIN"), ("ERROR", "INCOMPLETE"),
                              ("SYMBOL_UNAVAILABLE", "INCOMPLETE")):
            state = evaluate({"verdict": v, **AWFUL}, FRESH, cfg(), NOW)
            self.assertFalse(state["blocked"], v)
            self.assertEqual(state["evidence"], state_name, v)
            self.assertIn(v, state["reason"])

    def test_unmeasurable_verdicts_block_when_evidence_is_required(self):
        """The contract gate_require_evidence promises - and tools/live_readiness.py relies on for real money."""
        strict = cfg().model_copy(update={"gate_require_evidence": True})
        for v in ("NO_QUALIFYING_PARAMETERS", "TOO_FEW_SIGNALS", "INSUFFICIENT_DATA", "ERROR"):
            state = evaluate({"verdict": v, **AWFUL}, FRESH, strict, NOW)
            self.assertTrue(state["blocked"], v)
            self.assertIn("RESEARCH_LAB_NO_EVIDENCE", state["reason"])
        self.assertFalse(evaluate({"verdict": "ROBUST_ON_TEST", **AWFUL}, FRESH, strict, NOW)["blocked"],
                         "a judged, passing verdict is evidence")

    def test_the_numbers_are_never_even_read_for_those_verdicts(self):
        """pooled_r stays None, proving the gate returned before reaching the arithmetic."""
        state = evaluate({"verdict": "NO_QUALIFYING_PARAMETERS", **AWFUL}, FRESH, cfg(), NOW)
        self.assertIsNone(state["pooled_r"])
        self.assertIsNone(state["pooled_trades"])

    def test_a_missing_verdict_key_also_passes(self):
        self.assertFalse(evaluate(dict(AWFUL), FRESH, cfg(), NOW)["blocked"])


class TestStaleness(unittest.TestCase):
    """Hypothesis: stale research evidence."""

    def test_results_older_than_the_limit_stop_blocking(self):
        c = cfg()
        old = (NOW - timedelta(hours=c.gate_max_age_hours + 1)).isoformat()
        state = evaluate({"verdict": "NOT_ROBUST_ON_TEST", **AWFUL}, old, c, NOW)
        self.assertFalse(state["blocked"])
        self.assertIn("stale", state["reason"])

    def test_a_missing_timestamp_stops_blocking(self):
        state = evaluate({"verdict": "NOT_ROBUST_ON_TEST", **AWFUL}, None, cfg(), NOW)
        self.assertFalse(state["blocked"])

    def test_a_result_inside_the_window_still_blocks(self):
        c = cfg()
        recent = (NOW - timedelta(hours=c.gate_max_age_hours - 1)).isoformat()
        self.assertTrue(evaluate({"verdict": "NOT_ROBUST_ON_TEST", **AWFUL}, recent, c, NOW)["blocked"])

    def test_a_dead_lab_means_no_gate_at_all(self):
        """Deliberate - a broken lab must not freeze trading - but it means silence looks like a pass."""
        self.assertFalse(evaluate({}, FRESH, cfg(), NOW)["blocked"])
        self.assertFalse(evaluate(None, FRESH, cfg(), NOW)["blocked"])


class TestTradeCountAndLimit(unittest.TestCase):
    """Hypothesis: the numbers were read and genuinely passed."""

    def test_below_min_trades_the_gate_declines_to_judge(self):
        c = cfg()
        few = {"train": {"n_trades": 5, "expectancy_r": -0.9}, "test": {"n_trades": 3, "expectancy_r": -0.9}}
        state = evaluate({"verdict": "NOT_ROBUST_ON_TEST", **few}, FRESH, c, NOW)
        self.assertFalse(state["blocked"])
        self.assertIn("backtest trades", state["reason"])

    def test_positive_unseen_data_passes(self):
        good = {"train": {"n_trades": 400, "expectancy_r": -0.9}, "test": {"n_trades": 200, "expectancy_r": 0.1}}
        self.assertFalse(evaluate({"verdict": "NOT_ROBUST_ON_TEST", **good}, FRESH, cfg(), NOW)["blocked"])

    def test_unseen_alone_can_block_despite_a_flattering_pooled_average(self):
        """XAUUSDm 14 Oct: train +0.28R, unseen -0.34R over 77 trades."""
        c = cfg()
        self.assertTrue(c.gate_unseen_alone)
        mixed = {"train": {"n_trades": 300, "expectancy_r": 0.28},
                 "test": {"n_trades": 77, "expectancy_r": -0.34}}
        self.assertTrue(evaluate({"verdict": "NOT_ROBUST_ON_TEST", **mixed}, FRESH, c, NOW)["blocked"])

    def test_gate_live_off_disables_everything(self):
        class Off:
            gate_live = False
        self.assertFalse(evaluate({"verdict": "NOT_ROBUST_ON_TEST", **AWFUL}, FRESH, Off(), NOW)["blocked"])


class TestScopeAndRouting(unittest.TestCase):
    """Hypotheses: strategy/timeframe mismatch, and a bypass in routing."""

    def test_the_gate_is_consulted_for_one_strategy_only(self):
        c = cfg()
        self.assertEqual(list(c.gate_strategies), ["micro_scalper"])

    def test_the_call_site_is_limited_to_that_list(self):
        import inspect
        from app.core.engine import TradingCore
        src = inspect.getsource(TradingCore._process_signal)
        self.assertIn("if scfg.id in self.cfg.research_lab.gate_strategies:", src)

    def test_the_judged_timeframe_follows_micro_scalpers_own_trigger(self):
        import inspect
        from app.core.engine import TradingCore
        src = inspect.getsource(TradingCore.research_gate)
        self.assertIn('trigger_timeframe', src)
        self.assertEqual(
            str(ConfigManager().strategy("micro_scalper").param_values()["trigger_timeframe"]).upper(), "M1")

    def test_the_pending_queue_does_not_recheck_the_gate(self):
        """A signal queued waiting for spread executes on the verdict it had when queued, not the current one.
        The window is one signal TTL, so this is a narrow gap rather than the main route through."""
        import inspect
        from app.core.engine import TradingCore
        src = inspect.getsource(TradingCore._process_pending)
        self.assertNotIn("research_gate", src)
        self.assertIn("KILL_SWITCH_WHILE_WAITING", src)      # it does re-check these


if __name__ == "__main__":
    unittest.main()
