from __future__ import annotations

import unittest
from decimal import Decimal, ROUND_DOWN

from config_manager import ExchangeConfig, RiskConfig
from signal_parser import EntryType, ParsedSignal, SignalAction, TradeSide
from trader import CcxtFuturesTrader, RiskCalculationError


SYMBOL = "BTC/USDT:USDT"


class FakeExchange:
    def __init__(self, free_usdt: str, total_usdt: str) -> None:
        self.free_usdt = free_usdt
        self.total_usdt = total_usdt
        self.markets = {
            SYMBOL: {
                "limits": {
                    "amount": {"min": 0.001, "max": None},
                    "cost": {"min": 1},
                }
            }
        }

    async def fetch_balance(self, params: dict[str, str]) -> dict[str, object]:
        return {
            "USDT": {"free": self.free_usdt, "total": self.total_usdt},
            "free": {"USDT": self.free_usdt},
            "total": {"USDT": self.total_usdt},
        }

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        del symbol
        return str(Decimal(str(amount)).quantize(Decimal("0.001"), rounding=ROUND_DOWN))


def make_trader(free_usdt: str, total_usdt: str) -> CcxtFuturesTrader:
    # Position sizing is isolated from exchange construction so this test never
    # contacts an exchange or needs API credentials.
    trader = object.__new__(CcxtFuturesTrader)
    trader.exchange = FakeExchange(free_usdt, total_usdt)
    trader.exchange_config = ExchangeConfig(default_leverage=10)
    trader.exchange_spec = {"balance_params": {}}
    trader.risk_config = RiskConfig(fixed_usdt_risk=100, max_leverage=10)
    return trader


def limit_signal() -> ParsedSignal:
    return ParsedSignal(
        action=SignalAction.OPEN,
        symbol=SYMBOL,
        side=TradeSide.BUY,
        entry_type=EntryType.LIMIT,
        entry_price=100,
        stop_loss=90,
        take_profit=110,
    )


class MarginAwareSizingTests(unittest.IsolatedAsyncioTestCase):
    async def test_caps_size_using_free_margin_not_total_equity(self) -> None:
        sizing = await make_trader("5", "1000").calculate_position_size(limit_signal())

        # $100 risk would request 10 BTC and $100 margin at 10x. The account
        # only has $5 free, so it uses $4.90 after the 2% safety reserve.
        self.assertEqual(sizing.amount, 0.49)
        self.assertEqual(sizing.risk_usdt, 4.9)
        self.assertEqual(sizing.requested_risk_usdt, 100)
        self.assertEqual(sizing.estimated_margin_usdt, 4.9)
        self.assertEqual(sizing.available_margin_usdt, 4.9)
        self.assertTrue(sizing.risk_limited_by_available_margin)

    async def test_zero_free_margin_never_falls_back_to_total_equity(self) -> None:
        with self.assertRaisesRegex(RiskCalculationError, "no free USDT margin"):
            await make_trader("0", "1000").calculate_position_size(limit_signal())

