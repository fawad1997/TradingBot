"""Fixes for the defects confirmed in the 23 Oct 2026 audit, each pinned where it was found.

The gate, correlation, equity-chart and websocket fixes have tests of their own (test_research_gate_audit,
test_portfolio_risk*, test_equity_history, test_dashboard_ws_idle); this file holds the rest.
"""
import asyncio
import dataclasses
import json
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

from tests.helpers import eligible_route, make_signal, started_core
from app.alpha.exits import SimulatedTrade, with_execution_exits
from app.broker.paper import PaperBroker
from app.broker.sim import SimMarket, synthetic_bars, xau_spec
from app.config import ConfigManager, MLFilterCfg
from app.core.bridge import Bridge
from app.core.types import Position, RiskState, Side, TradePlan
from app.dashboard.api import create_app, summarize_candidate_counts
from app.edge.cost import CostModel
from app.research.metrics import trade_metrics
from app.storage.repo import MemoryRepo

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.now(timezone.utc)


def call(app, path: str, query: str = "", path_params: dict = None):
    """One GET straight to the endpoint (no TestClient, so no httpx needed)."""
    endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", None) == path)
    req = Request({"type": "http", "method": "GET", "path": path, "query_string": query.encode(), "headers": [],
                   "path_params": path_params or {}})
    return json.loads(asyncio.run(endpoint(req)).body)


def dashboard(repo):
    return create_app(Bridge(), repo, ConfigManager(), "tok")


def schema_columns(table: str) -> set:
    sql = (ROOT / "database" / "01_schema.sql").read_text(encoding="utf-8")
    body = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", sql, re.S).group(1)
    return {m.group(1) for m in re.finditer(r"^\s+([a-z_]+)\s+[a-z]", body, re.M)}


class TestEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = started_core()[0]

    def test_a_signal_waiting_for_its_spread_meets_a_risk_stop(self):
        """It used to re-check only the kill switch, so it could trade after the daily loss limit."""
        core = self.core
        sig = make_signal(core)
        core.pending = [{"signal": sig, "rr": eligible_route(), "scfg": core.cfgm.strategy("pullback_v1"),
                         "est": None, "reason": "SPREAD_ABOVE_MAX"}]
        before, core.risk = core.risk, dataclasses.replace(core.risk, state=RiskState.NO_NEW_TRADES)
        try:
            core._process_pending(core.last_tick)
        finally:
            core.risk = before
        self.assertEqual(core.pending, [])
        rej = [r for r in core.repo.tables["rejections"] if r.get("signal_id") == sig.id]
        self.assertEqual(rej[-1]["reason"], "RISK_STATE_NO_NEW_TRADES")
        self.assertTrue(rej[-1]["details"]["while_waiting_for_spread"])

    def test_strategy_health_is_judged_on_the_newest_trades(self):
        """An ascending select judged a strategy past 1,000 trades on its first 1,000 for good."""
        c = self.core.cfgm.strategy("mean_reversion_v1")
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for k in range(1100):
            self.core.repo.insert("trades", {"id": f"health-{k}", "strategy_id": c.id, "strategy_version": c.version,
                                             "state": "CLOSED", "closed_at": base + timedelta(hours=k),
                                             "r_multiple": 0.5 if k < 1000 else -1.0})
        self.core._refresh_strategy_stats()
        stats = self.core._strategy_stats[c.id]
        self.assertNotEqual(stats["health"], "HEALTHY", "the newest 100 trades all lost a full R")
        self.assertAlmostEqual(stats["expectancy_r"], (900 * 0.5 - 100) / 1000)

    def test_a_short_position_counts_as_short(self):
        core, cache = self.core, self.core.positions_cache
        core.positions_cache = [Position(ticket=7, symbol="EURUSDm", side=Side.SELL, volume=0.2, price_open=1.1,
                                         sl=1.11, tp=1.09, price_current=1.1, profit=0.0, swap=0.0, magic=1,
                                         comment="", time=0)]
        try:
            self.assertEqual(core._open_directions_by_symbol(), {"EURUSDm": -1})
        finally:
            core.positions_cache = cache


class TestStorage(unittest.TestCase):
    def test_idle_research_rows_fit_the_table(self):
        """It wrote columns the table does not have, and no symbol, and logged the refusal at DEBUG."""
        from app.research.lab.idle import _save
        repo = MemoryRepo()
        _save(repo, {"ran": True, "results": {"data_quality": {"note": "x"}}})
        row = repo.tables["research_lab_runs"][0]
        self.assertLessEqual(set(row) - {"id"}, schema_columns("research_lab_runs"))
        self.assertEqual((row["symbol"], row["verdict"]), ("*", "IDLE_DEEP_RESEARCH"))

    def test_limit_zero_means_zero_rows(self):
        repo = MemoryRepo()
        for k in range(3):
            repo.insert("app_logs", {"level": "WARNING", "message": str(k)})
        self.assertEqual(repo.select("app_logs", limit=0), [])
        self.assertEqual(len(repo.select("app_logs", limit=None)), 3)


