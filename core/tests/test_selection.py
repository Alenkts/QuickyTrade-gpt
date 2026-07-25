from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from quickytrade_core.domain import OptionChain
from quickytrade_core.selection import SelectionError, choose_chain_and_expiry

NOW = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)


def chain(exchange: str, trading_class: str = "QQQ", multiplier: str = "100") -> OptionChain:
    return OptionChain(
        exchange=exchange,
        underlying_con_id=100,
        trading_class=trading_class,
        multiplier=multiplier,
        expirations=("20260720",),
        strikes=(Decimal("100"),),
    )


class ChooseChainAndExpiryTests(unittest.TestCase):
    kwargs = dict(
        target_dte=0,
        now=NOW,
        timezone_name="America/New_York",
        trading_class_allowlist=frozenset({"QQQ"}),
        same_day_cutoff=datetime.strptime("12:00", "%H:%M").time(),
    )

    def test_single_smart_chain_resolves(self):
        selected, expiry = choose_chain_and_expiry((chain("SMART"),), **self.kwargs)
        self.assertEqual(selected.exchange, "SMART")
        self.assertEqual(expiry, "20260720")

    def test_same_product_listed_on_multiple_real_exchanges_is_not_ambiguous(self):
        # IBKR's reqSecDefOptParams returns one row per exchange an option class
        # is listed on (SMART plus each real venue) -- this must not be treated
        # as multiple distinct products for a multi-listed name like QQQ/SPY.
        chains = (chain("CBOE"), chain("SMART"), chain("BOX"))
        selected, expiry = choose_chain_and_expiry(chains, **self.kwargs)
        self.assertEqual(selected.exchange, "SMART")
        self.assertEqual(expiry, "20260720")

    def test_smart_wins_regardless_of_row_order(self):
        chains = (chain("SMART"), chain("CBOE"), chain("BOX"))
        selected, _ = choose_chain_and_expiry(chains, **self.kwargs)
        self.assertEqual(selected.exchange, "SMART")

    def test_genuinely_distinct_trading_classes_remain_ambiguous(self):
        allowlisted = dict(self.kwargs)
        allowlisted["trading_class_allowlist"] = frozenset({"QQQ", "QQQW"})
        chains = (chain("SMART", trading_class="QQQ"), chain("SMART", trading_class="QQQW"))
        with self.assertRaises(SelectionError) as ctx:
            choose_chain_and_expiry(chains, **allowlisted)
        self.assertEqual(ctx.exception.code, "OPTION_CHAIN_AMBIGUOUS")

    def test_no_allowlisted_expiry_match(self):
        with self.assertRaises(SelectionError) as ctx:
            choose_chain_and_expiry((chain("SMART"),), **{**self.kwargs, "target_dte": 5})
        self.assertEqual(ctx.exception.code, "TARGET_EXPIRY_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
