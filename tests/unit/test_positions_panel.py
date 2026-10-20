"""The live-positions panel must say the same thing whichever core published it, and in the symbol's own digits.

Two faults seen on 14 Oct within ten minutes, on the same open EURUSDm ticket:

  23:36   strategy vwap_reversion_v1   state PROTECTION
  23:46   strategy -                   state -

Nothing happened to the trade. positions_cache is ACCOUNT-WIDE (exec_broker.positions() takes no symbol), each
core owns one symbol's book, and all 13 cores write the list to the same bridge key where the last writer wins -
so the panel showed whichever core published last, and only that core's symbol carried a strategy and a state.

The second fault is in the same panel: every price was rendered at 2 decimal places, so a 5-digit FX quote lost
its last three digits and entry, current, stop and target collapsed into one number. A position hundreds of pips
from its target looked like it was sitting on it and refusing to close.
"""
import unittest

from tests.helpers import started_core


class _Pos:
    """Minimal stand-in for a broker position."""

    def __init__(self, ticket, symbol, magic=0, price=1.11234):
        self.ticket, self.symbol, self.magic = ticket, symbol, magic
        self.side, self.volume = "BUY", 0.02
        self.price_open, self.price_current = price, price + 0.012
        self.sl, self.tp, self.profit = price - 0.004, price + 0.015, 4.52


class _OtherCore:
    """A second core: its own symbol, its own book, its own spec."""

    def __init__(self, symbol, digits, book):
        self.symbol, self.book = symbol, book
        self.spec = type("Spec", (), {"digits": digits})()


class TestPositionsPanel(unittest.TestCase):
    def _publish_and_get_positions(self, core):
        core._publish()
        return core.bridge.get_state().get("positions") or []

    def test_a_managed_position_keeps_its_strategy_whichever_core_publishes(self):
        core, *_ = started_core()
        foreign_ticket = 2059769751

        # a trade owned by ANOTHER core's book, exactly like EURUSDm while the gold core publishes
        other_book = type("Book", (), {})()
        other_book.active = {"t1": {"position_id": foreign_ticket, "strategy_id": "vwap_reversion_v1",
                                    "state": "PROTECTION"}}
        other = _OtherCore("EURUSDm", 5, other_book)

        core.positions_cache = [_Pos(foreign_ticket, "EURUSDm", magic=core.cfg.broker.magic_number)]
        core.portfolio_cores = [core, other]

        rows = self._publish_and_get_positions(core)
        row = next(r for r in rows if r["ticket"] == foreign_ticket)
        self.assertEqual(row["strategy"], "vwap_reversion_v1",
                         "a position owned by another core must not lose its strategy")
        self.assertEqual(row["state"], "PROTECTION")

    def test_prices_carry_the_symbols_own_digits(self):
        core, *_ = started_core()
        other_book = type("Book", (), {})()
        other_book.active = {}
        core.positions_cache = [_Pos(1, "EURUSDm")]
        core.portfolio_cores = [core, _OtherCore("EURUSDm", 5, other_book)]
        row = next(r for r in self._publish_and_get_positions(core) if r["ticket"] == 1)
        self.assertEqual(row["digits"], 5, "a 5-digit FX symbol must not be rendered at 2 decimals")

    def test_an_unknown_symbol_still_renders(self):
        """digits is None for a symbol no core owns; the page falls back to 2 rather than breaking."""
        core, *_ = started_core()
        core.positions_cache = [_Pos(2, "SOMETHINGm")]
        core.portfolio_cores = [core]
        row = next(r for r in self._publish_and_get_positions(core) if r["ticket"] == 2)
        self.assertIsNone(row["digits"])
        self.assertIn("strategy", row)

    def test_single_core_mode_is_unchanged(self):
        """With no portfolio_cores attribute the core must still annotate its own book."""
        core, *_ = started_core()
        core.positions_cache = []
        if hasattr(core, "portfolio_cores"):
            del core.portfolio_cores
        self.assertEqual(self._publish_and_get_positions(core), [])


if __name__ == "__main__":
    unittest.main()
