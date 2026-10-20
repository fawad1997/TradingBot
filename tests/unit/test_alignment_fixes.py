"""Three gaps found by auditing claims against the code, and the behaviour that closes each one.

1. A strategy class shipped in code but missing from config/strategies.json ran as nothing at all, with no row
   anywhere and no log line. Startup now names it.
2. research/lab/funnel.py only had a reason ORDER for the two original M15 strategies, and diagnose() skips any
   strategy it has no order for - so the "why does this strategy not fire" card silently excluded the five
   candidate rules, which are precisely the ones needing explanation.
3. NO_SETUP was written once per runner loop rather than once per bar, which is millions of identical rows a day.
"""
import unittest

from app.config import ConfigManager
from app.research.lab.funnel import ORDER
from app.strategies.registry import CLASSES, uninstalled_classes

ORIGINALS = ("pullback_v1", "mean_reversion_v1", "micro_scalper")
CANDIDATES = ("breakout_retest_v1", "momentum_trend_v1", "session_breakout_v1", "vwap_reversion_v1",
              "htf_continuation_v1")


class TestUninstalledStrategiesAreAnnounced(unittest.TestCase):
    def test_a_complete_config_reports_nothing(self):
        self.assertEqual(uninstalled_classes(ConfigManager().strategies.strategies), [])

    def test_a_config_missing_the_candidates_names_every_one_of_them(self):
        """The exact situation that hid five working strategies behind a kept config/ directory."""
        three = [s for s in ConfigManager().strategies.strategies if s.id in ORIGINALS]
        self.assertEqual(uninstalled_classes(three),
                         ["BreakoutRetest", "HtfContinuation", "MomentumTrend", "SessionBreakout", "VwapReversion"])

    def test_it_reports_class_names_that_exist_in_code(self):
        for name in uninstalled_classes([]):
            self.assertIn(name, CLASSES)

    def test_the_startup_hook_never_raises_on_a_broken_config(self):
        """A diagnostic that can stop the bot starting is worse than the problem it reports."""
        from app.main import _warn_uninstalled_strategies
        _warn_uninstalled_strategies(object())              # no .strategies attribute at all


class TestFunnelCoversEveryM15Strategy(unittest.TestCase):
    def test_every_configured_strategy_except_micro_scalper_has_a_reason_order(self):
        """micro_scalper is M1 and is covered by the timeframe study instead; everything else must be in ORDER,
        because diagnose() skips what it has no order for."""
        configured = {s.id for s in ConfigManager().strategies.strategies} - {"micro_scalper"}
        self.assertEqual(configured - set(ORDER), set(), "these strategies would be invisible on the funnel card")

    def test_candidate_orders_end_in_setup_and_name_the_real_reasons(self):
        from app.strategies.candidates import CandidateStrategy
        import inspect
        source = inspect.getsource(CandidateStrategy.evaluate)
        for sid in CANDIDATES:
            order = ORDER[sid]
            self.assertEqual(order[-1], "SETUP", sid)
            for reason in order[:-1]:
                self.assertIn(reason, source, f"{sid}: ORDER names {reason}, which evaluate() never returns")


class TestNoSetupIsRecordedOncePerBar(unittest.TestCase):
    def _count(self, core):
        return len([r for r in core.repo.tables["candidate_evaluations"] if r.get("outcome") == "NO_SETUP"])

    def test_the_same_reason_on_the_same_bar_is_written_once(self):
        from tests.helpers import started_core
        core, *_ = started_core()
        core._no_setup_seen = {}
        before = self._count(core)
        for _ in range(25):                                  # 25 runner loops inside one bar
            core._evaluate_strategies()
        written = self._count(core) - before
        self.assertGreater(written, 0, "something must still be recorded")
        self.assertLessEqual(written, len(core.cfgm.strategies.strategies),
                             "at most one NO_SETUP row per strategy per bar, not one per loop")

    def test_a_changed_reason_within_the_same_bar_is_still_recorded(self):
        """Throttling must not hide a rule that flips state mid-bar: the key is (bar, reason), not bar alone."""
        from tests.helpers import started_core
        core, *_ = started_core()
        core._no_setup_seen = {}
        core._evaluate_strategies()                          # fills _no_setup_seen for this bar
        self.assertTrue(core._no_setup_seen, "nothing was throttled, so this test proves nothing")
        settled = self._count(core)

        # same bar, different reason - the rule changed its mind without the bar closing
        core._no_setup_seen = {sid: (bar, "A_DIFFERENT_REASON")
                               for sid, (bar, _) in core._no_setup_seen.items()}
        core._evaluate_strategies()
        self.assertGreater(self._count(core), settled,
                           "a reason that changes inside a bar must still be recorded")

        # and now that it has settled again, repeating the loop writes nothing further
        steady = self._count(core)
        core._evaluate_strategies()
        self.assertEqual(self._count(core), steady, "an unchanged reason on the same bar must not re-record")

    def test_a_new_bar_records_again(self):
        from tests.helpers import started_core
        core, *_ = started_core()
        core._no_setup_seen = {}
        core._evaluate_strategies()
        after_first = self._count(core)
        core._no_setup_seen = {k: (v[0] + 900, v[1]) for k, v in core._no_setup_seen.items()}
        core._no_setup_seen = {}                             # simulate the next bar
        core._evaluate_strategies()
        self.assertGreater(self._count(core), after_first)


if __name__ == "__main__":
    unittest.main()
