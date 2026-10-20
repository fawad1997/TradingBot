"""Integration tests: full pipeline on simulated markets, backtest causality, and
the PostgreSQL repository against a real server.

PostgreSQL tests run when either
  * TEST_DATABASE_URL is set and psycopg is installed (your PC), or
  * TEST_PG_SHIM="host:port:user:db" is set and the psql CLI exists (CI sandbox).
Use a THROW-AWAY database: tests write rows.
"""
import os
import shutil
import unittest
from collections import Counter

import numpy as np

from tests.helpers import started_core
from app.broker.paper import PaperBroker
from app.broker.sim import SimMarket, aggregate, synthetic_bars
from app.config import ConfigManager, Secrets
from app.core.engine import TradingCore
from app.features.frame import FeatureFrame, resample
from app.research.backtester import BTSettings, run_backtest
from app.simulation import make_sim_core, run_sim, sim_config_dir
from app.storage.repo import MemoryRepo


def pg_repo_factory():
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        try:
            import psycopg  # noqa: F401
            from app.storage.repo import PostgresRepo
            return lambda: PostgresRepo(url)
        except ImportError:
            pass
    shim = os.environ.get("TEST_PG_SHIM")
    if shim and shutil.which("psql"):
        host, port, user, db = shim.split(":")
        from tests.integration.pg_shim import shim_repo
        return lambda: shim_repo({"host": host, "port": port, "user": user, "db": db})
    return None


class TestPipeline(unittest.TestCase):
    def test_demo_pipeline_end_to_end(self):
        core, sim, broker, _ = make_sim_core("DEMO", n_bars=14000, warmup_bars=7000, balance=10000.0, seed=3)
        core.start()
        run_sim(core, sim, 4 * 6500)
        tr = core.repo.tables["trades"]
        closed = [t for t in tr if t["state"] in ("CLOSED", "BROKER_CLOSED")]
        self.assertGreater(len(closed), 0, "expected at least one trade on simulated trend data")
        ev = core.repo.tables["execution_events"]
        for t in closed:
            kinds = [e["event_type"] for e in ev if e["trade_id"] == t["id"]]
            self.assertLess(kinds.index("ORDER_SENT"), kinds.index("FILL"))      # intent before fill
            self.assertIsNotNone(t["r_multiple"])
            self.assertIsNotNone(t["net_pnl"])
        # no orphan: every broker position is known to the book
        for p in broker.positions():
            self.assertIsNotNone(core.book.by_position(p.ticket))
        # every rejection has a counterfactual
        self.assertEqual(len(core.repo.tables["rejections"]), len(core.repo.tables["counterfactuals"]))

    def test_100_dollar_account_trades_nothing_unsafe(self):
        core, sim, broker, _ = make_sim_core("DEMO", n_bars=12000, warmup_bars=7000, balance=100.0, seed=3)
        core.start()
        run_sim(core, sim, 4 * 4000)
        gates = Counter(r["reason"] for r in core.repo.tables["rejections"])
        self.assertEqual(len(core.repo.tables["trades"]), 0)
        self.assertGreater(gates["POSITION_SIZE_INFEASIBLE"], 0)

    def test_shadow_never_sends_orders(self):
        core, sim, broker, _ = make_sim_core("SHADOW", n_bars=12000, warmup_bars=7000, balance=10000.0, seed=3)
        core.start()
        run_sim(core, sim, 4 * 4000)
        self.assertEqual(len(core.repo.tables["trades"]), 0)
        self.assertEqual(len(broker.positions()), 0)
        self.assertTrue(all(s["status"] in ("SHADOW", "CANDIDATE") or s["shadow"]
                            for s in core.repo.tables["signals"]) or not core.repo.tables["signals"])

    def test_dry_run_paper_state_persists(self):
        bars = synthetic_bars(12000, seed=3)
        sim = SimMarket(bars, account_mode="DEMO", balance=10000)
        sim.seek(7000)
        d = sim_config_dir("DRY_RUN", {"paper.starting_balance": 10000.0})
        cfgm = ConfigManager(d / "config.json", d / "strategies.json")
        repo = MemoryRepo()
        core = TradingCore(cfgm, sim, repo, Secrets(), wall=lambda: sim.now, journal_path=d / "j.jsonl")
        self.assertIsInstance(core.exec_broker, PaperBroker)
        core.start()
        run_sim(core, sim, 4 * 4000)
        n_closed = len([t for t in repo.tables["trades"] if t["state"] in ("CLOSED", "BROKER_CLOSED")])
        if n_closed:
            st = repo.get_flag("paper_state")
            self.assertIsNotNone(st)
            self.assertNotEqual(st["balance"], 10000.0)


