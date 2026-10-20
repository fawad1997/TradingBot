"""Research-lab verdicts gate micro_scalper live. Cases use the lab run of 14 Oct 2026 (00:01 PKT)."""
import unittest
from datetime import datetime, timedelta, timezone

from app.config import ResearchLabCfg
from app.core.types import Gate
from app.dashboard.analysis import classify_block
from app.research.lab.gate import evaluate
from tests.helpers import eligible_route, make_signal, rows, started_core, trades

NOW = datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(hours=1)).isoformat()

# symbol: (train n, train R, test n, test R)
RUN = {
    "AAPLm": (60, -0.36, 20, -0.06), "AMZNm": (70, -0.80, 22, -0.64), "MSFTm": (47, -0.82, 11, -1.75),
    "NVDAm": (45, -0.99, 12, -1.87), "TSLAm": (55, -0.43, 19, -0.71), "BTCUSDm": (349, -0.09, 157, -0.12),
    "ETHUSDm": (269, -0.27, 130, -0.50), "EURUSDm": (212, -0.24, 81, -0.22), "GBPJPYm": (114, -0.26, 40, -0.64),
    "GBPUSDm": (84, -0.33, 41, -0.18), "XAUUSDm": (149, 0.27, 78, -0.29), "XNGUSDm": (165, -2.60, 71, -2.34),
    "XAUUSD247m": (146, 0.19, 43, -0.38),
}


def entry(tn, tr, vn, vr, verdict="NOT_ROBUST_ON_TEST"):
    return {"verdict": verdict, "train": {"n_trades": tn, "expectancy_r": tr},
            "test": {"n_trades": vn, "expectancy_r": vr}}


class TestRule(unittest.TestCase):
    def test_the_real_run(self):
        cfg = ResearchLabCfg()
        blocked = {s for s, v in RUN.items() if evaluate(entry(*v), FRESH, cfg, NOW)["blocked"]}
        # XAUUSD247m: train +0.19R but unseen -0.38R over 43 trades -> blocked by the unseen-alone rule
        self.assertEqual(blocked, {"AMZNm", "MSFTm", "NVDAm", "TSLAm", "ETHUSDm", "GBPJPYm", "XNGUSDm", "XAUUSD247m"})

    def test_run_of_14_oct_0050(self):
        run = {"AAPLm": (77, -0.51, 25, -0.21), "BTCUSDm": (350, -0.09, 155, -0.14), "EURUSDm": (212, -0.24, 80, -0.25),
               "GBPUSDm": (86, -0.35, 41, -0.18), "XAUUSDm": (151, 0.28, 77, -0.34),
               "XAUUSD247m": (147, 0.20, 42, -0.43)}
        allowed = {s for s, v in run.items() if not evaluate(entry(*v), FRESH, ResearchLabCfg(), NOW)["blocked"]}
        self.assertEqual(allowed, {"BTCUSDm", "EURUSDm", "GBPUSDm"})

    def test_unseen_alone_can_be_switched_off(self):
        cfg = ResearchLabCfg(gate_unseen_alone=False)
        self.assertFalse(evaluate(entry(151, 0.28, 77, -0.34), FRESH, cfg, NOW)["blocked"])

    def test_robust_symbol_is_never_blocked(self):
        e = entry(100, -0.9, 50, -0.9, verdict="ROBUST_ON_TEST")
        self.assertFalse(evaluate(e, FRESH, ResearchLabCfg(), NOW)["blocked"])

    def test_positive_unseen_result_is_not_blocked(self):
        self.assertFalse(evaluate(entry(200, -0.8, 50, 0.05), FRESH, ResearchLabCfg(), NOW)["blocked"])

    def test_too_few_trades_is_not_blocked(self):
        state = evaluate(entry(15, -2.0, 5, -2.0), FRESH, ResearchLabCfg(), NOW)
        self.assertFalse(state["blocked"])
        self.assertIn("only 20", state["reason"])

    def test_stale_results_are_ignored(self):
        old = (NOW - timedelta(hours=72)).isoformat()
        self.assertFalse(evaluate(entry(*RUN["XNGUSDm"]), old, ResearchLabCfg(), NOW)["blocked"])

    def test_switch_off(self):
        self.assertFalse(evaluate(entry(*RUN["XNGUSDm"]), FRESH, ResearchLabCfg(gate_live=False), NOW)["blocked"])

    def test_dashboard_category(self):
        self.assertEqual(classify_block("RESEARCH", "RESEARCH_LAB_NO_EDGE (...)"), "RESEARCH_LAB")


