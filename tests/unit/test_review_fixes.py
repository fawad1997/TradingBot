"""Fixes from the external review of 18 Oct 2026.

The review raised P0/P1 items against the Field Manual. Four survived checking against the code; this file
pins all four so they cannot silently come back. Each class names the claim it answers and whether the
reviewer was right, because a test that does not say what it is defending gets deleted in a year.

  P0-4  research gate fails open ................. CONFIRMED - the reviewer was right
  C     adaptive spread cap "only ratchets down" .. WRONG mechanism, but exposed a real staleness bug
  B     max_armed_symbols = 13 on 13 symbols ...... CONFIRMED - a ceiling that refuses nothing
  F     "fourteen checkpoints" ................... CONFIRMED - matched neither 13 nor 15
"""
import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.adaptive.optimizer import AdaptiveResult, AdaptiveState, compute
from app.config import AdaptiveCfg, NewsStraddleCfg, ResearchLabCfg
from app.core.types import STARTUP_SEQUENCE, StartupState
from app.research.lab.gate import NO_EVIDENCE_STATES, evaluate

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(hours=1)).isoformat()
STALE = (NOW - timedelta(hours=400)).isoformat()


def entry(tn, tr, vn, vr, verdict="NOT_ROBUST_ON_TEST"):
    return {"verdict": verdict, "train": {"n_trades": tn, "expectancy_r": tr},
            "test": {"n_trades": vn, "expectancy_r": vr}}


# ---------------------------------------------------------------- P0-4: the gate's fail-open behaviour
class TestGateFailsOpenByDefault(unittest.TestCase):
    """The reviewer's claim, reproduced. These assertions document behaviour that is DELIBERATE, not fixed.

    Absence of research evidence permits the trade. That is the right call while the lab is still filling
    up and the wrong one on real money, which is what gate_require_evidence is for."""

    def setUp(self):
        self.cfg = ResearchLabCfg(gate_require_evidence=False)

    def test_no_result_at_all_permits(self):
        out = evaluate(None, None, self.cfg, NOW)
        self.assertFalse(out["blocked"])
        self.assertEqual(out["evidence"], "MISSING")

    def test_stale_result_permits(self):
        out = evaluate(entry(200, -2.0, 100, -2.0), STALE, self.cfg, NOW)
        self.assertFalse(out["blocked"], "a 400h-old damning result still permits when failing open")
        self.assertEqual(out["evidence"], "STALE")

    def test_incomplete_numbers_permit(self):
        out = evaluate({"verdict": "NOT_ROBUST_ON_TEST", "train": {}, "test": {}}, FRESH, self.cfg, NOW)
        self.assertFalse(out["blocked"])
        self.assertEqual(out["evidence"], "INCOMPLETE")

    def test_thin_evidence_permits(self):
        out = evaluate(entry(3, -2.0, 2, -2.0), FRESH, self.cfg, NOW)
        self.assertFalse(out["blocked"])
        self.assertEqual(out["evidence"], "THIN")

    def test_every_permit_path_names_its_evidence_state(self):
        """No silent permits: whatever the gate allows, the result says on what basis."""
        for label, (e, ts) in {
            "missing": (None, None),
            "stale": (entry(200, -2.0, 100, -2.0), STALE),
            "incomplete": ({"verdict": "NOT_ROBUST_ON_TEST", "train": {}, "test": {}}, FRESH),
            "thin": (entry(3, -2.0, 2, -2.0), FRESH),
            "other verdict": (entry(200, 0.5, 100, 0.4, verdict="ROBUST"), FRESH),
            "unseen positive": (entry(200, -0.9, 100, 0.2), FRESH),
        }.items():
            with self.subTest(label):
                out = evaluate(e, ts, self.cfg, NOW)
                self.assertIn("evidence", out)
                self.assertTrue(out["reason"], "a permit with no stated reason is a silent permit")


