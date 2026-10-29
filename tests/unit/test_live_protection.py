import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.alpha.exits import OpenTradeState
from app.core.engine import TradingCore
from app.core.types import OrderResult, Side, Tick
from app.execution.book import TradeBook
from app.execution.trade_manager import TradeManager


class TestLiveProtection(unittest.TestCase):
    def test_exit_models_keep_brackets_and_enable_half_r_breakeven(self):
        path = Path(__file__).resolve().parents[2] / "config" / "strategies.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for strategy in data["strategies"]:
            exit_model = strategy["exit_model"]
            if strategy["id"] in ("trend_daily_v1", "trend_ensemble_v1"):
                # The deliberate exception: daily trend following trails a 3 x daily-ATR chandelier and must not
                # bank early - in its decade replay the account's lock cut gold from +0.35R to +0.07R a trade.
                self.assertEqual(exit_model["type"], "chandelier")
                self.assertFalse(exit_model["breakeven"]["enabled"])
                self.assertFalse(exit_model["profit_lock"]["enabled"])
                self.assertGreater(exit_model["target_r"], 0, "the broker requires a take-profit")
                continue
            if strategy["id"] == "turn_of_month_v1":
                # A calendar trade: it ends on the time stop after four trading days, not on the path
                self.assertEqual(exit_model["type"], "fixed")
                self.assertTrue(exit_model["time_stop"]["enabled"])
                self.assertFalse(exit_model["breakeven"]["enabled"])
                self.assertFalse(exit_model["profit_lock"]["enabled"])
                self.assertGreater(exit_model["target_r"], 0, "the broker requires a take-profit")
                continue
            self.assertEqual(exit_model["type"], "fixed")
            self.assertTrue(exit_model["breakeven"]["enabled"])
            self.assertEqual(exit_model["breakeven"]["activate_r"], 0.5)
            if strategy["id"] == "pullback_v1":
                self.assertEqual(exit_model["target_r"], 1.0)
            if strategy["id"] == "micro_scalper":
                self.assertEqual(strategy["params"]["target_atr_multiple"]["value"],
                                 strategy["params"]["stop_atr_multiple"]["value"])

    def test_breakeven_stop_moves_after_half_r_and_covers_spread(self):
        state = OpenTradeState(Side.BUY, entry=100.0, initial_sl=99.0, sl=99.0, tp=101.0,
                               volume=0.01, initial_volume=0.01, entry_time=1)
        trade = {"position_id": 1,
                 "exit_model": {"breakeven": {"enabled": True, "activate_r": 0.5, "cover_spread": True},
                                "partial": {"enabled": False}}}
        book = SimpleNamespace(open_positions=lambda: [trade], ensure_mstate=lambda _: state)
        manager = TradeManager.__new__(TradeManager)
        manager.symbol = "EURUSDm"
        manager.broker = SimpleNamespace(positions=lambda _: [SimpleNamespace(ticket=1, sl=99.0, tp=101.0)])
        manager.book = book
        manager.spec_fn = lambda: SimpleNamespace(point=0.01, stops_level=0, freeze_level=0)
        manager.on_uncertain = Mock()
        manager.profit_trail_activation_money = 0.0
        manager.profit_lock_floor_money = 0.05
        manager.small_win_activation_money = 0.0
        manager.close = Mock(return_value=True)
        manager.modify_sl = Mock(return_value=True)
        manager._ensure_bracket = Mock(return_value=SimpleNamespace(
            ticket=1, sl=99.0, tp=101.0, profit=0.0, swap=0.0
        ))

        manager.on_tick(Tick(time_msc=1, bid=100.4, ask=100.5))
        manager.modify_sl.assert_not_called()

        manager.on_tick(Tick(time_msc=2, bid=100.6, ask=100.8))
        manager.modify_sl.assert_called_once_with(trade, 100.2, "BREAKEVEN_0.5R")

    def test_profit_lock_lets_winner_run_then_closes_at_floor(self):
        state = OpenTradeState(Side.BUY, entry=1.0, initial_sl=0.99, sl=0.99, tp=1.1,
                               volume=0.01, initial_volume=0.01, entry_time=1)
        trade = {"id": "trade-1", "position_id": 1,
                 "entry_price": 1.0, "direction": "BUY", "state": "OPEN", "extra": {},
                 "tp": 1.1,
                 "exit_model": {"breakeven": {"enabled": False}, "partial": {"enabled": False}}}
        book = SimpleNamespace(open_positions=lambda: [trade], ensure_mstate=lambda _: state)
        book.mstate = {"trade-1": state}
        broker = SimpleNamespace(positions=lambda _: [
            SimpleNamespace(ticket=1, symbol="EURUSDm", side=Side.BUY, price_open=1.0, sl=0.99,
                            tp=1.1, price_current=1.001, profit=0.11, swap=0.0, volume=0.01)
        ], calc_profit=lambda _symbol, _side, _volume, entry, price: (price - entry) * 100.0,
            modify_sltp=Mock(return_value=SimpleNamespace(
                ok=True, already_closed=False, ambiguous=False, retcode=10009,
                retcode_text="DONE", latency_ms=0,
                comment="")))
        manager = TradeManager.__new__(TradeManager)
        manager.symbol = "EURUSDm"
        manager.broker = broker
        manager.book = book
        manager.spec_fn = lambda: SimpleNamespace(point=0.00001, tick_size=0.00001, digits=5,
                                                   round_price=lambda price: round(price, 5),
                                                   stops_level=0, freeze_level=0)
        manager.on_uncertain = Mock()
        manager.profit_trail_activation_money = 0.10
        manager.profit_lock_floor_money = 0.05
        manager.close = Mock(return_value=True)
        manager.book.exec_event = Mock()
        manager.book.transition = Mock()
        manager.book.update = Mock(side_effect=lambda _trade, **changes: trade.update(changes))
        manager.recon = SimpleNamespace()
        manager._ensure_bracket = Mock(side_effect=lambda _: broker.positions("EURUSDm")[0])

        manager.on_tick(Tick(time_msc=1, bid=1.001, ask=1.0011))
        manager.close.assert_not_called()
        broker.modify_sltp.assert_called_once()
        requested_sl = broker.modify_sltp.call_args.args[2]
        self.assertAlmostEqual(requested_sl, 1.00055, places=5)
        self.assertGreaterEqual(
            broker.calc_profit("EURUSDm", Side.BUY, 0.01, 1.0, requested_sl),
            0.055,
        )

        broker.positions = lambda _: [
            SimpleNamespace(ticket=1, symbol="EURUSDm", side=Side.BUY, price_open=1.0, sl=0.99,
                            tp=1.1, price_current=1.0005, profit=0.0505, swap=0.0, volume=0.01)
        ]
        manager.on_tick(Tick(time_msc=2, bid=1.0005, ask=1.0006))
        manager.close.assert_called_once_with(trade, 1.0, "SMALL_WIN_PROFIT_FLOOR")

    def _profit_lock_rejection_case(self, rejected_retcode, fresh_profit, market_bid=1.001,
                                    active=True, initial_profit=0.11, commission_per_lot=0.0,
                                    close_unprotectable=False):
        state = OpenTradeState(Side.BUY, entry=1.0, initial_sl=0.99, sl=0.99, tp=1.1,
                               volume=0.01, initial_volume=0.01, entry_time=1,
                               profit_lock_active=active)
        trade = {"id": "trade-1", "position_id": 1, "entry_price": 1.0,
                 "direction": "BUY", "state": "OPEN", "extra": {}, "tp": 1.1,
                 "exit_model": {"breakeven": {"enabled": False}, "partial": {"enabled": False}}}
        initial_pos = SimpleNamespace(
            ticket=1, symbol="EURUSDm", side=Side.BUY, price_open=1.0, sl=0.99,
            tp=1.1, price_current=market_bid, profit=initial_profit, swap=0.0, volume=0.01,
        )
        fresh_pos = SimpleNamespace(
            ticket=1, symbol="EURUSDm", side=Side.BUY, price_open=1.0, sl=0.99,
            tp=1.1, price_current=1.001, profit=fresh_profit, swap=0.0, volume=0.01,
        )
        broker = SimpleNamespace(
            positions=Mock(return_value=[fresh_pos]),
            calc_profit=lambda _symbol, _side, _volume, entry, price: (price - entry) * 100.0,
            modify_sltp=Mock(return_value=SimpleNamespace(
                ok=False, already_closed=False, ambiguous=False, retcode=rejected_retcode,
                retcode_text="INVALID_STOPS" if rejected_retcode == 10016 else "ERROR",
                comment="bad stops", latency_ms=0,
            )),
        )
        manager = TradeManager.__new__(TradeManager)
        manager.symbol = "EURUSDm"
        manager.broker = broker
        manager.book = SimpleNamespace(
            open_positions=lambda: [trade], ensure_mstate=lambda _: state,
            exec_event=Mock(), transition=Mock(), update=Mock(),
        )
        manager.spec_fn = lambda: SimpleNamespace(
            point=0.00001, tick_size=0.00001, digits=5, round_price=lambda price: round(price, 5),
            stops_level=0, freeze_level=0,
        )
        manager.on_uncertain = Mock()
        manager.profit_trail_activation_money = 0.10
        manager.profit_lock_floor_money = 0.05
        manager.slippage_points = 5.0
        manager.commission_per_lot_round_trip = commission_per_lot
        manager.profit_lock_stop_buffer_points = 3.0
        manager.close_when_profit_lock_unprotectable = close_unprotectable
        manager.close = Mock(return_value=True)
        manager._ensure_bracket = Mock(return_value=initial_pos)
        manager.on_tick(Tick(time_msc=1, bid=market_bid, ask=market_bid + 0.0001))
        return manager, state, trade

    def test_failed_profit_lock_move_keeps_the_winner_by_default(self):
        # the existing broker stop still protects the position, so a failed tightening must not cut a winner short
        manager, state, trade = self._profit_lock_rejection_case(10016, 0.11)
        manager.close.assert_not_called()
        self.assertEqual(state.sl, 0.99)

    def test_failed_profit_lock_move_closes_when_the_operator_opts_in(self):
        manager, _, trade = self._profit_lock_rejection_case(10016, 0.11, close_unprotectable=True)
        manager.close.assert_called_once_with(trade, 1.0, "DYNAMIC_PROFIT_FLOOR_MODIFY_FAILED")

    def test_profit_lock_stop_error_below_net_floor_retains_protective_stop(self):
        manager, state, trade = self._profit_lock_rejection_case(10011, 0.04)
        manager.close.assert_not_called()
        self.assertEqual(state.sl, 0.99)
        manager.on_uncertain.assert_not_called()

    def test_profit_lock_uses_buffer_beyond_broker_stop_distance(self):
        manager, state, trade = self._profit_lock_rejection_case(10016, 0.11, market_bid=1.00057)
        manager.broker.modify_sltp.assert_not_called()
        manager.close.assert_not_called()
        self.assertEqual(state.sl, 0.99)

    def test_profit_lock_activation_accounts_for_configured_exit_costs(self):
        manager, state, _ = self._profit_lock_rejection_case(
            10016, 0.15, active=False, initial_profit=0.15, commission_per_lot=10.0
        )
        self.assertFalse(state.profit_lock_active)
        manager.broker.modify_sltp.assert_not_called()
        manager.close.assert_not_called()

    def _hard_profit_target_case(self, floating_profit):
        state = OpenTradeState(Side.BUY, entry=1.0, initial_sl=0.99, sl=0.99, tp=1.1,
                               volume=0.01, initial_volume=0.01, entry_time=1)
        trade = {"id": "trade-1", "position_id": 1, "entry_price": 1.0,
                 "direction": "BUY", "state": "OPEN", "extra": {}, "tp": 1.1,
                 "exit_model": {"breakeven": {"enabled": False}, "partial": {"enabled": False}}}
        position = SimpleNamespace(
            ticket=1, symbol="EURUSDm", side=Side.BUY, price_open=1.0, sl=0.99,
            tp=1.1, price_current=1.01, profit=floating_profit, swap=0.0, volume=0.01,
        )
        broker = SimpleNamespace(
            calc_profit=lambda _symbol, _side, _volume, entry, price: (price - entry) * 100.0,
        )
        manager = TradeManager.__new__(TradeManager)
        manager.symbol = "EURUSDm"
        manager.broker = broker
        manager.book = SimpleNamespace(
            open_positions=lambda: [trade], ensure_mstate=lambda _: state,
            update=Mock(side_effect=lambda _trade, **changes: trade.update(changes)),
        )
        manager.spec_fn = lambda: SimpleNamespace(
            point=0.00001, tick_size=0.00001, digits=5, round_price=lambda price: round(price, 5),
            stops_level=0, freeze_level=0,
        )
        manager.profit_trail_activation_money = 0.0
        manager.profit_lock_floor_money = 0.05
        manager.hard_profit_target_money = 1.0
        manager.commission_per_lot_round_trip = 20.0
        manager.slippage_points = 5.0
        manager.close = Mock(return_value=True)
        manager._ensure_bracket = Mock(return_value=position)
        manager._modify_sl_result = Mock(return_value=SimpleNamespace(
            ok=True, already_closed=False, ambiguous=False,
        ))
        manager.on_tick(Tick(time_msc=1, bid=1.01, ask=1.0101))
        return manager, trade

    def test_hard_profit_target_closes_at_estimated_net_one_currency_unit(self):
        manager, trade = self._hard_profit_target_case(1.205)
        manager.close.assert_called_once_with(trade, 1.0, "HARD_PROFIT_TARGET")

    def test_hard_profit_target_does_not_close_below_net_target(self):
        manager, _ = self._hard_profit_target_case(1.20)
        manager.close.assert_not_called()

    def _dynamic_profit_case(self, peak_profit, current_profit, risk_money=None, floor_r=None, hard_target=1.0,
                             giveback=0.0):
        state = OpenTradeState(Side.BUY, entry=1.0, initial_sl=0.99, sl=0.99, tp=1.1,
                               volume=0.01, initial_volume=0.01, entry_time=1)
        trade = {"id": "trade-1", "position_id": 1, "entry_price": 1.0,
                 "direction": "BUY", "state": "OPEN", "extra": {}, "tp": 1.1,
                 "exit_model": {"breakeven": {"enabled": False}, "partial": {"enabled": False}},
                 "initial_risk_money": risk_money}
        position = SimpleNamespace(
            ticket=1, symbol="EURUSDm", side=Side.BUY, price_open=1.0, sl=0.99,
            tp=1.1, price_current=1.01, profit=peak_profit, swap=0.0, volume=0.01,
        )
        broker = SimpleNamespace(
            calc_profit=lambda _symbol, _side, _volume, entry, price: (price - entry) * 100.0,
        )
        manager = TradeManager.__new__(TradeManager)
        manager.symbol = "EURUSDm"
        manager.broker = broker
        manager.book = SimpleNamespace(
            open_positions=lambda: [trade], ensure_mstate=lambda _: state,
            update=Mock(side_effect=lambda _trade, **changes: trade.update(changes)),
        )
        manager.spec_fn = lambda: SimpleNamespace(
            point=0.00001, tick_size=0.00001, digits=5, round_price=lambda price: round(price, 5),
            stops_level=0, freeze_level=0,
        )
        manager.profit_trail_activation_money = 0.0
        manager.profit_lock_floor_money = 0.05
        manager.small_win_activation_money = 0.20
        manager.small_win_floor_money = 0.05
        manager.ambition_activation_money = 0.90
        manager.ambition_floor_money = 0.50
        manager.hard_profit_target_money = hard_target
        manager.profit_giveback_fraction = giveback
        manager.commission_per_lot_round_trip = 0.0
        manager.slippage_points = 0.0
        manager.profit_lock_stop_buffer_points = 3.0
        manager.floor_r = floor_r or {}
        manager.close = Mock(return_value=True)
        manager._ensure_bracket = Mock(return_value=position)
        manager._modify_sl_result = Mock(return_value=SimpleNamespace(
            ok=True, already_closed=False, ambiguous=False,
        ))
        manager.on_tick(Tick(time_msc=1, bid=1.01, ask=1.0101))
        if current_profit is not None:
            position.profit = current_profit
            manager.on_tick(Tick(time_msc=2, bid=1.005, ask=1.0051))
        return manager, state, trade

    def test_small_win_peak_retraces_to_positive_floor_and_closes(self):
        manager, state, trade = self._dynamic_profit_case(0.30, 0.04)
        self.assertAlmostEqual(state.profit_high_watermark_money, 0.30)
        self.assertAlmostEqual(state.profit_lock_target_money, 0.05)
        self.assertEqual(trade["extra"]["profit_high_watermark_money"], 0.30)
        manager.close.assert_called_once_with(trade, 1.0, "SMALL_WIN_PROFIT_FLOOR")

    def test_ambition_peak_ratchets_floor_and_closes_above_small_floor(self):
        manager, state, trade = self._dynamic_profit_case(0.95, 0.54)
        self.assertAlmostEqual(state.profit_high_watermark_money, 0.95)
        self.assertAlmostEqual(state.profit_lock_target_money, 0.55)
        self.assertAlmostEqual(trade["extra"]["profit_lock_target_money"], 0.55)
        manager.close.assert_called_once_with(trade, 1.0, "AMBITION_PROFIT_FLOOR")

    R_LEVELS = {"small_win_activation": 0.5, "small_win_floor": 0.1, "ambition_activation": 0.8, "ambition_floor": 0.4}

    def test_r_levels_ignore_noise_sized_profits(self):
        # 14 Oct 00:31: a $20-risk gold trade touched +$0.38 (0.02R), the $0.20 trigger armed a $0.05 floor and the
        # trade was closed at -$1.48. Scaled to risk, +$0.30 on a $20-risk trade arms nothing.
        manager, state, _ = self._dynamic_profit_case(0.30, 0.04, risk_money=20.0, floor_r=self.R_LEVELS)
        manager.close.assert_not_called()
        self.assertFalse(state.profit_lock_active)

    def test_r_levels_lock_a_real_small_win(self):
        # 15 Oct: a 0.07-lot XAUUSDm trade (1R = $18.97) ran to about +$10 and gave it all back. $10 crosses the
        # small-win rung (0.5R) but not the ambition rung (0.8R = $15.18), and this tier used to lock a FLAT
        # small_win_floor_r 0.1 - $1.90 on that trade, $2.00 here. So every peak between 0.5R and 0.8R protected
        # a tenth of risk no matter how high it got. The small-win tier now trails the peak exactly as the
        # ambition tier already did, so peak $12 at giveback 0.3 locks $8.40 rather than $2.00.
        manager, state, trade = self._dynamic_profit_case(12.0, 1.5, risk_money=20.0, floor_r=self.R_LEVELS,
                                                                hard_target=0.0, giveback=0.3)
        self.assertAlmostEqual(state.profit_lock_target_money, 8.4)        # peak 12, give back 30%
        manager.close.assert_called_once_with(trade, 1.0, "SMALL_WIN_PROFIT_FLOOR")

    def test_r_levels_small_win_floor_still_applies_to_a_modest_peak(self):
        """The trail can only ever RAISE the floor: a peak barely above activation keeps the 0.1R minimum."""
        manager, state, trade = self._dynamic_profit_case(10.0, 1.0, risk_money=20.0, floor_r=self.R_LEVELS,
                                                                hard_target=0.0, giveback=0.9)
        self.assertAlmostEqual(state.profit_lock_target_money, 2.0)        # 0.1R of 20 beats 10 x 0.1
        manager.close.assert_called_once_with(trade, 1.0, "SMALL_WIN_PROFIT_FLOOR")

    def test_r_levels_trail_a_big_win(self):
        manager, state, trade = self._dynamic_profit_case(20.0, 13.0, risk_money=20.0, floor_r=self.R_LEVELS,
                                                                hard_target=0.0, giveback=0.3)
        self.assertAlmostEqual(state.profit_lock_target_money, 14.0)       # peak 20, give back 30%
        manager.close.assert_called_once_with(trade, 1.0, "AMBITION_PROFIT_FLOOR")

    def test_profit_floor_price_handles_sell_positions(self):
        manager = TradeManager.__new__(TradeManager)
        manager.spec_fn = lambda: SimpleNamespace(
            point=0.00001, tick_size=0.00001, digits=5, round_price=lambda price: round(price, 5))
        manager.broker = SimpleNamespace(
            calc_profit=lambda _symbol, side, _volume, entry, price:
                (entry - price) * 100.0 if side is Side.SELL else (price - entry) * 100.0)
        position = SimpleNamespace(
            symbol="EURUSDm", side=Side.SELL, volume=0.01, price_open=1.0, swap=0.0)

        self.assertAlmostEqual(manager._profit_floor_price(position, 0.05), 0.9995, places=4)

    def test_profit_lock_state_is_restored_after_restart(self):
        book = TradeBook.__new__(TradeBook)
        book.mstate = {}
        trade = {
            "id": "trade-1",
            "direction": "BUY",
            "entry_price": 1.0,
            "initial_sl": 0.99,
            "sl": 1.0005,
            "tp": 1.1,
            "filled_volume": 0.01,
            "opened_at": 1,
            "extra": {
                "profit_lock_active": True,
                "profit_lock_stop_price": 1.00051,
                "profit_lock_target_money": 0.55,
                "profit_high_watermark_money": 0.95,
            },
        }

        state = book.ensure_mstate(trade)

        self.assertTrue(state.profit_lock_active)
        self.assertEqual(state.profit_lock_stop, 1.00051)
        self.assertEqual(state.profit_lock_target_money, 0.55)
        self.assertEqual(state.profit_high_watermark_money, 0.95)

    def test_symbol_exposure_blocks_opposite_and_pending_positions_only_for_same_asset(self):
        core = TradingCore.__new__(TradingCore)
        core.symbol = "XNGUSDm"
        core.positions_cache = [SimpleNamespace(symbol="XNGUSDm"), SimpleNamespace(symbol="EURUSDm")]
        core.pending = []
        core.portfolio_cores = [core]
        self.assertTrue(core._symbol_exposure([]))
        core.positions_cache = [SimpleNamespace(symbol="EURUSDm")]
        self.assertFalse(core._symbol_exposure([{"symbol": "EURUSDm"}]))

        core.positions_cache = []
        core.pending = [{"signal": SimpleNamespace(symbol="XNGUSDm")}]
        self.assertEqual(core._symbol_exposure([])["pending_signals"], 1)

        core.pending = []
        exposure = core._symbol_exposure([{"symbol": "XNGUSDm", "direction": "SELL"}])
        self.assertEqual(exposure["active_trades"], 1)

    def test_execution_rejects_signal_without_stop_or_take_profit(self):
        from tests.helpers import started_core, make_signal

        core, _, broker, _ = started_core()
        signal = make_signal(core)
        signal.plan.tp = None
        signal.plan.exit_model = {}
        outcome = core.exec.execute(signal, core.last_tick, core.server_now(),
                                    resize=lambda *_: None, spread_ok=lambda _: True,
                                    session=signal.session, regime=signal.regime)
        self.assertEqual(outcome.reason, "MISSING_PROTECTIVE_BRACKET")
        self.assertEqual(outcome.trade["state"], "CANCELLED")
        self.assertEqual(broker.positions(core.symbol), [])

    def test_execution_rechecks_protection_against_quote_at_dispatch(self):
        from tests.helpers import started_core, make_signal

        core, _, broker, _ = started_core()
        signal = make_signal(core)
        spec = broker.symbol_spec(core.symbol)
        minimum_distance = (
            spec.stops_level + spec.freeze_level
        ) * spec.point + core.last_tick.spread + core.cfg.execution.protection_safety_buffer_points * spec.point
        late_bid = signal.plan.sl + minimum_distance / 2
        late_tick = Tick(time_msc=core.last_tick.time_msc + 1, bid=late_bid,
                         ask=late_bid + core.last_tick.spread)
        broker.tick = lambda _symbol: late_tick

        sizing = SimpleNamespace(feasible=True, volume=0.01, risk_money=1.0, risk_fraction=0.001)
        outcome = core.exec.execute(
            signal, core.last_tick, core.server_now(), resize=lambda *_: sizing,
            spread_ok=lambda _: True, session=signal.session, regime=signal.regime,
        )

        self.assertEqual(outcome.status, "CANCELLED")
        self.assertEqual(outcome.reason, "STOP_INVALID_AT_PRICE")
        self.assertEqual(broker.positions(core.symbol), [])
        self.assertTrue(any(event["event_type"] == "PROTECTION_PREFLIGHT"
                            for event in core.repo.tables["execution_events"]))

    def test_portfolio_reservation_runs_after_order_check_and_before_order_send(self):
        from tests.helpers import started_core, make_signal
        from app.risk.sizing import SizingResult

        core, _, broker, _ = started_core()
        signal = make_signal(core)
        assert core.exec is not None
        assert core.last_tick is not None
        assert core.exec.reserve_risk is not None
        self.assertIs(getattr(core.exec.reserve_risk, "__self__", None), core)
        sizing = SizingResult(True, 0.01, 1.0, 0.001, 100.0, 0.0, "")
        calls = []
        order_check = broker.order_check

        def record_check(request):
            calls.append("order_check")
            return order_check(request)

        def unexpected_send(_request):
            calls.append("order_send")
            return None

        def reject_reservation(_trade, risk):
            calls.append(("reserve", risk))
            return False, "PORTFOLIO_OPEN_RISK_LIMIT"

        setattr(broker, "order_check", record_check)
        setattr(broker, "order_send", unexpected_send)
        setattr(core.exec, "reserve_risk", reject_reservation)

        outcome = core.exec.execute(
            signal, core.last_tick, core.server_now(), resize=lambda *_: sizing,
            spread_ok=lambda _: True, session=signal.session, regime=signal.regime,
        )

        self.assertEqual(outcome.status, "REJECTED")
        self.assertEqual(outcome.reason, "PORTFOLIO_OPEN_RISK_LIMIT")
        self.assertEqual(calls, ["order_check", ("reserve", 1.0)])
        self.assertEqual(broker.positions(core.symbol), [])

    def test_missing_broker_take_profit_is_attached_after_fill(self):
        from tests.helpers import started_core, submit, trades

        core, _, broker, _ = started_core()
        send = broker.order_send

        def drop_take_profit(request):
            result = send(request)
            if result.ok:
                position = broker.positions(request.symbol)[0]
                broker.modify_sltp(request.symbol, position.ticket, position.sl, 0.0)
            return result

        broker.order_send = drop_take_profit
        submit(core)
        position = broker.positions(core.symbol)[0]
        self.assertGreater(position.sl, 0)
        self.assertGreater(position.tp, 0)
        self.assertTrue(any(event.get("requested_state", {}).get("missing_tp")
                            for event in core.repo.tables["execution_events"]
                            if event["event_type"] == "ATTACH_SL"))
        self.assertGreater(trades(core)[0]["tp"], 0)
        broker.modify_sltp(core.symbol, position.ticket, position.sl, 0.0)
        core.manager.on_tick(core.last_tick)
        self.assertGreater(broker.positions(core.symbol)[0].tp, 0)

    def test_sl_changes_apply_live_stop_distance_and_failure_cooldown(self):
        clock = [100.0]
        trade = {"id": "trade-1", "position_id": 42, "entry_price": 99.0,
                 "direction": "BUY", "state": "OPEN", "tp": 101.0}
        broker = SimpleNamespace(
            tick=Mock(return_value=Tick(time_msc=1, bid=100.0, ask=100.02)),
            modify_sltp=Mock(return_value=OrderResult(
                ok=False, retcode=10016, retcode_text="INVALID_STOPS", comment="bad stops")),
        )
        book = SimpleNamespace(mstate={}, exec_event=Mock(), transition=Mock())
        manager = TradeManager(
            broker, book, SimpleNamespace(), "EURUSDm", 1, 30,
            lambda: SimpleNamespace(
                point=0.01, stops_level=10, freeze_level=0,
                round_price=lambda price: round(price, 2),
            ),
            Mock(), protection_safety_buffer_points=5.0, sl_modification_cooldown_sec=5.0,
        )
        manager._clock = lambda: clock[0]

        invalid = manager._modify_sl_result(trade, 99.95, "TEST")
        self.assertEqual(invalid.retcode_text, "LOCAL_STOP_DISTANCE")
        broker.modify_sltp.assert_not_called()

        clock[0] += 5.1
        failed = manager._modify_sl_result(trade, 99.50, "TEST")
        self.assertEqual(failed.retcode, 10016)
        broker.modify_sltp.assert_called_once()

        clock[0] += 1.0
        throttled = manager._modify_sl_result(trade, 99.60, "TEST")
        # Our own spacing rule, not the broker's. It used to report MT5's 10024/THROTTLED, which made 55% of
        # all stop moves on 16 Oct look like Exness rate-limiting us when every one was self-imposed.
        self.assertEqual(throttled.retcode_text, "OWN_COOLDOWN")
        self.assertNotEqual(throttled.retcode, 10024, "10024 means the BROKER is throttling")
        broker.modify_sltp.assert_called_once()

        clock[0] += 4.1
        manager._modify_sl_result(trade, 99.60, "TEST")
        self.assertEqual(broker.modify_sltp.call_count, 2)

    def test_frozen_close_retries_are_deferred_for_cooldown(self):
        clock = [200.0]
        position = SimpleNamespace(ticket=77, volume=0.1)
        trade = {"id": "trade-2", "position_id": 77}
        broker = SimpleNamespace(
            positions=Mock(return_value=[position]),
            close_position=Mock(return_value=OrderResult(
                ok=False, retcode=10029, retcode_text="FROZEN", comment="position frozen")),
        )
        book = SimpleNamespace(exec_event=Mock())
        manager = TradeManager(
            broker, book, SimpleNamespace(), "EURUSDm", 1, 30,
            lambda: SimpleNamespace(), Mock(), sl_modification_cooldown_sec=5.0,
        )
        manager._clock = lambda: clock[0]

        self.assertFalse(manager.close(trade, 1.0, "TEST_EXIT"))
        clock[0] += 1.0
        self.assertFalse(manager.close(trade, 1.0, "TEST_EXIT"))
        broker.close_position.assert_called_once()

        clock[0] += 4.1
        manager.close(trade, 1.0, "TEST_EXIT")
        self.assertEqual(broker.close_position.call_count, 2)


if __name__ == "__main__":
    unittest.main()
