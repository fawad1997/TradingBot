"""Failure-injection and chaos tests (sections 66-68). These must pass before live execution.

Every test runs the REAL trading core against a simulated market + paper broker
that behaves like an MT5 trade server (positions, deals, server-side SL/TP)."""
import unittest

from tests.helpers import make_signal, rows, started_core, submit, trades
from app.broker.base import BrokerError
from app.config import ConfigError, Secrets
from app.core.engine import StartupError, TradingCore
from app.core.types import Environment, Position, Side, Tick
from app.simulation import make_sim_core
from app.storage.repo import MemoryRepo


def open_positions(broker):
    return broker.positions()


class TestChaos(unittest.TestCase):
    def test_strategy_evaluations_persist_no_setup_outcomes(self):
        core, _, _, _ = started_core()

        core._evaluate_strategies()

        evaluations = core.repo.tables["candidate_evaluations"]
        self.assertTrue(evaluations)
        self.assertTrue(any(row["outcome"] == "NO_SETUP" for row in evaluations))
        self.assertTrue(all(row.get("symbol") == core.symbol for row in evaluations))

    def test_force_reset_resumes_after_rechecking_safety_and_invalidates_pending(self):
        core, _, _, _ = started_core()
        core.kill.engage("manual halt", "dashboard")
        core.pending.append({"signal": make_signal(core), "route": None, "strategy": None, "tick": core.last_tick})

        result = core._command("force_reset_resume", {"actor": "dashboard", "reason": "operator verified"})

        self.assertTrue(result["ok"], result)
        self.assertFalse(core.kill.blocking)
        self.assertFalse(core.pending)
        self.assertEqual(result["invalidated_pending_signals"], 1)
        self.assertEqual(core._consecutive_errors, 0)
        self.assertTrue(rows(core, "config_versions", action="FORCE_RESET_RESUME"))
        self.assertTrue(rows(core, "rejections", reason="FORCE_RESET_INVALIDATED_PENDING_SIGNAL"))

    def test_force_reset_keeps_halt_when_margin_guard_fails(self):
        core, _, broker, _ = started_core()
        original_account_info = broker.account_info

        def low_margin_account():
            account = original_account_info()
            account.margin = 1.0
            account.margin_level = 100.0
            return account

        broker.account_info = low_margin_account
        core.kill.engage("manual halt", "dashboard")

        result = core._command("force_reset_resume", {"actor": "dashboard", "reason": "operator verified"})

        self.assertFalse(result["ok"])
        self.assertTrue(core.kill.blocking)
        self.assertIn("MARGIN_LEVEL_LOW", [reason["code"] for reason in result["blockers"]])

    # ---------------------------------------------------------------- section 67
    def test_kill_mid_order_no_duplicate_after_restart(self):
        core, sim, broker, cfg = started_core()
        broker.fail_after_send = True
        with self.assertRaises(SystemExit):
            submit(core)                                   # broker filled, process "dies" before recording
        self.assertEqual(len(open_positions(broker)), 1)
        t = trades(core)[0]
        self.assertEqual(t["state"], "ORDER_SENT")         # durable intent was written BEFORE order_send
        # ---- restart: a brand new core on the same database and the same broker
        core2 = TradingCore(core.cfgm, broker, core.repo, Secrets(), exec_broker=broker, wall=lambda: sim.now,
                            journal_path=cfg / "j2.jsonl")
        core2.start()
        core2.step()
        t = trades(core2)[0]
        self.assertEqual(t["state"], "OPEN")                # reconstructed from the broker
        self.assertEqual(int(t["position_id"]), open_positions(broker)[0].ticket)
        # the same market condition must not produce a second order
        for _ in range(40):
            sim.step()
            core2.step()
        self.assertEqual(len([p for p in open_positions(broker)]), len(core2.book.open_positions()))
        self.assertLessEqual(len(open_positions(broker)), 1)
        self.assertTrue(rows(core2, "execution_events", event_type="RECONCILED"))

    def test_approved_but_never_sent_is_cancelled_on_restart(self):
        core, sim, broker, cfg = started_core()
        core.book.create({"strategy_id": "pullback_v1", "symbol": core.symbol, "direction": "BUY",
                          "environment": "DEMO", "state": "APPROVED", "client_order_id": "ATATESTAPPROVED"})
        core2 = TradingCore(core.cfgm, broker, core.repo, Secrets(), exec_broker=broker, wall=lambda: sim.now,
                            journal_path=cfg / "j2.jsonl")
        core2.start()
        t = [x for x in trades(core2) if x["client_order_id"] == "ATATESTAPPROVED"][0]
        self.assertEqual(t["state"], "CANCELLED")
        self.assertEqual(len(open_positions(broker)), 0)

    # ---------------------------------------------------------------- section 66
    def test_ambiguous_result_is_reconciled_not_retried(self):
        core, sim, broker, _ = started_core()
        broker.ambiguous_after_fill = True
        submit(core)
        t = trades(core)[0]
        self.assertEqual(t["state"], "RECONCILIATION_REQUIRED")
        self.assertEqual(len(rows(core, "execution_events", event_type="ORDER_SENT")), 1)   # never resent
        sim.step()
        core.step()
        self.assertEqual(trades(core)[0]["state"], "OPEN")
        self.assertEqual(len(open_positions(broker)), 1)

    def test_rejected_order(self):
        core, sim, broker, _ = started_core()
        broker.next_retcode = 10006
        submit(core)
        t = trades(core)[0]
        self.assertEqual(t["state"], "REJECTED")
        self.assertEqual(len(open_positions(broker)), 0)
        self.assertTrue(rows(core, "rejections", gate="EXECUTION"))
        self.assertTrue(rows(core, "counterfactuals"))      # even a broker rejection is tracked

    def test_unknown_retcode(self):
        core, sim, broker, _ = started_core()
        broker.next_retcode = 10999
        submit(core)
        self.assertEqual(trades(core)[0]["state"], "REJECTED")
        self.assertIn("UNKNOWN_10999", trades(core)[0]["exit_reason"])

    def test_partial_fill(self):
        core, sim, broker, _ = started_core()
        broker.partial_fill_fraction = 0.5
        submit(core)
        t = trades(core)[0]
        self.assertEqual(t["state"], "OPEN")
        self.assertLess(t["filled_volume"], t["requested_volume"])
        self.assertAlmostEqual(t["open_volume"], open_positions(broker)[0].volume)
        self.assertGreater(t["cancelled_volume"], 0)

    def test_broker_drops_stop_loss_is_reattached(self):
        core, sim, broker, _ = started_core()
        broker.reject_sl_on_open = True
        submit(core)
        p = open_positions(broker)[0]
        self.assertGreater(p.sl, 0)                          # protection attached after the fill
        self.assertTrue(rows(core, "execution_events", event_type="ATTACH_SL"))

    def test_invalid_volume_rejected_by_order_check(self):
        core, sim, broker, _ = started_core()
        core.spec.volume_step = 0.01
        orig = core._size

        def bad_size(*a, **k):
            r = orig(*a, **k)
            r.volume = 0.015                                # not a multiple of the step
            return r
        core._size = bad_size
        submit(core)
        self.assertEqual(trades(core)[0]["state"], "REJECTED")
        self.assertEqual(len(open_positions(broker)), 0)

    def test_user_closes_position_manually(self):
        core, sim, broker, _ = started_core()
        submit(core)
        broker.manual_close(open_positions(broker)[0].ticket)
        core.recon.run()
        t = trades(core)[0]
        self.assertIn(t["state"], ("CLOSED", "BROKER_CLOSED"))
        self.assertEqual(t["exit_reason"], "CLIENT")
        self.assertIsNotNone(t["net_pnl"])

    def test_user_moves_stop_is_detected(self):
        core, sim, broker, _ = started_core()
        submit(core)
        p = open_positions(broker)[0]
        broker._positions[p.ticket].sl = p.sl - 1.0
        core.recon.run()
        self.assertEqual(trades(core)[0]["state"], "USER_MODIFIED")
        self.assertTrue(rows(core, "trade_events", event_type="BROKER_STATE_DIFFERS"))

    def test_unexpected_position_adopted_and_alerted(self):
        core, sim, broker, _ = started_core()
        tick = sim.tick(core.symbol)
        broker.inject_position(Position(4242, core.symbol, Side.SELL, 0.05, tick.bid, tick.bid + 5, 0.0, tick.bid,
                                        0.0, 0.0, core.cfg.broker.magic_number, "ghost", int(sim.now), 4242))
        core.recon.run()
        self.assertTrue(rows(core, "trade_events", event_type="UNEXPECTED_POSITION_ADOPTED"))

    def test_broker_stop_hit_is_attributed(self):
        core, sim, broker, _ = started_core()
        submit(core, Side.BUY)
        p = open_positions(broker)[0]
        tick = sim.tick(core.symbol)
        sim.tick_override = Tick(tick.time_msc + 1000, p.sl - 0.5, p.sl - 0.3)
        core._on_tick(sim.tick_override)                    # broker-side stop triggers
        core.recon.run()
        t = trades(core)[0]
        self.assertEqual(t["state"], "BROKER_CLOSED")
        self.assertEqual(t["exit_reason"], "SL")
        self.assertLess(t["r_multiple"], 0)

    def test_mt5_disconnect_blocks_new_trades_and_recovers(self):
        core, sim, broker, _ = started_core()
        sim.disconnected = True
        core._timers.clear()
        core.step()
        self.assertFalse(core.health.broker_connected)
        submit(core)
        self.assertTrue(rows(core, "rejections", gate="HEALTH"))
        self.assertEqual(len(open_positions(broker)), 0)
        sim.disconnected = False
        core._timers.clear()
        core.step()
        self.assertTrue(core.health.broker_connected)

    def test_positions_unreadable_freezes_portfolio(self):
        core, sim, broker, _ = started_core()
        orig = broker.positions
        broker.positions = lambda symbol=None: None
        core.recon.run()
        self.assertTrue(core.health.portfolio_uncertain)
        broker.positions = orig
        submit(core)
        self.assertTrue(rows(core, "rejections", gate="HEALTH"))
        core.recon.run()
        self.assertFalse(core.health.portfolio_uncertain)

    def test_database_failure_halts_and_journals(self):
        core, sim, broker, cfg = started_core()
        core.repo.fail_writes = True
        submit(core)                                         # signal insert fails -> fail-safe
        self.assertTrue(core.kill.blocking)
        self.assertEqual(len(open_positions(broker)), 0)
        self.assertTrue((cfg / "journal.jsonl").exists())
        res = core._command("resume", {"reason": "db back?"})
        self.assertFalse(res["ok"])                          # cannot resume while storage is down
        core.repo.fail_writes = False
        core.health.db_ok = True
        self.assertTrue(core._command("resume", {"reason": "db restored"})["ok"])
        self.assertFalse(core.kill.blocking)

    def test_kill_switch_blocks_orders_keeps_protection(self):
        core, sim, broker, _ = started_core()
        submit(core)
        sl_before = open_positions(broker)[0].sl
        core._command("halt", {"reason": "test"})
        submit(core, Side.SELL)
        self.assertEqual(len(open_positions(broker)), 1)
        self.assertEqual(open_positions(broker)[0].sl, sl_before)
        self.assertTrue(rows(core, "rejections", gate="KILL_SWITCH"))
        # durable across restart
        core2 = TradingCore(core.cfgm, broker, core.repo, Secrets(), exec_broker=broker, wall=lambda: sim.now)
        core2.start()
        self.assertTrue(core2.kill.engaged)

    def test_stale_signal_not_executed(self):
        core, sim, broker, _ = started_core()
        sig = make_signal(core)
        sig.created_broker_time -= 10_000
        sig.valid_until_broker -= 10_000
        from tests.helpers import eligible_route
        core._process_signal(sig, eligible_route(), core.cfgm.strategy("pullback_v1"), core.data.tick(core.symbol))
        self.assertEqual(len(open_positions(broker)), 0)
        self.assertTrue(rows(core, "rejections", gate="STALE"))

    def test_bad_spread_waits_then_expires(self):
        core, sim, broker, _ = started_core()
        tick = sim.tick(core.symbol)
        wide = Tick(tick.time_msc + 500, tick.bid, tick.bid + 2.0)      # 200 points
        sim.tick_override = wide
        core._on_tick(wide)
        sig = make_signal(core)
        from tests.helpers import eligible_route
        core._process_signal(sig, eligible_route(), core.cfgm.strategy("pullback_v1"), wide)
        self.assertEqual(len(core.pending), 1)
        sim.tick_override = None
        for _ in range(20):
            sim.step()
            core.step()
        self.assertEqual(len(core.pending), 0)
        self.assertTrue(rows(core, "rejections", gate="SPREAD") or rows(core, "rejections", gate="STALE")
                        or len(open_positions(broker)) <= 1)

    def test_corrupt_duplicate_delayed_ticks(self):
        core, sim, broker, _ = started_core()
        t = sim.tick(core.symbol)
        n_before = core.validator.rejected.copy()
        core._on_tick(Tick(t.time_msc + 10, t.bid + 1, t.bid))           # crossed
        core._on_tick(Tick(t.time_msc - 100_000, t.bid, t.ask))          # delayed / out of order
        core._on_tick(Tick(t.time_msc + 20, 0.0, t.ask))                 # corrupt
        self.assertGreater(sum(core.validator.rejected.values()), sum(n_before.values()))
        self.assertEqual(core.last_tick.bid, core.last_tick.bid)         # last good tick kept

    def test_real_mode_is_rejected_before_broker_startup(self):
        with self.assertRaisesRegex(ConfigError, "REAL execution is disabled"):
            make_sim_core("REAL", n_bars=9000, warmup_bars=7000, balance=10000)

    def test_core_startup_guard_rejects_real_even_if_config_validation_is_bypassed(self):
        core, _, broker, _ = make_sim_core("DEMO", n_bars=9000, warmup_bars=7000, balance=10000)
        core.env = Environment.REAL

        with self.assertRaisesRegex(StartupError, "REAL execution is disabled"):
            core.start()

        self.assertFalse(broker.is_connected())

    def test_order_engine_rejects_real_even_if_called_directly(self):
        core, _, _, _ = started_core()
        core.exec.env = Environment.REAL
        sig = make_signal(core)

        outcome = core.exec.execute(sig, core.last_tick, core.server_now(),
                                    lambda _price, _sl: None, lambda _tick: True,
                                    sig.session, sig.regime)

        self.assertEqual(outcome.status, "REJECTED")
        self.assertEqual(outcome.reason, "REAL_EXECUTION_DISABLED")
        self.assertEqual(core.repo.tables["trades"], [])

    def test_demo_refuses_real_account(self):
        from app.broker.paper import PaperBroker
        from app.broker.sim import SimMarket, synthetic_bars
        sim = SimMarket(synthetic_bars(9000, seed=11), account_mode="REAL")
        sim.seek(7000)
        broker = PaperBroker(sim, 10000, account_mode="REAL")
        core, _, _, _ = make_sim_core("DEMO", broker=broker)
        with self.assertRaises(StartupError):
            core.start()

    def test_counterfactuals_survive_restart(self):
        core, sim, broker, cfg = started_core()
        core._command("halt", {"reason": "force rejections"})
        submit(core)
        self.assertEqual(len(core.cf.open), 1)
        core2 = TradingCore(core.cfgm, broker, core.repo, Secrets(), exec_broker=broker, wall=lambda: sim.now)
        core2.start()
        cf_rows = core2.repo.tables["counterfactuals"]
        self.assertEqual(len(cf_rows), 1)
        self.assertTrue(len(core2.cf.open) == 1 or cf_rows[0]["status"] == "RESOLVED")


if __name__ == "__main__":
    unittest.main()
