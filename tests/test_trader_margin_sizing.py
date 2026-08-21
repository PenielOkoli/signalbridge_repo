from __future__ import annotations

import unittest
from decimal import Decimal, ROUND_DOWN

from config_manager import ExchangeConfig, RiskConfig
from bot_runtime import BotSupervisor
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


class UserFacingErrorTests(unittest.TestCase):
    def test_trade_notification_formats_rr_and_blank_lines(self) -> None:
        self.assertEqual(
            BotSupervisor._format_risk_reward(100, 95, [110, 115]),
            "TP1 1:2.00 · TP2 1:3.00",
        )
        self.assertEqual(
            BotSupervisor._format_telegram_lines(["Title", "Details", "Risk: $5"]),
            "Title\n\nDetails\n\nRisk: $5",
        )

    def test_bybit_json_error_becomes_a_clear_margin_message(self) -> None:
        raw_error = (
            "failed to place limit entry for MNT/USDT:USDT: bybit "
            '{"retCode":"110007","retMsg":"ab not enough for new order","result":{},"time":178728911008}'
        )

        message = BotSupervisor._user_facing_error_reason(raw_error)

        self.assertEqual(
            message,
            "Not enough free USDT margin for this order. Add collateral or release funds from open orders/positions.",
        )
        self.assertNotIn("retCode", message)
        self.assertNotIn("178728911008", message)

    def test_unknown_raw_payload_is_not_forwarded_to_telegram(self) -> None:
        message = BotSupervisor._user_facing_error_reason('Bybit rejected request {"retCode":"10001","detail":"internal"}')

        self.assertEqual(message, "The exchange rejected the request. Review the signal details and available margin.")

    def test_unrecognized_technical_error_is_not_forwarded_to_telegram(self) -> None:
        message = BotSupervisor._user_facing_error_reason("BadRequest: request-id=abc123 malformed exchange response")

        self.assertEqual(
            message,
            "The order could not be completed. Review the signal details, account balance, and exchange settings.",
        )