class TestBacktestCausality(unittest.TestCase):
    def test_future_changes_do_not_change_past_trades(self):
        cfgm = ConfigManager()
        b5 = synthetic_bars(60000, seed=5)
        m = aggregate(b5, 900)[:-1]
        ff = FeatureFrame.from_bars(m, "M15")
        h1 = resample(ff, 3600, "H1")
        cut = int(ff.t[int(len(ff) * 0.7)])
        r1 = run_backtest(cfgm.strategy("pullback_v1"), ff, h1, BTSettings())
        c2 = ff.c.copy()
        h2, l2 = ff.h.copy(), ff.l.copy()
        mask = ff.t >= cut
        c2[mask] *= 1.05
        h2[mask] *= 1.05
        l2[mask] *= 1.05
        ff2 = FeatureFrame(ff.t, np.where(mask, ff.o * 1.05, ff.o), h2, l2, c2, ff.spread, ff.tick_volume, "M15")
        r2 = run_backtest(cfgm.strategy("pullback_v1"), ff2, resample(ff2, 3600, "H1"), BTSettings())
        before1 = [(t["signal_time"], t["direction"], round(t["r_multiple"], 9)) for t in r1.trades if t["exit_time"] < cut]
        before2 = [(t["signal_time"], t["direction"], round(t["r_multiple"], 9)) for t in r2.trades if t["exit_time"] < cut]
        self.assertGreater(len(before1), 5)
        self.assertEqual(before1, before2)


@unittest.skipIf(pg_repo_factory() is None, "no PostgreSQL configured for tests")
class TestPostgres(unittest.TestCase):
    def setUp(self):
        self.repo = pg_repo_factory()()

    def test_crud_types_and_views(self):
        from datetime import datetime, timezone
        from app.core.types import new_id
        r = self.repo
        sid = new_id()
        r.insert("signals", {"id": sid, "created_at": datetime.now(timezone.utc), "strategy_id": "t", "direction": "BUY",
                             "entry": 1.5, "exit_plan": {"a": [1, 2]}, "shadow": False, "status": "CANDIDATE"})
        r.update("signals", "id", sid, {"status": "REJECTED", "features": {"x": 1.0}})
        got = r.select("signals", {"id": sid})[0]
        self.assertEqual(got["status"], "REJECTED")
        self.assertEqual(got["exit_plan"], {"a": [1, 2]})
        r.insert("counterfactuals", {"id": new_id(), "signal_id": sid, "gate": "SPREAD", "status": "RESOLVED",
                                     "hypothetical_r": 1.5})
        self.assertTrue(any(v["gate"] == "SPREAD" for v in r.query("SELECT * FROM v_gate_contribution")))
        r.set_flag("test_flag", {"engaged": True})
        self.assertEqual(r.get_flag("test_flag")["engaged"], True)
        r.upsert("bars", {"symbol": "T", "timeframe": "M15", "time": datetime(2024, 1, 1, tzinfo=timezone.utc),
                          "open": 1, "high": 2, "low": 0.5, "close": 1.5}, ["symbol", "timeframe", "time"], [])
        r.upsert("bars", {"symbol": "T", "timeframe": "M15", "time": datetime(2024, 1, 1, tzinfo=timezone.utc),
                          "open": 9, "high": 9, "low": 9, "close": 9}, ["symbol", "timeframe", "time"], [])
        self.assertEqual(r.select("bars", {"symbol": "T"})[0]["open"], 1)        # DO NOTHING kept the first
        r.insert_many("ticks", [{"symbol": "T", "time": datetime.now(timezone.utc), "bid": 1.0, "ask": 1.1,
                                 "spread_points": 10.0, "session": "ASIA"} for _ in range(3)])
        self.assertGreaterEqual(len(r.select("ticks", {"symbol": "T"})), 3)
        with self.assertRaises(Exception):
            r.insert("signals", {"id": new_id(), "no_such_column": 1})

    def test_core_runs_on_postgres(self):
        """The real trading core, writing every table to PostgreSQL."""
        repo = self.repo
        core, sim, broker, _ = make_sim_core("DEMO", n_bars=9000, warmup_bars=7000, balance=10000.0, seed=3,
                                             repo=repo)
        core.start()
        core.step()
        from tests.helpers import submit
        submit(core)
        core._command("halt", {"reason": "pg test"})
        submit(core)
        core._command("resume", {"reason": "pg test done"})
        run_sim(core, sim, 120)
        self.assertEqual(core.ledger.failures, 0, core.ledger.last_error)
        open_rows = repo.query("SELECT count(*) AS n FROM v_open_trades")
        self.assertGreaterEqual(open_rows[0]["n"], 0)
        self.assertTrue(repo.select("execution_events", {"event_type": "FILL"}, limit=1))
        self.assertTrue(repo.select("rejections", {"gate": "KILL_SWITCH"}, limit=1))
        self.assertTrue(repo.select("config_versions", {"action": "KILL_SWITCH_ENGAGE"}, limit=1))


if __name__ == "__main__":
    unittest.main()