class TestDashboardWindows(unittest.TestCase):
    def test_a_bad_or_zero_parameter_is_clamped_not_an_error_or_the_whole_table(self):
        repo = MemoryRepo()
        for k in range(5):
            repo.insert("app_logs", {"level": "WARNING", "message": str(k)})
        app = dashboard(repo)
        self.assertEqual(len(call(app, "/api/logs/{tab}", "limit=0", {"tab": "errors"})), 1)
        self.assertEqual(len(call(app, "/api/logs/{tab}", "limit=oops", {"tab": "errors"})), 5)
        self.assertEqual(call(app, "/api/equity", "hours=oops"), [])

    def test_a_busy_window_is_counted_whole(self):
        """The newest 2,000 rejections, filtered afterwards, reported 2,000 of 2,600."""
        repo = MemoryRepo()
        for k in range(2600):
            repo.insert("rejections", {"created_at": NOW - timedelta(minutes=k % 600), "strategy_id": "s",
                                       "symbol": "EURUSDm", "gate": "SPREAD", "reason": "SPREAD_ABOVE_MAX"})
        repo.insert("rejections", {"created_at": NOW - timedelta(days=5), "strategy_id": "s", "gate": "SPREAD"})
        d = call(dashboard(repo), "/api/analysis/{kind}", "hours=48&limit=10", {"kind": "blocked"})
        self.assertEqual(d["summary"]["blocked"], 2600)
        self.assertEqual(len(d["rows"]), 10)

    def test_candidate_summary_counts_the_window_and_grouped_rows(self):
        repo = MemoryRepo()
        for k in range(12):
            repo.insert("candidate_evaluations", {"created_at": NOW - timedelta(hours=k), "symbol": "A",
                                                  "strategy_id": "s", "stage": "STRATEGY", "gate": "g",
                                                  "reason": "r", "outcome": "NO_SETUP"})
        self.assertEqual(call(dashboard(repo), "/api/candidates/summary", "days=7")["evaluations"], 12)
        grouped = summarize_candidate_counts([{"symbol": "A", "strategy_id": "s", "stage": "S", "gate": "g",
                                               "reason": "r", "outcome": "NO_SETUP", "n": 7},
                                              {"symbol": "A", "strategy_id": "s", "stage": "S", "gate": "g",
                                               "reason": "r", "outcome": "CANDIDATE", "n": 2}])
        self.assertEqual(grouped["evaluations"], 9)
        self.assertEqual(grouped["groups"][0]["outcomes"], {"NO_SETUP": 7, "CANDIDATE": 2})

    def test_the_integrity_panel_says_when_the_only_gated_rule_is_switched_off(self):
        r = call(dashboard(MemoryRepo()), "/api/integrity")["research"]
        self.assertIn("micro_scalper", r["gated"])
        self.assertEqual(r["gated_disabled"], ["micro_scalper"])


