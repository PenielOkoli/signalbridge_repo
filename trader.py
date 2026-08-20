"""
SignalBridge CCXT futures execution engine.

The engine receives a validated ParsedSignal and executes the appropriate trade
workflow for USDT perpetuals on supported CCXT exchanges:
1. Open: set leverage, size the position, place the entry, then place stop-loss
   and take-profit protection immediately.
2. Close: market-close an existing position, fully or partially.
3. Cancel: cancel outstanding open orders for a symbol.
4. Amend: replace protective orders on an active position.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Awaitable, Callable

import ccxt.async_support as ccxt
from pydantic import BaseModel, ConfigDict, Field

from config_manager import ExchangeConfig, ExchangeId, ExchangeMode, RiskConfig, RiskMode
from signal_parser import EntryType, ParsedSignal, SignalAction, TradeSide


EXCHANGE_SPECS: dict[str, dict[str, Any]] = {
    ExchangeId.BYBIT.value: {
        "label": "Bybit",
        "ccxt_id": "bybit",
        "options": {"defaultType": "swap", "defaultSubType": "linear", "adjustForTimeDifference": True},
        "request_params": {"category": "linear"},
        "balance_params": {"type": "swap"},
        "sandbox": True,
        "fetch_currencies": False,
    },
    ExchangeId.BINGX.value: {
        "label": "BingX",
        "ccxt_id": "bingx",
        "options": {"defaultType": "swap"},
        "sandbox": False,
    },
    ExchangeId.BINANCE_USDM.value: {
        "label": "Binance USD-M",
        "ccxt_id": "binanceusdm",
        "options": {"defaultType": "future", "adjustForTimeDifference": True},
        "balance_params": {"type": "future"},
        "sandbox": True,
        "protection_order_types": {"stop_loss": "STOP_MARKET", "take_profit": "TAKE_PROFIT_MARKET"},
        "protection_trigger_field": "stopPrice",
        "protection_extra_params": {"workingType": "MARK_PRICE"},
    },
    ExchangeId.OKX.value: {
        "label": "OKX",
        "ccxt_id": "okx",
        "options": {"defaultType": "swap"},
        "request_params": {"tdMode": "cross"},
        "sandbox": True,
        "requires_password": True,
    },
    ExchangeId.BITGET.value: {
        "label": "Bitget",
        "ccxt_id": "bitget",
        "options": {"defaultType": "swap"},
        "sandbox": False,
        "requires_password": True,
    },
    ExchangeId.KUCOIN_FUTURES.value: {
        "label": "KuCoin Futures",
        "ccxt_id": "kucoinfutures",
        "options": {"defaultType": "swap"},
        "sandbox": False,
        "requires_password": True,
    },
    ExchangeId.MEXC.value: {
        "label": "MEXC",
        "ccxt_id": "mexc",
        "options": {"defaultType": "swap"},
        "sandbox": False,
    },
    ExchangeId.GATEIO.value: {
        "label": "Gate.io",
        "ccxt_id": "gateio",
        "options": {"defaultType": "swap"},
        "sandbox": False,
    },
    ExchangeId.PHEMEX.value: {
        "label": "Phemex",
        "ccxt_id": "phemex",
        "options": {"defaultType": "swap"},
        "sandbox": True,
    },
    ExchangeId.COINEX.value: {
        "label": "CoinEx",
        "ccxt_id": "coinex",
        "options": {"defaultType": "swap"},
        "sandbox": False,
    },
}


def _compact_exception_chain(exc: BaseException) -> str:
    """Return one safe, single-line message including nested network causes."""

    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = " ".join(str(current).split()).strip()
        if message and message not in parts:
            parts.append(message)
        current = current.__cause__ or current.__context__
    return " | ".join(parts)


class TraderError(RuntimeError):
    """Base error for trading failures."""


class TraderConfigurationError(TraderError):
    """Raised when exchange credentials or market configuration are invalid."""


class RiskCalculationError(TraderError):
    """Raised when position sizing cannot produce a valid order amount."""


class OrderPlacementError(TraderError):
    """Raised when an exchange order request fails."""


class ProtectionOrderError(OrderPlacementError):
    """Raised when entry succeeds but stop-loss/take-profit placement fails."""


class DailyTradeLimitError(TraderError):
    """Raised when the configured rolling 24-hour trade limit has been reached."""


class PositionLookupError(TraderError):
    """Raised when a required exchange position cannot be found or interpreted."""


class TraderModel(BaseModel):
    """Base model for trader contracts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, use_enum_values=True)


class PositionSizing(TraderModel):
    """Computed position details before exchange placement."""

    symbol: str
    side: TradeSide
    entry_type: EntryType
    leverage: int = Field(ge=1, le=125)
    amount: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit_targets: list[float] = Field(default_factory=list)
    risk_usdt: float = Field(gt=0)
    estimated_notional_usdt: float = Field(gt=0)


class ExecutionResult(TraderModel):
    """Exchange response summary safe for logs/API output."""

    action: SignalAction
    symbol: str
    side: TradeSide | None = None
    amount: float | None = None
    leverage: int | None = None
    action_order_id: str | None = None
    entry_order_id: str | None = None
    stop_loss_order_id: str | None = None
    take_profit_order_id: str | None = None
    take_profit_order_ids: list[str] = Field(default_factory=list)
    canceled_order_ids: list[str] = Field(default_factory=list)
    amended_fields: list[str] = Field(default_factory=list)
    protective_orders_native: bool = False
    close_fraction: float | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit_targets: list[float] = Field(default_factory=list)
    risk_usdt: float | None = None
    raw_entry_status: str | None = None
    message: str = ""


class ExchangePositionView(TraderModel):
    """Readable open-position snapshot for dashboard verification."""

    symbol: str
    side: TradeSide
    contracts: float = Field(ge=0)
    entry_price: float | None = None
    mark_price: float | None = None
    leverage: float | None = None
    liquidation_price: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    notional_usdt: float | None = None


class ExchangeOrderView(TraderModel):
    """Readable open-order snapshot for dashboard verification."""

    order_id: str
    symbol: str
    side: str
    order_type: str
    status: str
    amount: float | None = None
    remaining: float | None = None
    price: float | None = None
    trigger_price: float | None = None
    reduce_only: bool = False
    timestamp: str | None = None


class ExchangeSnapshot(TraderModel):
    """Exchange account state relevant for execution verification."""

    fetched_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    total_open_positions: int = 0
    total_open_orders: int = 0
    free_usdt: float | None = None
    total_usdt: float | None = None
    open_positions: list[ExchangePositionView] = Field(default_factory=list)
    open_orders: list[ExchangeOrderView] = Field(default_factory=list)