class TestLiveCore(unittest.TestCase):
    def _core(self, lab_entry, overrides=None):
        core, *_ = started_core(overrides=overrides or {})
        core.lab_hint = lab_entry
        core.lab_hint_created_at = (core.now_utc() - timedelta(hours=1)).isoformat()
        return core

    def test_micro_scalper_is_rejected_where_the_lab_found_no_edge(self):
        core = self._core(entry(*RUN["XNGUSDm"]))
        sig = make_signal(core)
        sig.strategy_id = "micro_scalper"
        core._process_signal(sig, eligible_route("micro_scalper"), core.cfgm.strategy("micro_scalper"),
                             core.last_tick)
        self.assertEqual(len(trades(core)), 0)
        rej = rows(core, "rejections", signal_id=sig.id)[-1]
        self.assertEqual(rej["gate"], str(Gate.RESEARCH))
        self.assertIn("RESEARCH_LAB_NO_EDGE", rej["reason"])

    def test_other_strategies_are_not_affected(self):
        core = self._core(entry(*RUN["XNGUSDm"]))
        sig = make_signal(core)           # pullback_v1
        core._process_signal(sig, eligible_route(), core.cfgm.strategy("pullback_v1"), core.last_tick)
        self.assertFalse([r for r in rows(core, "rejections", signal_id=sig.id) if r["gate"] == str(Gate.RESEARCH)])

    def test_symbol_without_a_block_trades_normally(self):
        # Pinned to an explicit limit rather than whatever is deployed. XAUUSDm pools to about -0.04R,
        # which sat above the old -0.30 limit and below the -0.05 one adopted on 19 Oct 2026. The rule
        # under test is "a symbol above the limit is allowed", not the value of the limit itself - that
        # is an operator decision and must not be able to break this test.
        core = self._core(entry(*RUN["XAUUSDm"]), overrides={"research_lab.gate_block_below_r": -0.30})
        self.assertFalse(core.research_gate()["blocked"])

    def test_the_same_symbol_is_refused_once_the_limit_is_tightened(self):
        """The other side of the same rule, and the effect of the 19 Oct change."""
        core = self._core(entry(*RUN["XAUUSDm"]), overrides={"research_lab.gate_block_below_r": -0.02})
        self.assertTrue(core.research_gate()["blocked"])


class TestDashboardApi(unittest.TestCase):
    def test_lab_endpoint_reports_the_live_effect(self):
        from starlette.testclient import TestClient
        from app.config import ConfigManager
        from app.core.bridge import Bridge
        from app.dashboard.api import create_app
        from app.storage.repo import MemoryRepo
        repo = MemoryRepo()
        created = datetime.now(timezone.utc).isoformat()
        repo.set_flag("research_lab_latest", {"created_at": created, "symbols": {
            "XNGUSDm": entry(*RUN["XNGUSDm"]), "XAUUSDm": entry(*RUN["XAUUSDm"])}}, "test")
        cfgm = ConfigManager()
        # Same reason as above: pin the limit the endpoint is judged against, so a deliberate change to
        # the deployed value cannot make this test fail for the wrong reason.
        cfgm.app.research_lab.gate_block_below_r = -0.30
        r = TestClient(create_app(Bridge(), repo, cfgm, None)).get("/api/research/lab")
        self.assertEqual(r.status_code, 200)
        gates = r.json()["live_gate"]
        self.assertTrue(gates["XNGUSDm"]["blocked"], "a clearly negative symbol must still be refused")
        self.assertFalse(gates["XAUUSDm"]["blocked"], "one above the limit must still be allowed")


if __name__ == "__main__":
    unittest.main()
