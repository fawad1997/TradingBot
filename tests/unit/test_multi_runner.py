import unittest
from types import SimpleNamespace

from app.core.bridge import Bridge
from app.core.engine import StartupError
from app.core.multi import MultiAssetRunner


class FakeCore:
    def __init__(self, symbol, runner, fail_start=False):
        self.symbol = symbol
        self.runner = runner
        self.fail_start = fail_start
        self.data = SimpleNamespace(is_connected=lambda: True, shutdown=lambda: None)
        self.pending = []
        self._consecutive_errors = 0
        self.steps = 0
        self.stopped = False

    def start(self):
        if self.fail_start:
            raise StartupError("symbol history unavailable")

    def step(self):
        self.steps += 1
        if self.symbol == "GBPUSDm":
            self.runner.stop_event.set()

    def shutdown(self):
        self.stopped = True


class TestMultiAssetRunner(unittest.TestCase):
    def test_scans_each_available_symbol_and_skips_unavailable_symbols(self):
        bridge = Bridge()
        runner = None
        created = []

        def factory(symbol):
            core = FakeCore(symbol, runner, fail_start=symbol == "BTCUSDm")
            created.append(core)
            return core

        runner = MultiAssetRunner(None, ["XAUUSDm", "BTCUSDm", "GBPUSDm"], factory, bridge)
        runner.start()
        self.assertEqual([core.symbol for core in runner.cores], ["XAUUSDm", "GBPUSDm"])
        self.assertTrue(all(core.portfolio_cores is runner.cores for core in runner.cores))
        market_status = bridge.get_state()["symbol_market_status"]
        self.assertEqual(set(market_status), {"XAUUSDm", "GBPUSDm"})
        self.assertTrue(all(status["schedule_type"] == "estimated_weekend_hours"
                            for status in market_status.values()))
        runner.run()
        self.assertEqual([core.steps for core in runner.cores], [1, 1])
        self.assertTrue(all(core.stopped for core in runner.cores))


if __name__ == "__main__":
    unittest.main()
