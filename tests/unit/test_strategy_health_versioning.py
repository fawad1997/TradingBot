import unittest
from dataclasses import replace
from types import SimpleNamespace

from app.config import ConfigManager
from app.core.engine import TradingCore
from app.core.types import Environment, StrategyHealth, SymbolSpec
from app.governance.health import StrategyHealthMonitor, strategy_health_key
from app.regime.detector import RegimeState
from app.regime.router import route
from app.storage.repo import MemoryRepo
from app.broker.sim import xau_spec


def strategy(version):
    return SimpleNamespace(
        id="micro_scalper",
        version=version,
        status="DEMO",
        enabled=True,
        allowed_regimes=[],
        stats={},
    )


class TestStrategyHealthVersioning(unittest.TestCase):
    def make_core(self, repository):
        core = TradingCore.__new__(TradingCore)
        core.cfgm = ConfigManager()
        configured = core.cfgm.strategy("micro_scalper")
        old_version = configured.model_copy(update={"version": "1.0.0"})
        core.cfgm.strategies.strategies = [old_version, configured]
        core.repo = repository
        core.shealth = StrategyHealthMonitor()
        core._strategy_stats = {}
        self.current_version = configured.version
        core._every = lambda name, seconds: False
        core._alert = lambda level, text: None
        return core

    def test_new_version_does_not_inherit_old_version_restriction(self):
        repo = MemoryRepo()
        repo.insert_many("trades", [
            {
                "strategy_id": "micro_scalper",
                "strategy_version": "1.0.0",
                "state": "CLOSED",
                "closed_at": index,
                "r_multiple": -1.0,
            }
            for index in range(50)
        ])
        core = self.make_core(repo)

        core._refresh_strategy_stats()

        old_key = strategy_health_key("micro_scalper", "1.0.0")
        new_key = strategy_health_key("micro_scalper", self.current_version)
        self.assertEqual(core.shealth.state[old_key], StrategyHealth.RESTRICTED)
        self.assertEqual(core.shealth.state[new_key], StrategyHealth.HEALTHY)
        self.assertEqual(core._strategy_stats["micro_scalper"]["version"], self.current_version)
        self.assertEqual(core._strategy_stats["micro_scalper"]["trades"], 0)

        old_route = route(
            [strategy("1.0.0")],
            RegimeState(),
            Environment.DEMO,
            core.shealth.state,
            shadow_enabled=False,
        )
        new_route = route(
            [strategy(self.current_version)],
            RegimeState(),
            Environment.DEMO,
            core.shealth.state,
            shadow_enabled=False,
        )
        self.assertFalse(old_route["micro_scalper"].executable)
        self.assertTrue(new_route["micro_scalper"].executable)

    def test_unavailable_trade_history_fails_closed(self):
        class FailingRepo(MemoryRepo):
            def select(self, table, where=None, order_by=None, desc=False, limit=None):
                raise ConnectionError("database unavailable")

        core = self.make_core(FailingRepo())
        core._refresh_strategy_stats()

        self.assertEqual(
            core.shealth.state[strategy_health_key("micro_scalper", self.current_version)],
            StrategyHealth.RESTRICTED,
        )
        self.assertEqual(
            core._strategy_stats["micro_scalper"]["health_note"],
            "closed-trade health history unavailable",
        )


class TestOpenRiskReservation(unittest.TestCase):
    def make_core(self, row):
        xng_spec = replace(
            xau_spec(),
            name="XNGUSDm",
            digits=4,
            point=0.0001,
            tick_size=0.0001,
            tick_value=1.0,
            tick_value_loss=1.0,
            tick_value_profit=1.0,
            contract_size=10000.0,
        )
        core = TradingCore.__new__(TradingCore)
        core.repo = MemoryRepo()
        core.repo.insert("trades", row)
        core.positions_cache = []
        core.symbol = "GBPUSDm"
        core.spec = xng_spec
        core.data = SimpleNamespace(symbol_spec=lambda _symbol: xng_spec)
        return core

    def test_unfilled_order_reserves_risk_from_requested_entry_not_zero(self):
        core = self.make_core({
            "id": "ambiguous-order",
            "symbol": "XNGUSDm",
            "direction": "SELL",
            "state": "RECONCILIATION_REQUIRED",
            "entry_price": None,
            "entry_requested": 3.3687,
            "sl": 3.3776,
            "initial_sl": 3.3776,
            "requested_volume": 0.05,
            "filled_volume": None,
            "open_volume": None,
        })

        self.assertAlmostEqual(core._open_risk_money(), 4.45)

    def test_unknown_active_trade_risk_blocks_new_entries(self):
        core = self.make_core({
            "id": "missing-entry-order",
            "symbol": "XNGUSDm",
            "direction": "SELL",
            "state": "RECONCILIATION_REQUIRED",
            "entry_price": None,
            "entry_requested": None,
            "sl": 3.3776,
            "initial_sl": 3.3776,
            "requested_volume": 0.05,
        })

        self.assertEqual(core._open_risk_money(), float("inf"))


if __name__ == "__main__":
    unittest.main()