class CcxtFuturesTrader:
    """Async CCXT USDT perpetual futures trader."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        api_password: str,
        exchange_config: ExchangeConfig,
        risk_config: RiskConfig,
    ) -> None:
        if not api_key or not api_secret:
            raise TraderConfigurationError("exchange API key and secret are required")

        self.exchange_config = exchange_config
        self.risk_config = risk_config
        self.exchange_id = str(exchange_config.exchange_id)
        self.exchange_spec = self._exchange_spec(self.exchange_id)
        if self.exchange_spec.get("requires_password") and not api_password:
            raise TraderConfigurationError(f"{self.exchange_label} requires an API passphrase/password")

        exchange_class = getattr(ccxt, str(self.exchange_spec["ccxt_id"]), None)
        if exchange_class is None:
            raise TraderConfigurationError(f"CCXT does not provide an adapter for {self.exchange_id}")

        exchange_kwargs: dict[str, Any] = {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": dict(self.exchange_spec.get("options", {})),
        }
        if api_password:
            exchange_kwargs["password"] = api_password

        self.exchange = exchange_class(exchange_kwargs)
        self.exchange.options["adjustForTimeDifference"] = True
        self.exchange.options["recvWindow"] = 10000
        if self.exchange_spec.get("fetch_currencies") is False:
            self.exchange.has["fetchCurrencies"] = False
        self._executed_trade_timestamps: deque[datetime] = deque()
        self._markets_loaded = False

        if exchange_config.mode == ExchangeMode.TESTNET or exchange_config.mode == "testnet":
            if not self.exchange_spec.get("sandbox", False):
                raise TraderConfigurationError(f"{self.exchange_label} does not expose a CCXT sandbox/testnet mode")
            try:
                self.exchange.set_sandbox_mode(True)
            except Exception as exc:
                raise TraderConfigurationError(
                    f"{self.exchange_label} sandbox/testnet mode could not be enabled by CCXT"
                ) from exc

    @property
    def exchange_label(self) -> str:
        return str(self.exchange_spec.get("label") or self.exchange_id)

    @property
    def trade_timestamps(self) -> tuple[datetime, ...]:
        """Expose the rolling trade timestamps for status reporting."""

        return tuple(self._executed_trade_timestamps)

    async def close(self) -> None:
        """Close CCXT network resources."""

        await self.exchange.close()

    async def execute_signal(self, signal: ParsedSignal) -> ExecutionResult:
        """Dispatch a validated instruction to the correct exchange workflow."""

        await self._prepare_exchange()

        action = signal.action
        if action == SignalAction.OPEN or action == "open":
            return await self._execute_open_signal(signal)
        if action == SignalAction.CLOSE or action == "close":
            return await self._close_position(signal)
        if action == SignalAction.CANCEL or action == "cancel":
            return await self._cancel_orders(signal.symbol)
        if action == SignalAction.AMEND or action == "amend":
            return await self._amend_position(signal)

        raise TraderConfigurationError(f"unsupported signal action: {action}")

    async def has_open_position(self, symbol: str) -> bool:
        """Return True when the account currently has an open position for the symbol."""

        await self._prepare_exchange()
        try:
            await self._get_open_position(symbol)
        except PositionLookupError:
            return False
        return True

    async def has_open_orders(self, symbol: str) -> bool:
        """Return True when the account currently has working orders for the symbol."""

        await self._prepare_exchange()
        try:
            open_orders = await self._call_exchange(
                lambda: self.exchange.fetch_open_orders(symbol, params=self._request_params())
            )
        except Exception as exc:
            raise TraderConfigurationError(
                f"failed to fetch {self.exchange_label} open orders for {symbol}: {self._exchange_error_detail(exc)}"
            ) from exc
        return any(self._order_id(order) for order in open_orders)

    async def fetch_exchange_snapshot(self) -> ExchangeSnapshot:
        """Fetch current open positions and working orders for dashboard verification."""

        await self._prepare_exchange()

        try:
            positions = await self._call_exchange(lambda: self.exchange.fetch_positions(None, params=self._request_params()))
        except Exception as exc:
            raise TraderConfigurationError(
                f"failed to fetch {self.exchange_label} positions: {self._exchange_error_detail(exc)}"
            ) from exc

        try:
            open_orders = await self._call_exchange(lambda: self.exchange.fetch_open_orders(None, params=self._request_params()))
        except Exception as exc:
            raise TraderConfigurationError(
                f"failed to fetch {self.exchange_label} open orders: {self._exchange_error_detail(exc)}"
            ) from exc

        free_usdt: float | None = None
        total_usdt: float | None = None
        try:
            balance = await self._call_exchange(lambda: self.exchange.fetch_balance(params=self._balance_params()))
            usdt_bucket = balance.get("USDT") or {}
            free_usdt = self._to_float_or_none(
                usdt_bucket.get("free") if isinstance(usdt_bucket, dict) else None,
                (balance.get("free") or {}).get("USDT") if isinstance(balance.get("free"), dict) else None,
            )
            total_usdt = self._to_float_or_none(
                usdt_bucket.get("total") if isinstance(usdt_bucket, dict) else None,
                (balance.get("total") or {}).get("USDT") if isinstance(balance.get("total"), dict) else None,
            )
        except Exception:
            free_usdt = None
            total_usdt = None

        position_views: list[ExchangePositionView] = []
        for position in positions:
            symbol = str(position.get("symbol") or "").strip()
            if not symbol:
                continue
            contracts = float(self._position_contracts(position))
            if contracts <= 0:
                continue

            side = self._position_side(position)
            entry_price = self._to_float_or_none(position.get("entryPrice"), position.get("average"), (position.get("info") or {}).get("avgPrice"))
            mark_price = self._to_float_or_none(position.get("markPrice"), (position.get("info") or {}).get("markPrice"))
            leverage = self._to_float_or_none(position.get("leverage"), (position.get("info") or {}).get("leverage"))
            liquidation_price = self._to_float_or_none(
                position.get("liquidationPrice"),
                (position.get("info") or {}).get("liqPrice"),
            )
            unrealized_pnl = self._to_float_or_none(
                position.get("unrealizedPnl"),
                position.get("unrealizedPnlValue"),
                (position.get("info") or {}).get("unrealisedPnl"),
            )
            # ccxt's bybit parse_position() reads 'curRealisedPnl' for
            # realizedPnl, but Bybit's open-positions endpoint only ever
            # returns 'cumRealisedPnl' -- so the unified field is always
            # null here. Read the raw field directly instead.
            realized_pnl = self._to_float_or_none(
                position.get("realizedPnl"),
                (position.get("info") or {}).get("cumRealisedPnl"),
            )
            reference_price = mark_price if mark_price and mark_price > 0 else entry_price
            notional_usdt = round(contracts * reference_price, 4) if reference_price and reference_price > 0 else None

            position_views.append(
                ExchangePositionView(
                    symbol=symbol,
                    side=side,
                    contracts=contracts,
                    entry_price=entry_price,
                    mark_price=mark_price,
                    leverage=leverage,
                    liquidation_price=liquidation_price,
                    unrealized_pnl=unrealized_pnl,
                    realized_pnl=realized_pnl,
                    notional_usdt=notional_usdt,
                )
            )

        order_views: list[ExchangeOrderView] = []
        for order in open_orders:
            order_id = self._order_id(order)
            symbol = str(order.get("symbol") or "").strip()
            if not order_id or not symbol:
                continue

            info = order.get("info") or {}
            timestamp_ms = order.get("timestamp") or info.get("createdTime")
            timestamp = None
            if timestamp_ms not in (None, ""):
                try:
                    timestamp = datetime.fromtimestamp(float(timestamp_ms) / 1000, timezone.utc).isoformat()
                except Exception:
                    timestamp = None

            order_views.append(
                ExchangeOrderView(
                    order_id=order_id,
                    symbol=symbol,
                    side=str(order.get("side") or info.get("side") or "").lower() or "-",
                    order_type=str(order.get("type") or info.get("orderType") or "").lower() or "-",
                    status=str(order.get("status") or info.get("orderStatus") or "").lower() or "-",
                    amount=self._to_float_or_none(order.get("amount"), info.get("qty")),
                    remaining=self._to_float_or_none(order.get("remaining"), info.get("leavesQty")),
                    price=self._to_float_or_none(order.get("price"), info.get("price")),
                    trigger_price=self._to_float_or_none(order.get("triggerPrice"), info.get("triggerPrice")),
                    reduce_only=bool(order.get("reduceOnly") or info.get("reduceOnly")),
                    timestamp=timestamp,
                )
            )

        position_views.sort(key=lambda item: item.symbol)
        order_views.sort(key=lambda item: (item.symbol, item.timestamp or ""), reverse=False)

        return ExchangeSnapshot(
            free_usdt=free_usdt,
            total_usdt=total_usdt,
            total_open_positions=len(position_views),
            total_open_orders=len(order_views),
            open_positions=position_views,
            open_orders=order_views,
        )

    async def calculate_position_size(self, signal: ParsedSignal) -> PositionSizing:
        """Calculate amount from the configured risk model and stop distance."""

        if signal.entry_type not in (EntryType.MARKET, EntryType.LIMIT, "market", "limit"):
            raise RiskCalculationError("open signals require a supported entry_type")

        entry = await self._resolve_reference_entry_price(signal)
        self._validate_signal_geometry(signal, float(entry))
        leverage = self._effective_leverage(signal)

        try:
            stop = Decimal(str(signal.stop_loss))
            risk_usdt = await self._resolve_risk_budget_usdt()
            risk_per_unit = abs(entry - stop)
            raw_amount = risk_usdt / risk_per_unit
        except (InvalidOperation, ZeroDivisionError) as exc:
            raise RiskCalculationError("unable to calculate position size from signal prices") from exc

        if raw_amount <= 0:
            raise RiskCalculationError("calculated position amount must be greater than zero")

        self._validate_raw_amount_before_precision(signal.symbol, raw_amount, risk_per_unit)
        amount = self._amount_to_exchange_precision(signal.symbol, raw_amount)
        notional = Decimal(str(amount)) * entry
        self._validate_market_limits(signal.symbol, Decimal(str(amount)), notional)

        return PositionSizing(
            symbol=signal.symbol,
            side=signal.side,
            entry_type=signal.entry_type,
            leverage=leverage,
            amount=amount,
            entry_price=float(entry),
            stop_loss=float(stop),
            take_profit_targets=self._selected_take_profit_targets(signal),
            risk_usdt=float(risk_usdt),
            estimated_notional_usdt=float(notional),
        )

    async def _execute_open_signal(self, signal: ParsedSignal) -> ExecutionResult:
        await self._enforce_daily_trade_limit()
        sizing = await self.calculate_position_size(signal)

        try:
            await self._call_exchange(
                lambda: self.exchange.set_leverage(sizing.leverage, sizing.symbol, params=self._request_params())
            )
        except Exception as exc:
            if not self._is_non_fatal_leverage_error(exc):
                raise OrderPlacementError(
                    f"failed to set leverage for {sizing.symbol}: {self._exchange_error_detail(exc)}"
                ) from exc

        entry_order: dict[str, Any] | None = None
        stop_order: dict[str, Any] | None = None
        take_profit_orders: list[dict[str, Any]] = []

        # Bybit lets stopLoss/takeProfit be attached directly on the entry
        # order itself -- market or limit -- as metadata on that same order,
        # not as separate live orders. For a limit entry, that metadata only
        # activates once the entry fills; if the entry is canceled first,
        # the attached protection is canceled with it automatically, since
        # it was never a standalone order to begin with. That's what fixes
        # the orphaned-SL/TP problem for limit entries. It only holds one TP
        # price at a time, so multi-target take-profit signals still place
        # the extra targets as separate conditional orders.
        use_native_protection = self.exchange_id == ExchangeId.BYBIT.value
        attach_take_profit_natively = use_native_protection and len(sizing.take_profit_targets) == 1

        try:
            entry_order_params = self._entry_order_params(sizing.entry_type)
            if sizing.entry_type in (EntryType.LIMIT, "limit"):
                entry_order_params["timeInForce"] = "GTC"
            if use_native_protection:
                entry_order_params["stopLoss"] = {"triggerPrice": sizing.stop_loss}
                if attach_take_profit_natively:
                    entry_order_params["takeProfit"] = {"triggerPrice": sizing.take_profit_targets[0]}
            entry_order = await self._call_exchange(
                lambda: self.exchange.create_order(
                    symbol=sizing.symbol,
                    type=self._entry_order_type(sizing.entry_type),
                    side=self._side_value(sizing.side),
                    amount=sizing.amount,
                    price=sizing.entry_price if sizing.entry_type in (EntryType.LIMIT, "limit") else None,
                    params=entry_order_params,
                )
            )
        except Exception as exc:
            raise OrderPlacementError(
                f"failed to place {self._entry_order_type(sizing.entry_type)} entry for {sizing.symbol}: {self._exchange_error_detail(exc)}"
            ) from exc

        self._record_trade_execution()

        remaining_take_profit_targets = [] if attach_take_profit_natively else sizing.take_profit_targets

        try:
            if not use_native_protection:
                stop_order = await self._place_reduce_only_trigger_order(
                    symbol=sizing.symbol,
                    close_side=self._opposite_side(sizing.side),
                    amount=sizing.amount,
                    trigger_price=sizing.stop_loss,
                    trigger_direction=self._trigger_direction(sizing.side, is_take_profit=False),
                    is_take_profit=False,
                )

            if remaining_take_profit_targets:
                tp_amounts = self._split_amount_across_targets(
                    sizing.symbol,
                    Decimal(str(sizing.amount)),
                    len(remaining_take_profit_targets),
                )
                for take_profit_target, take_profit_amount in zip(remaining_take_profit_targets, tp_amounts, strict=True):
                    take_profit_orders.append(
                        await self._place_reduce_only_trigger_order(
                            symbol=sizing.symbol,
                            close_side=self._opposite_side(sizing.side),
                            amount=take_profit_amount,
                            trigger_price=take_profit_target,
                            trigger_direction=self._trigger_direction(sizing.side, is_take_profit=True),
                            is_take_profit=True,
                        )
                    )
        except Exception as exc:
            entry_id = self._order_id(entry_order)
            raise ProtectionOrderError(
                f"entry order {entry_id or '<unknown>'} was placed, but protective orders failed: {self._exchange_error_detail(exc)}"
            ) from exc

        take_profit_order_ids = [order_id for order_id in (self._order_id(order) for order in take_profit_orders) if order_id]
        action_order_id = self._order_id(entry_order)

        message = "entry and protective orders submitted"
        if use_native_protection:
            message = (
                "entry submitted with stop-loss and take-profit attached to the order"
                if attach_take_profit_natively
                else "entry submitted with stop-loss attached to the order; take-profit orders submitted separately"
            )

        return ExecutionResult(
            action=SignalAction.OPEN,
            symbol=sizing.symbol,
            side=sizing.side,
            amount=sizing.amount,
            leverage=sizing.leverage,
            action_order_id=action_order_id,
            entry_order_id=action_order_id,
            stop_loss_order_id=self._order_id(stop_order),
            take_profit_order_id=take_profit_order_ids[0] if take_profit_order_ids else None,
            take_profit_order_ids=take_profit_order_ids,
            protective_orders_native=use_native_protection,
            entry_price=sizing.entry_price,
            stop_loss=sizing.stop_loss,
            take_profit_targets=sizing.take_profit_targets,
            risk_usdt=sizing.risk_usdt,
            raw_entry_status=entry_order.get("status") if entry_order else None,
            message=message,
        )

    async def _cancel_orders(self, symbol: str) -> ExecutionResult:
        canceled_order_ids = await self._cancel_open_orders_for_symbol(symbol)
        return ExecutionResult(
            action=SignalAction.CANCEL,
            symbol=symbol,
            canceled_order_ids=canceled_order_ids,
            message=f"canceled {len(canceled_order_ids)} open orders",
        )

    async def _close_position(self, signal: ParsedSignal) -> ExecutionResult:
        position = await self._get_open_position(signal.symbol)
        position_side = self._position_side(position)
        contracts = self._position_contracts(position)
        close_fraction = signal.close_fraction or 1.0
        canceled_order_ids = await self._cancel_open_orders_for_symbol(signal.symbol)

        amount = self._amount_to_exchange_precision(
            signal.symbol,
            contracts * Decimal(str(close_fraction)),
        )
        if amount <= 0:
            raise RiskCalculationError("close amount rounded to zero at exchange precision")

        try:
            close_order = await self._call_exchange(
                lambda: self.exchange.create_order(
                    symbol=signal.symbol,
                    type="market",
                    side=self._opposite_side(position_side),
                    amount=amount,
                    price=None,
                    params=self._close_order_params(),
                )
            )
        except Exception as exc:
            raise OrderPlacementError(
                f"failed to close position for {signal.symbol}: {self._exchange_error_detail(exc)}"
            ) from exc

        action_order_id = self._order_id(close_order)
        return ExecutionResult(
            action=SignalAction.CLOSE,
            symbol=signal.symbol,
            side=position_side,
            amount=amount,
            action_order_id=action_order_id,
            canceled_order_ids=canceled_order_ids,
            close_fraction=close_fraction,
            raw_entry_status=close_order.get("status"),
            message="position close submitted",
        )

    async def _amend_position(self, signal: ParsedSignal) -> ExecutionResult:
        snapshot = await self.fetch_exchange_snapshot()
        position = await self._get_open_position(signal.symbol)
        position_side = self._position_side(position)
        position_amount = self._position_contracts(position)
        entry_price = self._position_entry_price(position)
        is_bybit = self.exchange_id == ExchangeId.BYBIT.value

        existing_stop_loss, existing_take_profit_targets = self._existing_protective_targets(
            snapshot,
            signal.symbol,
            position_side,
            entry_price,
        )
        if is_bybit:
            native_stop_loss, native_take_profit = self._position_native_protection(position)
            if native_stop_loss is not None:
                existing_stop_loss = native_stop_loss
            if native_take_profit is not None and not existing_take_profit_targets:
                existing_take_profit_targets = [native_take_profit]

        amended_fields: list[str] = []
        stop_order: dict[str, Any] | None = None
        take_profit_orders: list[dict[str, Any]] = []

        new_stop_loss = signal.stop_loss
        if new_stop_loss is None and signal.move_stop_to_entry:
            new_stop_loss = entry_price
        if new_stop_loss is None:
            new_stop_loss = existing_stop_loss

        take_profit_targets = self._selected_take_profit_targets(signal)
        if not take_profit_targets:
            take_profit_targets = existing_take_profit_targets
        self._validate_amend_protective_plan(
            side=position_side,
            reference_entry_price=entry_price,
            stop_loss=new_stop_loss,
            take_profit_targets=take_profit_targets,
            allow_stop_at_entry=signal.move_stop_to_entry,
        )
        canceled_order_ids = await self._cancel_open_orders_for_symbol(signal.symbol)

        attach_take_profit_natively = is_bybit and len(take_profit_targets) == 1
        remaining_take_profit_targets = take_profit_targets
        used_native_protection = False

        if is_bybit and (new_stop_loss is not None or attach_take_profit_natively):
            await self._set_native_position_protection(
                symbol=signal.symbol,
                stop_loss=new_stop_loss,
                take_profit=take_profit_targets[0] if attach_take_profit_natively else None,
            )
            used_native_protection = True
            if new_stop_loss is not None:
                amended_fields.append("stop_loss")
            if attach_take_profit_natively:
                amended_fields.append("take_profit")
                remaining_take_profit_targets = []
        elif new_stop_loss is not None:
            stop_order = await self._place_reduce_only_trigger_order(
                symbol=signal.symbol,
                close_side=self._opposite_side(position_side),
                amount=float(position_amount),
                trigger_price=new_stop_loss,
                trigger_direction=self._trigger_direction(position_side, is_take_profit=False),
                is_take_profit=False,
            )
            amended_fields.append("stop_loss")

        if remaining_take_profit_targets:
            tp_amounts = self._split_amount_across_targets(signal.symbol, position_amount, len(remaining_take_profit_targets))
            for take_profit_target, tp_amount in zip(remaining_take_profit_targets, tp_amounts, strict=True):
                take_profit_orders.append(
                    await self._place_reduce_only_trigger_order(
                        symbol=signal.symbol,
                        close_side=self._opposite_side(position_side),
                        amount=tp_amount,
                        trigger_price=take_profit_target,
                        trigger_direction=self._trigger_direction(position_side, is_take_profit=True),
                        is_take_profit=True,
                    )
                )
            if "take_profit" not in amended_fields:
                amended_fields.append("take_profit")

        if not amended_fields:
            raise TraderConfigurationError("amend signal did not contain a supported protective-order change")

        take_profit_order_ids = [order_id for order_id in (self._order_id(order) for order in take_profit_orders) if order_id]
        return ExecutionResult(
            action=SignalAction.AMEND,
            symbol=signal.symbol,
            side=position_side,
            amount=float(position_amount),
            stop_loss_order_id=self._order_id(stop_order),
            take_profit_order_id=take_profit_order_ids[0] if take_profit_order_ids else None,
            take_profit_order_ids=take_profit_order_ids,
            canceled_order_ids=canceled_order_ids,
            amended_fields=amended_fields,
            protective_orders_native=used_native_protection,
            entry_price=entry_price,
            stop_loss=new_stop_loss,
            take_profit_targets=take_profit_targets,
            message="protective orders amended",
        )

    @staticmethod
    def _existing_protective_targets(
        snapshot: ExchangeSnapshot,
        symbol: str,
        side: TradeSide | str,
        entry_price: float,
    ) -> tuple[float | None, list[float]]:
        existing_stop_loss: float | None = None
        existing_take_profit_targets: list[float] = []

        for order in snapshot.open_orders:
            if order.symbol != symbol or not order.reduce_only or order.trigger_price is None:
                continue

            trigger_price = float(order.trigger_price)
            if side == TradeSide.BUY or side == "buy":
                if trigger_price < entry_price and existing_stop_loss is None:
                    existing_stop_loss = trigger_price
                elif trigger_price > entry_price:
                    existing_take_profit_targets.append(trigger_price)
            elif side == TradeSide.SELL or side == "sell":
                if trigger_price > entry_price and existing_stop_loss is None:
                    existing_stop_loss = trigger_price
                elif trigger_price < entry_price:
                    existing_take_profit_targets.append(trigger_price)
            elif existing_stop_loss is None:
                existing_stop_loss = trigger_price

        existing_take_profit_targets = sorted({float(target) for target in existing_take_profit_targets})
        return existing_stop_loss, existing_take_profit_targets

    async def _place_reduce_only_trigger_order(
        self,
        symbol: str,
        close_side: str,
        amount: float,
        trigger_price: float,
        trigger_direction: int,
        is_take_profit: bool,
    ) -> dict[str, Any]:
        return await self._call_exchange(
            lambda: self.exchange.create_order(
                symbol=symbol,
                type=self._protection_order_type(is_take_profit),
                side=close_side,
                amount=amount,
                price=None,
                params=self._protection_order_params(trigger_price, trigger_direction),
            )
        )

    async def _set_native_position_protection(
        self,
        symbol: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        attempts: int = 3,
    ) -> None:
        """Attach stop-loss/take-profit directly to an open Bybit position via
        the position/trading-stop endpoint, instead of separate conditional
        orders. Only fields that are provided are changed; omitting one
        leaves the position's existing value untouched.
        """
        if stop_loss is None and take_profit is None:
            return

        market = self.exchange.market(symbol)
        request = dict(self._request_params())
        request["symbol"] = market["id"]
        request["positionIdx"] = 0
        request["tpslMode"] = "Full"
        if stop_loss is not None:
            request["stopLoss"] = self.exchange.price_to_precision(symbol, stop_loss)
        if take_profit is not None:
            request["takeProfit"] = self.exchange.price_to_precision(symbol, take_profit)

        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                await self._call_exchange(
                    lambda: self.exchange.private_post_v5_position_trading_stop(request)
                )
                return
            except Exception as exc:
                last_error = exc
                # Bybit can briefly lag between an order fill and the
                # position becoming visible to this endpoint. Retry a
                # couple of times before surfacing the failure.
                if attempt < attempts - 1:
                    await asyncio.sleep(0.5)
        raise ProtectionOrderError(
            f"failed to attach native stop-loss/take-profit for {symbol}: {self._exchange_error_detail(last_error)}"
        )

    async def _resolve_reference_entry_price(self, signal: ParsedSignal) -> Decimal:
        if signal.entry_price is not None:
            return Decimal(str(signal.entry_price))

        try:
            ticker = await self._call_exchange(lambda: self.exchange.fetch_ticker(signal.symbol, params=self._request_params()))
        except Exception as exc:
            raise RiskCalculationError(f"unable to fetch current market price for {signal.symbol}") from exc

        price = self._first_positive_decimal(
            ticker.get("last"),
            ticker.get("mark"),
            ticker.get("bid"),
            ticker.get("ask"),
            (ticker.get("info") or {}).get("markPrice"),
            (ticker.get("info") or {}).get("lastPrice"),
            ((Decimal(str(ticker["bid"])) + Decimal(str(ticker["ask"]))) / 2)
            if ticker.get("bid") not in (None, "") and ticker.get("ask") not in (None, "")
            else None,
        )
        if price is None:
            raise RiskCalculationError(f"exchange ticker for {signal.symbol} did not include a usable price")
        return price

    async def _resolve_risk_budget_usdt(self) -> Decimal:
        if self.risk_config.risk_mode == RiskMode.FIXED_USDT or self.risk_config.risk_mode == "fixed_usdt":
            return Decimal(str(self.risk_config.fixed_usdt_risk))
        if self.risk_config.risk_mode == RiskMode.BALANCE_PERCENT or self.risk_config.risk_mode == "balance_percent":
            available_balance = await self._fetch_available_usdt_balance()
            return (available_balance * Decimal(str(self.risk_config.balance_risk_percent))) / Decimal("100")
        raise TraderConfigurationError(f"unsupported risk mode: {self.risk_config.risk_mode}")

    async def _fetch_available_usdt_balance(self) -> Decimal:
        try:
            balance = await self._call_exchange(lambda: self.exchange.fetch_balance(params=self._balance_params()))
        except Exception as exc:
            raise TraderConfigurationError("unable to fetch account balance for balance_percent sizing") from exc

        usdt_bucket = balance.get("USDT") or {}
        price = self._first_positive_decimal(
            usdt_bucket.get("free") if isinstance(usdt_bucket, dict) else None,
            (balance.get("free") or {}).get("USDT") if isinstance(balance.get("free"), dict) else None,
            usdt_bucket.get("total") if isinstance(usdt_bucket, dict) else None,
            (balance.get("total") or {}).get("USDT") if isinstance(balance.get("total"), dict) else None,
        )
        if price is None:
            raise TraderConfigurationError("could not determine an available USDT balance for sizing")
        return price

    async def _enforce_daily_trade_limit(self) -> None:
        if not self.risk_config.daily_trade_limit:
            return

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        while self._executed_trade_timestamps and self._executed_trade_timestamps[0] < cutoff:
            self._executed_trade_timestamps.popleft()

        if len(self._executed_trade_timestamps) >= self.risk_config.daily_trade_limit:
            raise DailyTradeLimitError(
                f"daily trade limit reached: {self.risk_config.daily_trade_limit} trades in the last 24 hours"
            )

    def _record_trade_execution(self) -> None:
        self._executed_trade_timestamps.append(datetime.now(timezone.utc))

    async def _cancel_open_orders_for_symbol(self, symbol: str) -> list[str]:
        try:
            open_orders = await self._call_exchange(lambda: self.exchange.fetch_open_orders(symbol, params=self._request_params()))
        except Exception as exc:
            raise OrderPlacementError(f"failed to fetch open orders for {symbol}: {self._exchange_error_detail(exc)}") from exc

        canceled_order_ids: list[str] = []
        for order in open_orders:
            order_id = self._order_id(order)
            if not order_id:
                continue
            try:
                await self._call_exchange(lambda: self.exchange.cancel_order(order_id, symbol, params=self._request_params()))
                canceled_order_ids.append(order_id)
            except Exception as exc:
                raise OrderPlacementError(
                    f"failed to cancel order {order_id} for {symbol}: {self._exchange_error_detail(exc)}"
                ) from exc
        return canceled_order_ids

    async def _get_open_position(self, symbol: str) -> dict[str, Any]:
        positions: list[dict[str, Any]] = []
        try:
            positions = await self._call_exchange(lambda: self.exchange.fetch_positions([symbol], params=self._request_params()))
        except Exception:
            try:
                single_position = await self._call_exchange(lambda: self.exchange.fetch_position(symbol, params=self._request_params()))
                positions = [single_position] if single_position else []
            except Exception as exc:
                raise PositionLookupError(f"failed to fetch position data for {symbol}") from exc

        for position in positions:
            if position.get("symbol") and position.get("symbol") != symbol:
                continue
            if self._position_contracts(position) > 0:
                return position

        raise PositionLookupError(f"no open position found for {symbol}")

    async def _prepare_exchange(self) -> None:
        await self._sync_exchange_clock()
        try:
            await self._call_exchange(lambda: self.exchange.load_markets(reload=not self._markets_loaded))
            self._markets_loaded = True
        except Exception as exc:
            raise TraderConfigurationError(f"failed to load {self.exchange_label} market metadata: {self._exchange_error_detail(exc)}") from exc

    async def _sync_exchange_clock(self) -> None:
        try:
            await self.exchange.load_time_difference()
        except Exception:
            # Later requests still retry once on InvalidNonce if this sync fails transiently.
            return

    async def _call_exchange(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await operation()
        except ccxt.InvalidNonce:
            await self._sync_exchange_clock()
            return await operation()

    @staticmethod
    def _exchange_error_detail(exc: Exception) -> str:
        message = _compact_exception_chain(exc)
        lowered = message.lower()
        if (
            "could not resolve host" in lowered
            or "remote name could not be resolved" in lowered
            or "name or service not known" in lowered
            or "getaddrinfo failed" in lowered
            or "nodename nor servname provided" in lowered
        ):
            return "exchange API host could not be resolved by DNS; check VPN, DNS, firewall, or regional network blocking"
        if isinstance(exc, ccxt.NetworkError):
            return f"network error while contacting exchange API: {message or exc.__class__.__name__}"
        if '"retcode":10024' in lowered or "compliance rules triggered" in lowered or "regulatory restrictions" in lowered:
            return "exchange account is restricted from this product due to compliance, KYC, or regional eligibility rules"
        if '"retcode":10009' in lowered or "service restricted" in lowered:
            return "exchange service is unavailable for this account's region"
        if '"retcode":33004' in lowered or "api key has expired" in lowered:
            return "exchange API key has expired"
        if '"retcode":110007' in lowered or "not enough for new order" in lowered:
            return "not enough available balance for this order"
        if '"retcode":110043' in lowered or "leverage not modified" in lowered:
            return "leverage already set to the requested value"
        if '"retcode":110017' in lowered or "reduce-only order" in lowered and "position" in lowered:
            return "reduce-only order would exceed the current position size"
        # Anything else: never surface a raw exchange JSON payload to a user-facing
        # log line. Keep whatever human-readable text precedes the embedded JSON
        # (ccxt error strings are typically "<ExchangeId> <message> <json blob>"),
        # and fall back to the exception class name if nothing readable remains.
        readable = message.split("{", 1)[0].strip(" :\u2014-")
        if readable and readable.lower() != exc.__class__.__name__.lower():
            return readable
        return exc.__class__.__name__

    @classmethod
    def _is_non_fatal_leverage_error(cls, exc: Exception) -> bool:
        raw = _compact_exception_chain(exc).lower()
        return '"retcode":110043' in raw or "leverage not modified" in raw

    @staticmethod
    def _to_float_or_none(*values: Any) -> float | None:
        for value in values:
            if value in (None, ""):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _amount_to_exchange_precision(self, symbol: str, amount: Decimal) -> float:
        normalized = self._amount_to_exchange_precision_decimal(symbol, amount)
        return float(normalized)

    def _amount_to_exchange_precision_decimal(self, symbol: str, amount: Decimal) -> Decimal:
        try:
            precise = self.exchange.amount_to_precision(symbol, float(amount))
            normalized = Decimal(str(precise)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN).normalize()
        except Exception as exc:
            raise RiskCalculationError(
                f"unable to apply exchange precision for {symbol}: {self._exchange_error_detail(exc)}"
            ) from exc

        if normalized <= 0:
            raise RiskCalculationError("amount rounded to zero at exchange precision")
        return normalized

    def _validate_raw_amount_before_precision(self, symbol: str | None, raw_amount: Decimal, risk_per_unit: Decimal) -> None:
        if not symbol:
            return

        market = self.exchange.markets.get(symbol)
        amount_limits = ((market or {}).get("limits") or {}).get("amount") or {}
        min_amount = amount_limits.get("min")
        if min_amount is None:
            return

        minimum_amount = Decimal(str(min_amount))
        if raw_amount >= minimum_amount:
            return

        minimum_risk = (minimum_amount * risk_per_unit).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        raise RiskCalculationError(
            f"calculated amount {raw_amount.normalize()} {symbol} is below exchange minimum {minimum_amount}; "
            f"increase fixed risk to at least {minimum_risk} USDT or use a tighter stop loss"
        )

    def _validate_market_limits(self, symbol: str, amount: Decimal, notional: Decimal) -> None:
        market = self.exchange.markets.get(symbol)
        if not market:
            raise TraderConfigurationError(f"market is not loaded or unsupported: {symbol}")

        limits = market.get("limits") or {}
        amount_limits = limits.get("amount") or {}
        cost_limits = limits.get("cost") or {}

        min_amount = amount_limits.get("min")
        if min_amount is not None and amount < Decimal(str(min_amount)):
            raise RiskCalculationError(f"amount {amount} is below exchange minimum {min_amount} for {symbol}")

        max_amount = amount_limits.get("max")
        if max_amount is not None and amount > Decimal(str(max_amount)):
            raise RiskCalculationError(f"amount {amount} is above exchange maximum {max_amount} for {symbol}")

        min_cost = cost_limits.get("min")
        if min_cost is not None and notional < Decimal(str(min_cost)):
            raise RiskCalculationError(f"notional {notional} is below exchange minimum {min_cost} for {symbol}")

    def _effective_leverage(self, signal: ParsedSignal) -> int:
        requested = signal.leverage or self.exchange_config.default_leverage
        return min(requested, self.risk_config.max_leverage)

    def _selected_take_profit_targets(self, signal: ParsedSignal) -> list[float]:
        targets = list(signal.take_profit_targets)
        if not targets and signal.take_profit is not None:
            targets = [signal.take_profit]
        return targets[: self.risk_config.max_take_profit_orders]

    def _split_amount_across_targets(self, symbol: str, total_amount: Decimal, target_count: int) -> list[float]:
        if target_count <= 0:
            raise RiskCalculationError("at least one take-profit target is required")
        if target_count == 1:
            return [float(self._amount_to_exchange_precision_decimal(symbol, total_amount))]

        allocated = Decimal("0")
        amounts: list[float] = []
        even_split = total_amount / Decimal(str(target_count))
        for index in range(target_count):
            raw_amount = total_amount - allocated if index == target_count - 1 else even_split
            precise_amount = self._amount_to_exchange_precision_decimal(symbol, raw_amount)
            allocated += precise_amount
            amounts.append(float(precise_amount))
        return amounts

    @staticmethod
    def _validate_signal_geometry(signal: ParsedSignal, reference_entry_price: float) -> None:
        targets = signal.take_profit_targets or ([signal.take_profit] if signal.take_profit is not None else [])
        if not targets:
            raise RiskCalculationError("open signal requires at least one take-profit target")

        if signal.side == TradeSide.BUY or signal.side == "buy":
            if signal.stop_loss is None or signal.stop_loss >= reference_entry_price:
                raise RiskCalculationError(
                    f"long signal requires stop_loss below entry (reference entry {reference_entry_price:.4f})"
                )
            if any(target <= reference_entry_price for target in targets):
                raise RiskCalculationError(
                    f"long signal requires all take-profit targets above entry (reference entry {reference_entry_price:.4f})"
                )
            return

        if signal.side == TradeSide.SELL or signal.side == "sell":
            if signal.stop_loss is None or signal.stop_loss <= reference_entry_price:
                raise RiskCalculationError(
                    f"short signal requires stop_loss above entry (reference entry {reference_entry_price:.4f})"
                )
            if any(target >= reference_entry_price for target in targets):
                raise RiskCalculationError(
                    f"short signal requires all take-profit targets below entry (reference entry {reference_entry_price:.4f})"
                )
            return

        raise RiskCalculationError(f"unsupported trade side: {signal.side}")

    @staticmethod
    def _validate_amend_protective_plan(
        side: TradeSide | str,
        reference_entry_price: float,
        stop_loss: float | None,
        take_profit_targets: list[float],
        allow_stop_at_entry: bool = False,
    ) -> None:
        if stop_loss is None and not take_profit_targets:
            raise RiskCalculationError("amend execution requires a stop loss, take-profit target, or move_stop_to_entry=true")

        if stop_loss is not None:
            if side == TradeSide.BUY or side == "buy":
                stop_is_invalid = stop_loss > reference_entry_price if allow_stop_at_entry else stop_loss >= reference_entry_price
                if stop_is_invalid:
                    raise RiskCalculationError(
                        f"long amend requires stop_loss at or below entry when moving to break-even, otherwise below entry (reference entry {reference_entry_price:.4f})"
                    )
            elif side == TradeSide.SELL or side == "sell":
                stop_is_invalid = stop_loss < reference_entry_price if allow_stop_at_entry else stop_loss <= reference_entry_price
                if stop_is_invalid:
                    raise RiskCalculationError(
                        f"short amend requires stop_loss at or above entry when moving to break-even, otherwise above entry (reference entry {reference_entry_price:.4f})"
                    )
            else:
                raise RiskCalculationError(f"unsupported trade side: {side}")

        if take_profit_targets:
            if side == TradeSide.BUY or side == "buy":
                if any(target <= reference_entry_price for target in take_profit_targets):
                    raise RiskCalculationError(
                        f"long amend requires all take-profit targets above entry (reference entry {reference_entry_price:.4f})"
                    )
                return

            if side == TradeSide.SELL or side == "sell":
                if any(target >= reference_entry_price for target in take_profit_targets):
                    raise RiskCalculationError(
                        f"short amend requires all take-profit targets below entry (reference entry {reference_entry_price:.4f})"
                    )
                return

        if side != TradeSide.BUY and side != "buy" and side != TradeSide.SELL and side != "sell":
            raise RiskCalculationError(f"unsupported trade side: {side}")

    @staticmethod
    def _entry_order_type(entry_type: EntryType | str) -> str:
        return "limit" if entry_type == EntryType.LIMIT or entry_type == "limit" else "market"

    def _entry_order_params(self, entry_type: EntryType | str) -> dict[str, Any]:
        params = self._request_params()
        if entry_type not in (EntryType.LIMIT, "limit") and self.exchange_id == ExchangeId.BYBIT.value:
            # Bybit market orders are internally capped by a price band. On
            # thin testnet books, an explicit bounded tolerance avoids false
            # rejections while still preventing unbounded slippage.
            params.update(
                {
                    "slippageToleranceType": "Percent",
                    "slippageTolerance": "5" if self.exchange_config.mode in (ExchangeMode.TESTNET, "testnet") else "2",
                }
            )
        return params

    def _close_order_params(self) -> dict[str, Any]:
        params = self._request_params()
        params["reduceOnly"] = True
        return params

    def _protection_order_type(self, is_take_profit: bool) -> str:
        order_types = self.exchange_spec.get("protection_order_types") or {}
        if is_take_profit:
            return str(order_types.get("take_profit") or "market")
        return str(order_types.get("stop_loss") or "market")

    def _protection_order_params(self, trigger_price: float, trigger_direction: int) -> dict[str, Any]:
        params = self._request_params()
        trigger_field = str(self.exchange_spec.get("protection_trigger_field") or "triggerPrice")
        params[trigger_field] = trigger_price
        if trigger_field != "triggerPrice":
            params["triggerPrice"] = trigger_price
        params["reduceOnly"] = True
        params.update(self.exchange_spec.get("protection_extra_params") or {})
        if self.exchange_id == ExchangeId.BYBIT.value:
            params["triggerDirection"] = trigger_direction
            params["closeOnTrigger"] = True
        return params

    def _request_params(self) -> dict[str, Any]:
        return dict(self.exchange_spec.get("request_params") or {})

    def _balance_params(self) -> dict[str, Any]:
        params = self.exchange_spec.get("balance_params")
        return dict(params if params is not None else self._request_params())

    @staticmethod
    def _exchange_spec(exchange_id: str) -> dict[str, Any]:
        spec = EXCHANGE_SPECS.get(exchange_id)
        if spec is None:
            supported = ", ".join(sorted(EXCHANGE_SPECS))
            raise TraderConfigurationError(f"unsupported exchange {exchange_id!r}; supported exchanges: {supported}")
        return spec

    @staticmethod
    def _side_value(side: TradeSide | str) -> str:
        return "buy" if side == TradeSide.BUY or side == "buy" else "sell"

    @staticmethod
    def _opposite_side(side: TradeSide | str) -> str:
        return "sell" if side == TradeSide.BUY or side == "buy" else "buy"

    @staticmethod
    def _trigger_direction(side: TradeSide | str, is_take_profit: bool) -> int:
        is_long = side == TradeSide.BUY or side == "buy"
        if is_long:
            return 1 if is_take_profit else 2
        return 2 if is_take_profit else 1

    @staticmethod
    def _order_id(order: dict[str, Any] | None) -> str | None:
        if not order:
            return None
        order_id = order.get("id")
        return str(order_id) if order_id is not None else None

    @staticmethod
    def _first_positive_decimal(*values: Any) -> Decimal | None:
        for value in values:
            if value in (None, ""):
                continue
            try:
                numeric = Decimal(str(value))
            except (ArithmeticError, InvalidOperation, ValueError):
                continue
            if numeric > 0:
                return numeric
        return None

    @staticmethod
    def _position_contracts(position: dict[str, Any]) -> Decimal:
        info = position.get("info") or {}
        candidates = (
            position.get("contracts"),
            position.get("contractSize"),
            position.get("size"),
            info.get("size"),
            info.get("positionAmt"),
            info.get("qty"),
        )
        for candidate in candidates:
            if candidate in (None, ""):
                continue
            try:
                return abs(Decimal(str(candidate)))
            except (ArithmeticError, InvalidOperation, ValueError):
                continue
        return Decimal("0")

    @staticmethod
    def _position_side(position: dict[str, Any]) -> TradeSide:
        info = position.get("info") or {}
        raw_side = str(position.get("side") or info.get("side") or "").strip().lower()
        if raw_side in {"long", "buy"}:
            return TradeSide.BUY
        if raw_side in {"short", "sell"}:
            return TradeSide.SELL

        signed_amount = None
        for candidate in (position.get("contracts"), info.get("positionAmt"), info.get("size")):
            if candidate in (None, ""):
                continue
            try:
                signed_amount = Decimal(str(candidate))
                break
            except (ArithmeticError, InvalidOperation, ValueError):
                continue

        if signed_amount is not None:
            return TradeSide.BUY if signed_amount >= 0 else TradeSide.SELL

        raise PositionLookupError("position side could not be determined")

    @staticmethod
    def _position_entry_price(position: dict[str, Any]) -> float:
        info = position.get("info") or {}
        candidates = (
            position.get("entryPrice"),
            position.get("average"),
            info.get("avgPrice"),
            info.get("entryPrice"),
        )
        for candidate in candidates:
            if candidate in (None, ""):
                continue
            try:
                numeric = float(candidate)
            except (TypeError, ValueError):
                continue
            if numeric > 0:
                return numeric
        raise PositionLookupError("position entry price could not be determined")

    @staticmethod
    def _position_native_protection(position: dict[str, Any]) -> tuple[float | None, float | None]:
        """Read Bybit's native position-attached stop-loss/take-profit, if any."""
        info = position.get("info") or {}

        def _positive_or_none(value: Any) -> float | None:
            if value in (None, ""):
                return None
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return None
            return numeric if numeric > 0 else None

        return _positive_or_none(info.get("stopLoss")), _positive_or_none(info.get("takeProfit"))


BybitTrader = CcxtFuturesTrader