class TestResearchCosts(unittest.TestCase):
    def test_the_backtester_is_handed_commission_in_money(self):
        """A price distance read as money booked gold's commission 100x too small, EURUSD's 100,000x."""
        from app.research.lab import worker
        cfg = SimpleNamespace(cost=SimpleNamespace(commission_per_lot_round_trip=7.0,
                                                   symbol_commission_per_lot_round_trip={}))
        self.assertEqual(worker._commission_money(cfg, "EURUSDm"), 7.0)
        self.assertAlmostEqual(worker._commission_price(cfg, "EURUSDm", SimpleNamespace(tick_value=1.0,
                                                                                         tick_size=1e-5)), 7e-5)
        import inspect
        src = inspect.getsource(worker)
        self.assertRegex(src, r"_research_every_strategy\(\s*cfgm, sym, m15, h1_all, spec, _commission_money\(")

    def test_trade_metrics_carry_the_dispersion_the_selector_reads(self):
        self.assertAlmostEqual(trade_metrics([1.0, -1.0, 1.0, -1.0])["sd_r"], 1.0)

    def test_the_cost_model_uses_the_symbols_own_commission(self):
        cfg = ConfigManager().app.cost.model_copy(update={"commission_per_lot_round_trip": 7.0,
                                                          "symbol_commission_per_lot_round_trip": {"EURUSDm": 3.5}})
        plan = TradePlan(direction=Side.BUY, entry=2000.0, sl=1990.0, tp=2020.0, exit_model={}, setup_key="k",
                         quality=0.5, ttl_seconds=60)
        self.assertEqual(CostModel(cfg).estimate(plan, xau_spec("EURUSDm"), 0.2).commission_per_lot, 3.5)
        self.assertEqual(CostModel(cfg).estimate(plan, xau_spec("XAUUSDm"), 0.2).commission_per_lot, 7.0)

    def test_paper_trading_pays_commission_on_the_closing_deal(self):
        sim = SimMarket(synthetic_bars(600, seed=1), balance=10000.0)
        sim.seek(500)
        pb = PaperBroker(sim, 10000.0, 0.0, commission_per_lot=lambda s: 7.0)
        tick = sim.tick(sim.symbol)
        pos = Position(ticket=5, symbol=sim.symbol, side=Side.BUY, volume=0.5, price_open=tick.ask, sl=0.0, tp=0.0,
                       price_current=tick.bid, profit=0.0, swap=0.0, magic=1, comment="", time=0)
        pb._positions[pos.ticket] = pos
        self.assertTrue(pb.close_position(pos, 0.5, 30, 1, "test").ok)
        deal = pb._deals[-1]
        self.assertAlmostEqual(deal.commission, -3.5)
        self.assertAlmostEqual(pb.balance, 10000.0 + deal.profit - 3.5)

    def test_the_deployed_filter_threshold_is_the_one_the_unseen_data_judged(self):
        from tests.unit.test_cost_data_learning import synthetic_rows
        from app.ml.filter import train_strategy
        st = train_strategy(synthetic_rows(), MLFilterCfg())
        self.assertEqual(st["threshold"], st["test"]["threshold"])


class TestIntrabarPath(unittest.TestCase):
    """A bar that closed against the trade made its peak first (MT5's tester order), so its low is tested
    against the profit lock that peak armed; a bar that closed with the trade made its low first."""

    def run_bars(self, second_bar):
        model = with_execution_exits({"type": "fixed", "target_r": 10.0}, {"profit_giveback_fraction": 0.0})
        sim = SimulatedTrade(Side.BUY, 100.0, 99.0, 110.0, model, 0, 0.0, 0.0)
        self.assertIsNone(sim.step(100.0, 100.3, 99.9, 100.2, 0.0))
        return sim, sim.step(*second_bar, 0.0)

    def test_a_falling_bar_through_the_new_lock_is_stopped_there(self):
        sim, res = self.run_bars((100.9, 100.95, 100.40, 100.60))      # peak 0.95R arms a 0.55R lock at 100.55
        self.assertIsNotNone(res, "the old rule tested only the close, 100.60, and kept it open")
        self.assertEqual(res.exit_reason, "PROFIT_LOCK")
        self.assertAlmostEqual(res.exit_price, 100.55, places=6)

    def test_a_rising_bar_keeps_the_trade_and_raises_its_stop(self):
        sim, res = self.run_bars((100.40, 100.95, 100.40, 100.90))
        self.assertIsNone(res)
        self.assertAlmostEqual(sim.st.sl, 100.55, places=6)


class TestConfigRewrite(unittest.TestCase):
    def test_a_dashboard_toggle_keeps_the_notes_in_strategies_json(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            for name in ("config.json", "strategies.json"):
                shutil.copy(ROOT / "config" / name, tmp / name)
            ConfigManager(tmp / "config.json", tmp / "strategies.json").set_strategy_enabled(
                "pullback_v1", False, "test", "checking the rewrite")
            data = json.loads((tmp / "strategies.json").read_text(encoding="utf-8"))
            self.assertIn("_comment", data)
            micro = next(s for s in data["strategies"] if s["id"] == "micro_scalper")
            self.assertIn("disabled_reason", micro)
            self.assertFalse(next(s for s in data["strategies"] if s["id"] == "pullback_v1")["enabled"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestSessionReport(unittest.TestCase):
    def test_fill_slippage_is_keyed_by_the_trades_symbol_and_converted_to_basis_points(self):
        from app.reports_session import review
        repo = MemoryRepo()
        repo.insert("trades", {"id": "t1", "symbol": "EURUSDm", "state": "OPEN"})
        repo.insert("symbols", {"symbol": "EURUSDm", "point": 0.00001, "captured_at": NOW})
        repo.insert("execution_events", {"created_at": NOW, "event_type": "FILL", "trade_id": "t1",
                                         "slippage_points": 3.0, "price": 1.1})
        out = review(repo, NOW + timedelta(minutes=1), hours=8)
        self.assertEqual(out["slippage_by_symbol"], {"EURUSDm": 3.0})
        self.assertAlmostEqual(out["avg_slippage_bp"], round(3.0 * 0.00001 / 1.1 * 1e4, 2))


if __name__ == "__main__":
    unittest.main()
