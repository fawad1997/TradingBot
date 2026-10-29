"""Strategy selector: evidence after costs decides each strategy's share of risk; losers are benched to shadow."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.config import SelectorCfg
from app.core import engine
from app.core.types import Environment, Gate, TrendRegime, epoch_to_utc
from app.regime.detector import RegimeState
from app.regime.router import route
from app.selection.selector import (ACTIVE, BENCHED, PROBATION, UNPROVEN, Allocation, allocate_strategy)
from tests.helpers import make_signal, rows, started_core

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc).timestamp()
CFG = SelectorCfg()


def trade(r, sym="XAUUSD", hours_ago=1.0, regime="TREND_UP"):
    return {"strategy_id": "pullback_v1", "symbol": sym, "state": "CLOSED", "r_multiple": r, "regime": regime,
            "closed_at": epoch_to_utc(NOW - hours_ago * 3600)}


def cf(r, gate="SIZE", sym="XAUUSD", regime="TREND_UP"):
    return {"strategy_id": "pullback_v1", "symbol": sym, "status": "RESOLVED", "gate": gate, "hypothetical_r": r,
            "regime": regime, "resolved_at": epoch_to_utc(NOW - 3600)}


def record(wins, losses, win_r=1.0, loss_r=-1.0, make=trade, **kw):
    return [make(win_r, **kw) for _ in range(wins)] + [make(loss_r, **kw) for _ in range(losses)]


def alloc(trades=(), cfs=(), lab=None, lab_at=None, previous=None, cfg=CFG, sym="XAUUSD"):
    return allocate_strategy("pullback_v1", "1.0.0", sym, list(trades), list(cfs), lab, lab_at, previous, cfg, NOW)


class TestStates(unittest.TestCase):
    def test_cold_start_trades_small_rather_than_not_at_all(self):
        a = alloc()
        self.assertEqual((a.state, a.multiplier), (UNPROVEN, 0.25))
        self.assertAlmostEqual(a.posterior_r, CFG.prior_r)

    def test_confidently_positive_account_record_earns_full_size(self):
        a = alloc(record(65, 35))
        self.assertEqual((a.state, a.multiplier), (ACTIVE, 1.0))
        self.assertGreater(a.lower_r, 0)

    def test_paper_evidence_alone_never_reaches_full_size(self):
        a = alloc(cfs=record(130, 70, make=cf))
        self.assertGreater(a.lower_r, 0)
        self.assertEqual((a.state, a.multiplier), (PROBATION, 0.5))

    def test_a_losing_record_is_benched(self):
        a = alloc(record(20, 40))
        self.assertEqual((a.state, a.multiplier), (BENCHED, 0.0))

    def test_shrinkage_stops_a_lucky_streak(self):
        a = alloc(record(8, 0))
        self.assertEqual(a.state, UNPROVEN)
        self.assertLess(a.posterior_r, 1.0 * 8 / 28 + 1e-9)

    def test_benched_needs_a_margin_to_return(self):
        slightly_positive = record(21, 19)
        self.assertEqual(alloc(slightly_positive).state, PROBATION)
        was_benched = alloc(record(0, 40))
        self.assertEqual(alloc(slightly_positive, previous=was_benched).state, BENCHED)
        self.assertEqual(alloc(record(70, 30), previous=was_benched).state, ACTIVE)


class TestEvidence(unittest.TestCase):
    def test_quality_gates_do_not_count_as_evidence(self):
        self.assertEqual(alloc(cfs=record(0, 100, make=cf, gate="COST")).state, UNPROVEN)
        self.assertEqual(alloc(cfs=record(0, 100, make=cf, gate="SIZE")).state, BENCHED)

    def test_old_trades_age_out(self):
        self.assertEqual(alloc(record(0, 100, hours_ago=200 * 24)).state, UNPROVEN)

    def test_other_symbols_do_not_decide_this_one(self):
        self.assertEqual(alloc(record(0, 100, sym="EURUSD")).state, UNPROVEN)

    def test_fresh_research_counts_stale_or_other_version_does_not(self):
        lab = {"strategy_version": "1.0.0", "r_sd": 1.0, "train": {"n_trades": 100, "expectancy_r": 0.3},
               "test": {"n_trades": 40, "expectancy_r": 0.3}}
        fresh = epoch_to_utc(NOW - 3600).isoformat()
        a = alloc(lab=lab, lab_at=fresh)
        self.assertEqual(a.state, PROBATION)
        self.assertEqual(a.counts, {"lab_test": 40, "lab_train": 100})
        self.assertEqual(alloc(lab=lab, lab_at=epoch_to_utc(NOW - 100 * 3600).isoformat()).state, UNPROVEN)
        self.assertEqual(alloc(lab={**lab, "strategy_version": "0.9.0"}, lab_at=fresh).state, UNPROVEN)


class TestRegimeVeto(unittest.TestCase):
    def test_a_strategy_that_loses_in_one_regime_is_benched_there_only(self):
        trades = record(0, 40, sym="EURUSD", regime="RANGE") + record(30, 10, win_r=1.5, loss_r=-0.5,
                                                                     sym="EURUSD", regime="TREND_UP")
        a = alloc(trades)
        self.assertEqual(a.vetoed_regimes, ["RANGE"])
        self.assertEqual(a.regime_scores["TREND_UP"]["state"], ACTIVE)

    def test_veto_can_be_switched_off(self):
        a = alloc(record(0, 40, regime="RANGE"), cfg=SelectorCfg(regime_veto=False))
        self.assertEqual(a.vetoed_regimes, [])


def _strategy(sid="pullback_v1", risk_multiplier=0.5):
    return SimpleNamespace(id=sid, version="1.0.0", status="DEMO", enabled=True, allowed_regimes=[],
                           risk_multiplier=risk_multiplier)


def _alloc(state, mult, vetoed=()):
    return Allocation("pullback_v1", "XAUUSD", state, mult, "test", 0.0, 0.0, 0.0, 0.0, {}, list(vetoed))


class TestRouter(unittest.TestCase):
    regime = RegimeState(trend=TrendRegime.TREND_UP, label="TREND_UP")

    def _route(self, allocations):
        return route([_strategy()], self.regime, Environment.DEMO, {}, True, allocations)["pullback_v1"]

    def test_no_allocations_is_todays_behaviour(self):
        r = self._route(None)
        self.assertTrue(r.executable)
        self.assertEqual(r.risk_multiplier, 0.5)

    def test_multiplier_only_ever_reduces_risk(self):
        self.assertEqual(self._route({"pullback_v1": _alloc(ACTIVE, 1.0)}).risk_multiplier, 0.5)
        self.assertEqual(self._route({"pullback_v1": _alloc(UNPROVEN, 0.25)}).risk_multiplier, 0.125)

    def test_benched_routes_to_shadow(self):
        r = self._route({"pullback_v1": _alloc(BENCHED, 0.0)})
        self.assertEqual((r.executable, r.shadow), (False, True))
        self.assertTrue(r.reason.startswith("selector"))

    def test_regime_veto_routes_to_shadow(self):
        r = self._route({"pullback_v1": _alloc(ACTIVE, 1.0, vetoed=["TREND_UP"])})
        self.assertEqual((r.executable, r.shadow), (False, True))


class TestConfig(unittest.TestCase):
    def test_multipliers_must_be_ordered(self):
        with self.assertRaises(ValueError):
            SelectorCfg(unproven_multiplier=0.6, probation_multiplier=0.5)


class TestLiveCore(unittest.TestCase):
    def _losing_core(self, repo=None):
        core, *_ = started_core(overrides={"selector.mode": "auto"}, **({"repo": repo} if repo else {}))
        closed = epoch_to_utc(core.wall() - 3600)
        for r in [-1.0] * 40 + [1.0] * 5:
            core.repo.insert("trades", {"id": f"t{len(rows(core, 'trades'))}", "strategy_id": "pullback_v1",
                                        "strategy_version": "1.0.0", "symbol": core.symbol, "state": "CLOSED",
                                        "r_multiple": r, "regime": "TREND_UP", "closed_at": closed})
        engine._SELECTOR_ROWS.clear()      # start-up already scored the empty history; read the new rows
        return core

    def test_cores_share_one_read_of_the_history(self):
        core = self._losing_core()
        core._run_selector()
        calls = []
        real = core.repo.select
        core.repo.select = lambda table, *a, **k: (calls.append(table), real(table, *a, **k))[1]
        core._run_selector()
        self.assertEqual(calls, [], "a second core within the TTL must reuse the rows already read")

    def test_benched_strategy_is_rejected_by_the_selector_gate(self):
        core = self._losing_core()
        core._run_selector()
        self.assertEqual(core.allocations["pullback_v1"].state, BENCHED)
        saved = core.repo.get_flag(f"selector:{core.symbol}")
        self.assertEqual(saved["allocations"]["pullback_v1"]["state"], BENCHED)
        self.assertTrue(rows(core, "selector_history", strategy_id="pullback_v1"))

        rr = route(core.cfgm.strategies.strategies, RegimeState(trend=TrendRegime.TREND_UP, label="TREND_UP"),
                   core.env, {}, True, core.allocations)["pullback_v1"]
        sig = make_signal(core)
        core._process_signal(sig, rr, core.cfgm.strategy("pullback_v1"), core.last_tick)
        rej = rows(core, "rejections", signal_id=sig.id)[-1]
        self.assertEqual(rej["gate"], str(Gate.SELECTOR))
        stored = rows(core, "signals", id=sig.id)[-1]["features"]["selector"]
        self.assertEqual((stored["state"], stored["applied"]), (BENCHED, True))

    def test_benched_state_survives_a_restart(self):
        from app.storage.repo import MemoryRepo
        core = self._losing_core()
        core._run_selector()
        fresh = MemoryRepo()         # no trades: only the saved state can keep it benched
        fresh.set_flag(f"selector:{core.symbol}", core.repo.get_flag(f"selector:{core.symbol}"), "test")
        again, *_ = started_core(repo=fresh)
        self.assertEqual(again.allocations["pullback_v1"].state, BENCHED,
                         "a restart must not hand a benched strategy a fresh start")


class TestResearchCarryForward(unittest.TestCase):
    def test_a_cycle_without_per_strategy_research_keeps_the_last_one(self):
        from app.research.lab.store import FLAG, save_cycle
        from app.storage.repo import MemoryRepo
        repo, root = MemoryRepo(), tempfile.mkdtemp(prefix="lab_")
        first = "2026-10-18T22:00:00+00:00"
        save_cycle({"created_at": first, "symbols": {}, "strategies": {"pullback_v1": {"XAUUSD": {"verdict": "X"}}},
                    "strategy_verdict_counts": {"pullback_v1": {"X": 1}}}, repo, root)
        save_cycle({"created_at": "2026-10-19T04:00:00+00:00", "symbols": {}}, repo, root)
        flag = repo.get_flag(FLAG)
        self.assertEqual(flag["strategies"]["pullback_v1"]["XAUUSD"]["verdict"], "X")
        self.assertEqual(flag["strategies_created_at"], first)


class TestNightly(unittest.TestCase):
    def test_due_once_per_day_after_the_hour(self):
        from app.research.lab.worker import nightly_due
        at = lambda d, h: datetime(2026, 10, d, h, 30, tzinfo=timezone.utc)
        self.assertTrue(nightly_due(at(20, 22), 21, None))
        self.assertTrue(nightly_due(at(20, 22), 21, at(20, 20).isoformat()))
        self.assertFalse(nightly_due(at(20, 22), 21, at(20, 21).isoformat()))
        self.assertFalse(nightly_due(at(21, 10), 21, at(20, 21).isoformat()))
        self.assertTrue(nightly_due(at(21, 10), 21, at(20, 20).isoformat()))


class TestDashboardApi(unittest.TestCase):
    def test_selector_endpoint_lists_every_symbol(self):
        import asyncio
        import json
        from app.config import ConfigManager
        from app.core.bridge import Bridge
        from app.dashboard.api import create_app
        from app.storage.repo import MemoryRepo
        repo = MemoryRepo()
        repo.set_flag("selector:XAUUSDm", {"computed_at": "x", "mode": "auto",
                                           "allocations": {"pullback_v1": alloc().to_dict()}}, "test")
        app = create_app(Bridge(), repo, ConfigManager(), "token")
        endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/selector")
        body = json.loads(asyncio.run(endpoint(None)).body)
        self.assertEqual(list(body["symbols"]), ["XAUUSDm"])
        self.assertEqual(body["symbols"]["XAUUSDm"]["allocations"]["pullback_v1"]["state"], UNPROVEN)


if __name__ == "__main__":
    unittest.main()