class TestGateCanFailClosed(unittest.TestCase):
    """gate_require_evidence inverts it: no evidence becomes a block, not a pass."""

    def setUp(self):
        self.cfg = ResearchLabCfg(gate_require_evidence=True)

    def test_all_four_no_evidence_states_block(self):
        cases = {
            "MISSING": (None, None),
            "STALE": (entry(200, -2.0, 100, -2.0), STALE),
            "INCOMPLETE": ({"verdict": "NOT_ROBUST_ON_TEST", "train": {}, "test": {}}, FRESH),
            "THIN": (entry(3, -2.0, 2, -2.0), FRESH),
        }
        self.assertEqual(set(cases), set(NO_EVIDENCE_STATES), "a new no-evidence state was added untested")
        for state, (e, ts) in cases.items():
            with self.subTest(state):
                out = evaluate(e, ts, self.cfg, NOW)
                self.assertTrue(out["blocked"], f"{state} must block when evidence is required")
                self.assertEqual(out["evidence"], state)
                self.assertIn("RESEARCH_LAB_NO_EVIDENCE", out["reason"])

    def test_a_missing_verdict_blocks_but_a_different_verdict_does_not(self):
        """The reviewer asked for 'missing verdicts' specifically.

        No verdict at all is not a judgement. A verdict that merely is not NOT_ROBUST_ON_TEST (say ROBUST)
        is the lab having looked and not condemned it, which is real evidence."""
        no_verdict = {"train": {"n_trades": 200, "expectancy_r": -2.0},
                      "test": {"n_trades": 100, "expectancy_r": -2.0}}
        out = evaluate(no_verdict, FRESH, self.cfg, NOW)
        self.assertTrue(out["blocked"])
        self.assertEqual(out["evidence"], "INCOMPLETE")

        out = evaluate(entry(200, 0.5, 100, 0.4, verdict="ROBUST"), FRESH, self.cfg, NOW)
        self.assertFalse(out["blocked"])
        self.assertEqual(out["evidence"], "OK")

    def test_good_evidence_still_permits(self):
        """Failing closed must not become 'block everything' - a real pass still passes."""
        out = evaluate(entry(200, 0.5, 100, 0.4, verdict="ROBUST"), FRESH, self.cfg, NOW)
        self.assertFalse(out["blocked"])
        self.assertEqual(out["evidence"], "OK")

    def test_a_real_no_edge_verdict_still_blocks_for_its_own_reason(self):
        out = evaluate(entry(200, -0.9, 100, -1.2), FRESH, self.cfg, NOW)
        self.assertTrue(out["blocked"])
        self.assertIn("RESEARCH_LAB_NO_EDGE", out["reason"])
        self.assertEqual(out["evidence"], "OK", "the lab did judge this one; the evidence is fine")

    def test_gate_off_still_wins(self):
        """Turning the gate off must turn it off, whatever the evidence flag says."""
        cfg = ResearchLabCfg(gate_live=False, gate_require_evidence=True)
        out = evaluate(None, None, cfg, NOW)
        self.assertFalse(out["blocked"])
        self.assertEqual(out["evidence"], "GATE_OFF")

    def test_default_is_still_fail_open(self):
        """Demo trade flow must not change by accident when this ships."""
        self.assertFalse(ResearchLabCfg().gate_require_evidence)


# ------------------------------------------------- Claim C: the cap recovers, but it did not expire
class TestAdaptiveCapIsNotARatchet(unittest.TestCase):
    """The reviewer's mechanism was wrong. The cap is recomputed and ASSIGNED each cycle, so it recovers."""

    def test_cap_widens_again_when_spreads_widen(self):
        cfg = AdaptiveCfg(spread_min_samples=50)
        atr = [1.0] * (cfg.atr_long_bars + 5)
        tight = compute(1.0, atr, [10.0] * 120, 60.0, cfg)
        self.assertIsNotNone(tight.max_spread_points, "a quiet market should tighten below the global 60")
        wide = compute(1.0, atr, [55.0] * 120, 60.0, cfg)
        self.assertIsNone(wide.max_spread_points,
                          "once the derived cap is no longer tighter than global, it must revert to None")

    def test_apply_assigns_rather_than_minimises(self):
        st = AdaptiveState("XAUUSDm")
        st.apply(AdaptiveResult(vol_scale=1.0, max_spread_points=12.0, updated=True, reason="OK"))
        st.apply(AdaptiveResult(vol_scale=1.0, max_spread_points=40.0, updated=True, reason="OK"))
        self.assertEqual(st.max_spread_points, 40.0, "a later, looser cap must replace the earlier tight one")


class TestAdaptiveCapExpires(unittest.TestCase):
    """The real bug the claim led to: a cap that cannot refresh used to live forever."""

    def _state_with_cap(self, cap, age_minutes):
        st = AdaptiveState("XAUUSDm")
        st.max_spread_points = cap
        st.updated_at = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).isoformat()
        return st

    def test_fresh_cap_is_used(self):
        st = self._state_with_cap(12.0, 10)
        self.assertEqual(st.effective_max_spread_points(180), 12.0)

    def test_expired_cap_is_dropped(self):
        st = self._state_with_cap(12.0, 400)
        self.assertIsNone(st.effective_max_spread_points(180),
                          "a cap older than the limit must stop tightening the spread gate")

    def test_cap_surviving_a_failed_refresh_still_expires(self):
        """apply() keeps the old value when a refresh cannot run - that is where the stale cap came from."""
        st = self._state_with_cap(12.0, 400)
        st.apply(AdaptiveResult(updated=False, reason="INSUFFICIENT_ATR_HISTORY (3)"))
        self.assertEqual(st.max_spread_points, 12.0, "apply() still keeps it...")
        self.assertIsNone(st.effective_max_spread_points(180), "...but the consumer must not use it")

    def test_cap_restored_across_a_restart_expires(self):
        """A weekend restart must not reinstate Friday's quiet-hour ceiling."""
        friday = self._state_with_cap(12.0, 60 * 60).to_dict()
        monday = AdaptiveState("XAUUSDm")
        monday.restore(friday)
        self.assertEqual(monday.max_spread_points, 12.0)
        self.assertIsNone(monday.effective_max_spread_points(180))

    def test_cap_without_a_timestamp_is_not_trusted(self):
        st = AdaptiveState("XAUUSDm")
        st.max_spread_points = 12.0
        st.updated_at = None
        self.assertIsNone(st.effective_max_spread_points(180),
                          "a cap that cannot be shown to be fresh must not be used")

    def test_no_cap_is_simply_no_cap(self):
        self.assertIsNone(AdaptiveState("XAUUSDm").effective_max_spread_points(180))


# -------------------------------------------------------- Claim B: a ceiling that constrains nothing
class TestStraddleCeilingBinds(unittest.TestCase):
    SYMS = [f"SYM{i}m" for i in range(13)]

    def test_ceiling_equal_to_symbol_count_does_not_bind(self):
        cfg = NewsStraddleCfg(enabled=True, symbols=self.SYMS, max_armed_symbols=13)
        self.assertFalse(cfg.ceiling_binds(), "13 symbols with a ceiling of 13 refuses nothing")

    def test_ceiling_above_symbol_count_does_not_bind(self):
        cfg = NewsStraddleCfg(enabled=True, symbols=self.SYMS, max_armed_symbols=20)
        self.assertFalse(cfg.ceiling_binds())

    def test_lower_ceiling_binds(self):
        cfg = NewsStraddleCfg(enabled=True, symbols=self.SYMS, max_armed_symbols=4)
        self.assertTrue(cfg.ceiling_binds())

    def test_wildcard_is_assumed_to_bind(self):
        """The symbol count is not knowable from this model, so do not claim it is non-binding."""
        self.assertTrue(NewsStraddleCfg(enabled=True, symbols=["*"], max_armed_symbols=3).ceiling_binds())


# ------------------------------------------------------------ Claim F: documentation vs the code
class TestStartupCountsComeFromCode(unittest.TestCase):
    def test_the_two_numbers_are_what_the_docs_must_say(self):
        self.assertEqual(len(STARTUP_SEQUENCE), 13)
        self.assertEqual(len(list(StartupState)), 15)

    def test_the_difference_is_exactly_the_terminal_states(self):
        extra = {str(s) for s in StartupState} - {str(s) for s in STARTUP_SEQUENCE}
        self.assertEqual(extra, {"STOPPED", "FAILED"},
                         "if this changes, every hand-written count in the docs is now wrong")

    def test_fourteen_appears_nowhere(self):
        """The exact error the reviewer found: a number matching neither 13 nor 15."""
        self.assertNotIn(len(STARTUP_SEQUENCE), (14,))
        self.assertNotIn(len(list(StartupState)), (14,))


class TestDocFactsTool(unittest.TestCase):
    def test_tool_runs_and_reports_the_code_derived_counts(self):
        r = subprocess.run([sys.executable, "tools/doc_facts.py", "--json"],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        facts = json.loads(r.stdout)
        self.assertEqual(facts["startup"]["checkpoints"], len(STARTUP_SEQUENCE))
        self.assertEqual(facts["startup"]["enum_members"], len(list(StartupState)))
        self.assertEqual(sorted(facts["startup"]["terminal_states"]), ["FAILED", "STOPPED"])


class TestGateErrorPathFollowsThePolicy(unittest.TestCase):
    """The ninth fail-open path: an exception inside research_gate(). The reviewer listed this one as
    'database errors' and asked that an unavailable result not be treated as approval."""

    def _core_with_broken_gate(self, require: bool):
        from tests.helpers import started_core
        core, *_ = started_core(overrides={"research_lab.gate_require_evidence": require})

        def boom(*_a, **_k):
            raise RuntimeError("database is down")

        # NOT cfgm.strategy: that call sits inside a nested try/except that swallows it, so the outer
        # handler never runs and this test would pass without exercising anything. now_utc() is in the
        # outer try block.
        core.now_utc = boom
        return core

    def test_an_exception_permits_when_failing_open(self):
        state = self._core_with_broken_gate(False).research_gate()
        self.assertFalse(state["blocked"])
        self.assertEqual(state.get("evidence"), "ERROR")

    def test_an_exception_blocks_when_evidence_is_required(self):
        state = self._core_with_broken_gate(True).research_gate()
        self.assertTrue(state["blocked"], "a gate that cannot run must not count as approval")
        self.assertIn("RESEARCH_LAB_NO_EVIDENCE", state["reason"])


class TestLiveReadinessCoversTheReviewFindings(unittest.TestCase):
    """The three settings that are fine on demo and not fine on real money."""

    def test_config_checks_exist_and_name_all_three(self):
        from tools.live_readiness import _config_checks
        names = " ".join(c["name"] for c in _config_checks()).lower()
        for needle in ("research gate", "ceiling", "commission"):
            self.assertIn(needle, names)

    def test_each_check_explains_itself(self):
        from tools.live_readiness import _config_checks
        for c in _config_checks():
            with self.subTest(c["name"]):
                self.assertTrue(c["why"], "a readiness check with no stated reason cannot be acted on")
                self.assertTrue(c["needs"])


if __name__ == "__main__":
    unittest.main()
