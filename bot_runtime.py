"""
SignalBridge bot supervisor.

This module owns the long-lived Telegram / parser / trader runtime so the API
bridge can control it through explicit start, stop, and authentication calls.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Literal

from telethon import TelegramClient, events
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.utils import get_peer_id

from bridge_logging import LogStore
from config_manager import AppConfig, ConfigManager, ConfigManagerError, RuntimeSecrets
from signal_context import SignalRegistry, TrackedSignal
from signal_parser import (
    NoTradeSignalError,
    ParsedSignal,
    REPLY_MARKET_ACTIVATION_NOTE,
    SignalAction,
    SignalParser,
    SignalParserError,
    SignalValidationError,
    amend_signal_from_open_update,
    merged_open_signal,
    normalize_bybit_linear_symbol,
)
from trader import CcxtFuturesTrader, DailyTradeLimitError, ExchangeSnapshot, ProtectionOrderError, RiskCalculationError, TraderError


BotState = Literal["stopped", "starting", "running", "stopping", "error"]
TelegramAuthState = Literal[
    "unknown",
    "unauthenticated",
    "code_sent",
    "password_required",
    "authenticated",
    "error",
]


class RuntimeSupervisorError(RuntimeError):
    """Base error for the SignalBridge runtime supervisor."""


class RuntimeConfigurationError(RuntimeSupervisorError):
    """Raised when the runtime cannot start from the current config."""


class TelegramAuthError(RuntimeSupervisorError):
    """Raised when the Telegram phone authentication flow fails."""


@dataclass(slots=True)
class PendingTelegramLogin:
    """In-flight Telegram phone auth context."""

    client: TelegramClient
    phone_code_hash: str
    created_at: datetime


@dataclass(slots=True)
class SignalEventEnvelope:
    """Normalized Telegram event details used for parsing and tracking."""

    event_type: Literal["new_message", "edited_message"]
    chat_key: str
    chat_name: str
    message_id: int | None
    reply_to_message_id: int | None
    raw_text: str
    reply_to_text: str = ""


class BotSupervisor:
    """Own the Telegram worker lifecycle and phone authentication flow."""

    def __init__(self, config_manager: ConfigManager, log_store: LogStore) -> None:
        self.config_manager = config_manager
        self.log_store = log_store
        self._lock = asyncio.Lock()
        self._bot_state: BotState = "stopped"
        self._auth_state: TelegramAuthState = "unknown"
        self._last_error: str | None = None
        self._started_at: datetime | None = None
        self._bot_task: asyncio.Task[None] | None = None
        self._telegram_client: TelegramClient | None = None
        self._parser: SignalParser | None = None
        self._trader: CcxtFuturesTrader | None = None
        self._pending_login: PendingTelegramLogin | None = None
        self._signal_registry = SignalRegistry()

    async def shutdown(self) -> None:
        """Gracefully stop background services and pending auth sessions."""

        await self.stop_bot()
        await self._clear_pending_login()

    async def get_status(self) -> dict[str, Any]:
        """Return a dashboard-oriented runtime snapshot."""

        config = self._load_or_initialize_config()
        trades_last_24h = len(self._trader.trade_timestamps) if self._trader else 0
        session_file_present = self._session_file_path(config).exists()
        telegram_app_configured = self.config_manager.telegram_app_configured(config)
        ready_for_auth = bool(telegram_app_configured and config.telegram.phone_number)
        parser_key_configured = self.config_manager.parser_api_key_configured(config)
        ready_for_trading = bool(
            ready_for_auth
            and config.telegram.monitored_chats
            and config.exchange.encrypted_api_key
            and config.exchange.encrypted_api_secret
            and parser_key_configured
        )

        auth_state = self._auth_state
        if auth_state == "unknown":
            auth_state = "authenticated" if session_file_present else "unauthenticated"

        return {
            "bridge": {
                "status": "online",
                "version": "1.0.0",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "log_entries": self.log_store.count(),
                "activity_log_path": str(self.log_store.file_path) if self.log_store.file_path else None,
            },
            "bot": {
                "state": self._bot_state,
                "running": self._bot_state == "running",
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "last_error": self._last_error,
                "can_start": ready_for_trading
                and auth_state == "authenticated"
                and self._bot_state in {"stopped", "error"}
                and self._bot_task is None,
                "trades_last_24h": trades_last_24h,
                "monitored_chat_count": len(config.telegram.monitored_chats),
                "tracked_signal_count": self._signal_registry.count(),
            },
            "telegram": {
                "auth_state": auth_state,
                "configured": ready_for_auth,
                "app_configured": telegram_app_configured,
                "phone_number_set": bool(config.telegram.phone_number),
                "session_file_present": session_file_present,
                "monitored_chats": config.telegram.monitored_chats,
                "code_sent_at": self._pending_login.created_at.isoformat() if self._pending_login else None,
            },
            "config": {
                "ready_for_auth": ready_for_auth,
                "ready_for_trading": ready_for_trading,
                "telegram_configured": ready_for_auth,
                "exchange_id": config.exchange.exchange_id,
                "exchange_mode": config.exchange.mode,
                "exchange_credentials_configured": bool(
                    config.exchange.encrypted_api_key and config.exchange.encrypted_api_secret
                ),
                "openai_configured": parser_key_configured,
                "risk_mode": config.risk.risk_mode,
                "daily_trade_limit": config.risk.daily_trade_limit,
                "max_take_profit_orders": config.risk.max_take_profit_orders,
            },
        }

    async def start_bot(self) -> dict[str, Any]:
        """Start the Telegram worker if the runtime is fully configured."""

        async with self._lock:
            if self._bot_state == "running":
                return await self.get_status()
            if self._bot_state == "starting":
                raise RuntimeSupervisorError("bot is already starting")

            config = self._load_or_initialize_config()
            runtime_secrets = self._load_runtime_secrets(config)
            self._assert_trading_ready(config, runtime_secrets)

            self._bot_state = "starting"
            self._last_error = None
            self.log_store.append("info", "starting trading worker")

            parser = SignalParser(runtime_secrets.openai_api_key, config.openai)
            trader = CcxtFuturesTrader(
                runtime_secrets.exchange_api_key,
                runtime_secrets.exchange_api_secret,
                runtime_secrets.exchange_api_password,
                config.exchange,
                config.risk,
            )
            client = self._build_telegram_client(config)

            try:
                await client.connect()
                if not await client.is_user_authorized():
                    self._auth_state = "unauthenticated"
                    raise RuntimeConfigurationError(
                        "telegram session is not authenticated; request and verify a phone login code first"
                    )
            except Exception:
                await trader.close()
                await client.disconnect()
                self._bot_state = "stopped"
                raise

            self._auth_state = "authenticated"
            self._telegram_client = client
            self._parser = parser
            self._trader = trader
            self._started_at = datetime.now(timezone.utc)
            self._register_telegram_handler(client, config)
            self._bot_task = asyncio.create_task(self._run_telegram_loop(client), name="signalbridge-telegram")
            self._bot_state = "running"
            self.log_store.append(
                "info",
                "trading worker started",
                monitored_chat_count=len(config.telegram.monitored_chats),
                session_name=self.config_manager.resolve_telegram_session_name(config),
            )
            return await self.get_status()

    async def stop_bot(self) -> dict[str, Any]:
        """Stop the Telegram worker if it is active."""

        async with self._lock:
            if self._bot_state in {"stopped", "error"} and self._bot_task is None:
                self._bot_state = "stopped"
                return await self.get_status()

            self._bot_state = "stopping"
            self.log_store.append("info", "stopping trading worker")
            client = self._telegram_client
            task = self._bot_task

            if client is not None:
                await client.disconnect()

            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=10)
                except asyncio.TimeoutError:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

            self._bot_state = "stopped"
            self._started_at = None
            self.log_store.append("info", "trading worker stopped")
            return await self.get_status()

    async def request_telegram_code(self) -> dict[str, Any]:
        """Send a Telegram OTP to the configured phone number."""

        async with self._lock:
            config = self._load_or_initialize_config()
            if not self.config_manager.telegram_app_configured(config) or not config.telegram.phone_number:
                raise RuntimeConfigurationError(
                    "Telegram phone number is required before requesting a code"
                )

            await self._clear_pending_login()
            client = self._build_telegram_client(config)

            try:
                await client.connect()
                if await client.is_user_authorized():
                    self._auth_state = "authenticated"
                    self.log_store.append("info", "telegram session already authenticated")
                    await client.disconnect()
                    return await self.get_status()

                result = await client.send_code_request(config.telegram.phone_number)
            except FloodWaitError as exc:
                await client.disconnect()
                raise TelegramAuthError(f"telegram rate limited code delivery; wait {exc.seconds} seconds") from exc
            except (ApiIdInvalidError, PhoneNumberInvalidError) as exc:
                await client.disconnect()
                raise TelegramAuthError(str(exc)) from exc
            except Exception as exc:
                await client.disconnect()
                raise TelegramAuthError("failed to request Telegram login code") from exc

            self._pending_login = PendingTelegramLogin(
                client=client,
                phone_code_hash=result.phone_code_hash,
                created_at=datetime.now(timezone.utc),
            )
            self._auth_state = "code_sent"
            self.log_store.append("info", "telegram login code requested")
            return await self.get_status()

    async def verify_telegram_code(self, code: str, password: str | None = None) -> dict[str, Any]:
        """Verify the Telegram login code and persist the session file locally."""

        async with self._lock:
            config = self._load_or_initialize_config()
            pending = self._pending_login
            if pending is None:
                raise TelegramAuthError("no pending Telegram login request; request a code first")

            try:
                await pending.client.sign_in(
                    phone=config.telegram.phone_number,
                    code=code,
                    phone_code_hash=pending.phone_code_hash,
                )
            except SessionPasswordNeededError:
                if not password:
                    self._auth_state = "password_required"
                    self.log_store.append("warning", "telegram account requires two-factor password")
                    raise TelegramAuthError("telegram account requires a two-factor password") from None
                try:
                    await pending.client.sign_in(password=password)
                except Exception as exc:
                    raise TelegramAuthError("telegram two-factor password was rejected") from exc
            except PhoneCodeInvalidError as exc:
                raise TelegramAuthError("telegram login code is invalid") from exc
            except PhoneCodeExpiredError as exc:
                raise TelegramAuthError("telegram login code has expired; request a new code") from exc
            except Exception as exc:
                raise TelegramAuthError("telegram login verification failed") from exc

            self._auth_state = "authenticated"
            self.log_store.append("info", "telegram session authenticated and stored locally")
            await pending.client.disconnect()
            self._pending_login = None
            return await self.get_status()

    async def logout_telegram(self) -> dict[str, Any]:
        """Log out the persisted Telegram session and remove local session artifacts."""

        await self.stop_bot()

        async with self._lock:
            config = self._load_or_initialize_config()
            await self._clear_pending_login()
            client = self._build_telegram_client(config)

            try:
                await client.connect()
                if await client.is_user_authorized():
                    await client.log_out()
            finally:
                await client.disconnect()

            for path in self._session_related_paths(config):
                if path.exists():
                    path.unlink(missing_ok=True)

            self._auth_state = "unauthenticated"
            self._signal_registry.reset()
            self.log_store.append("info", "telegram session logged out and local session files removed")
            return await self.get_status()

    async def get_exchange_snapshot(self) -> dict[str, Any]:
        """Return current exchange positions and working orders for verification."""

        config = self._load_or_initialize_config()
        runtime_secrets = self._load_runtime_secrets(config)
        if not runtime_secrets.exchange_api_key or not runtime_secrets.exchange_api_secret:
            raise RuntimeConfigurationError("encrypted exchange API credentials are required")

        live_trader = self._trader
        owns_trader = live_trader is None
        trader = live_trader or CcxtFuturesTrader(
            runtime_secrets.exchange_api_key,
            runtime_secrets.exchange_api_secret,
            runtime_secrets.exchange_api_password,
            config.exchange,
            config.risk,
        )

        try:
            snapshot: ExchangeSnapshot = await trader.fetch_exchange_snapshot()
        except TraderError as exc:
            raise RuntimeConfigurationError(str(exc)) from exc
        finally:
            if owns_trader:
                await trader.close()

        return {
            "exchange_id": config.exchange.exchange_id,
            "mode": config.exchange.mode,
            "source": "live_runtime" if not owns_trader else "on_demand",
            **snapshot.model_dump(mode="json"),
        }

    async def list_available_telegram_chats(self) -> dict[str, Any]:
        """Return the authenticated account's available Telegram channels/groups."""

        async with self._lock:
            config = self._load_or_initialize_config()
            if not self.config_manager.telegram_app_configured(config):
                raise RuntimeConfigurationError("Telegram app credentials are not configured on the server")

            owns_client = False
            client = self._telegram_client
            if client is None:
                client = self._build_telegram_client(config)
                owns_client = True

            try:
                if owns_client:
                    await client.connect()
                if not await client.is_user_authorized():
                    self._auth_state = "unauthenticated"
                    raise TelegramAuthError("telegram session is not authenticated; verify a login code first")

                self._auth_state = "authenticated"
                chats: list[dict[str, Any]] = []
                async for dialog in client.iter_dialogs(ignore_migrated=True):
                    if not (dialog.is_channel or dialog.is_group):
                        continue

                    entity = dialog.entity
                    username = getattr(entity, "username", None)
                    peer_id = get_peer_id(entity)
                    source_value = username or str(peer_id)
                    chats.append(
                        {
                            "peer_id": str(peer_id),
                            "source_value": source_value,
                            "title": dialog.name,
                            "username": username,
                            "kind": "channel" if dialog.is_channel and not dialog.is_group else "group",
                            "member_count_hint": getattr(entity, "participants_count", None),
                        }
                    )
            except TelegramAuthError:
                raise
            except Exception as exc:
                raise TelegramAuthError("failed to load Telegram chats from the authenticated session") from exc
            finally:
                if owns_client:
                    await client.disconnect()

            chats.sort(key=lambda item: (item["kind"], item["title"].lower(), item["source_value"].lower()))
            return {
                "chats": chats,
                "selected": config.telegram.monitored_chats,
            }

    async def on_config_updated(self, previous: AppConfig, current: AppConfig) -> None:
        """Handle config changes that affect pending auth or a running worker."""

        telegram_identity_changed = any(
            [
                previous.telegram.api_id != current.telegram.api_id,
                previous.telegram.api_hash != current.telegram.api_hash,
                previous.telegram.phone_number != current.telegram.phone_number,
                previous.telegram.session_name != current.telegram.session_name,
            ]
        )

        if telegram_identity_changed:
            await self._clear_pending_login()
            self._auth_state = "authenticated" if self._session_file_path(current).exists() else "unauthenticated"
            self._signal_registry.reset()
            self.log_store.append("warning", "telegram identity settings changed; request a new login code if needed")

        if self._bot_state == "running":
            self.log_store.append("warning", "configuration updated while bot is running; restart the bot to apply changes")

    @property
    def bot_state(self) -> BotState:
        return self._bot_state

    async def _run_telegram_loop(self, client: TelegramClient) -> None:
        reconnect_delay_seconds = 5
        max_reconnect_delay_seconds = 60
        reconnecting = False

        try:
            while self._bot_state in {"running", "starting"}:
                try:
                    if not client.is_connected():
                        await client.connect()
                    if not await client.is_user_authorized():
                        self._auth_state = "unauthenticated"
                        self._bot_state = "error"
                        self._last_error = "telegram session is no longer authenticated"
                        self.log_store.append("error", "telegram session is no longer authenticated")
                        break

                    if reconnecting:
                        self._last_error = None
                        self.log_store.append("info", "telegram listener reconnected")
                        reconnecting = False
                        reconnect_delay_seconds = 5

                    await client.run_until_disconnected()
                    if self._bot_state in {"stopping", "stopped"}:
                        break

                    reconnecting = True
                    self._last_error = "telegram client disconnected; attempting reconnect"
                    self.log_store.append(
                        "warning",
                        "telegram client disconnected; attempting reconnect",
                        retry_in_seconds=reconnect_delay_seconds,
                    )
                    with contextlib.suppress(Exception):
                        await client.disconnect()
                    await asyncio.sleep(reconnect_delay_seconds)
                    reconnect_delay_seconds = min(reconnect_delay_seconds * 2, max_reconnect_delay_seconds)
                except (ConnectionError, OSError, TimeoutError) as exc:
                    if self._bot_state not in {"running", "starting"}:
                        break

                    reconnecting = True
                    self._last_error = str(exc) or exc.__class__.__name__
                    self.log_store.append(
                        "warning",
                        "telegram connection lost; retrying",
                        error=exc.__class__.__name__,
                        detail=str(exc)[:240],
                        retry_in_seconds=reconnect_delay_seconds,
                    )
                    with contextlib.suppress(Exception):
                        await client.disconnect()
                    await asyncio.sleep(reconnect_delay_seconds)
                    reconnect_delay_seconds = min(reconnect_delay_seconds * 2, max_reconnect_delay_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._bot_state = "error"
            self._last_error = str(exc)
            self.log_store.append("error", "telegram listener crashed", error=exc.__class__.__name__)
        finally:
            await self._dispose_live_runtime(client)

    async def _dispose_live_runtime(self, client: TelegramClient | None = None) -> None:
        trader = self._trader
        live_client = self._telegram_client

        self._bot_task = None
        self._telegram_client = None
        self._parser = None
        self._trader = None
        self._started_at = None

        if trader is not None:
            await trader.close()
        if client is not None:
            await client.disconnect()
        elif live_client is not None:
            await live_client.disconnect()

    async def _handle_signal_event(self, event: Any, event_type: Literal["new_message", "edited_message"]) -> None:
        parser = self._parser
        trader = self._trader
        if parser is None or trader is None:
            self.log_store.append("warning", "signal received while worker is not armed")
            return

        envelope = await self._build_signal_event_envelope(event, event_type)

        try:
            parse_context = self._signal_registry.build_parser_context(
                chat_key=envelope.chat_key,
                chat_name=envelope.chat_name,
                message_id=envelope.message_id,
                reply_to_message_id=envelope.reply_to_message_id,
                reply_to_text=envelope.reply_to_text,
                event_type=envelope.event_type,
            )
            parsed = await parser.parse(envelope.raw_text, parse_context)
            tracked_signal = self._signal_registry.resolve_reference(
                chat_key=envelope.chat_key,
                parsed=parsed,
                message_id=envelope.message_id,
                reply_to_message_id=envelope.reply_to_message_id,
            )
            executable_signal, execution_mode, tracked_signal, replacement_cancel_symbol = await self._resolve_executable_signal(
                trader,
                parsed,
                tracked_signal,
                envelope,
            )
            self.log_store.append(
                "trade",
                "parsed trade instruction",
                chat=envelope.chat_name,
                event_type=envelope.event_type,
                message_id=envelope.message_id,
                reply_to_message_id=envelope.reply_to_message_id,
                action=parsed.action,
                symbol=executable_signal.symbol or parsed.symbol,
                side=parsed.side,
                entry_type=parsed.entry_type,
                entry=parsed.entry_price,
                stop=parsed.stop_loss,
                take_profit=parsed.take_profit,
                close_fraction=parsed.close_fraction,
                is_signal_update=parsed.is_signal_update,
                reference_signal_key=tracked_signal.key if tracked_signal is not None else parsed.reference_signal_key,
                execution_mode=execution_mode,
            )
            result = await self._execute_signal_with_mode(
                trader,
                executable_signal,
                execution_mode,
                replacement_cancel_symbol,
            )
            signal_snapshot = self._snapshot_after_execution(parsed, executable_signal, result, tracked_signal, execution_mode)
            tracked_after = self._signal_registry.record_execution(
                chat_key=envelope.chat_key,
                chat_name=envelope.chat_name,
                message_id=envelope.message_id,
                raw_message=envelope.raw_text,
                signal_snapshot=signal_snapshot,
                result=result,
                tracked_signal=tracked_signal,
            )
            self.log_store.append(
                "trade",
                "trade instruction executed",
                chat=envelope.chat_name,
                event_type=envelope.event_type,
                message_id=envelope.message_id,
                action=result.action,
                symbol=result.symbol,
                side=result.side,
                amount=result.amount,
                leverage=result.leverage,
                action_order_id=result.action_order_id,
                entry_order_id=result.entry_order_id,
                stop_loss_order_id=result.stop_loss_order_id,
                take_profit_order_id=result.take_profit_order_id,
                take_profit_order_ids=result.take_profit_order_ids,
                canceled_order_ids=result.canceled_order_ids,
                amended_fields=result.amended_fields,
                tracked_signal_key=tracked_after.key if tracked_after is not None else None,
                execution_mode=execution_mode,
                execution_message=result.message,
            )
        except NoTradeSignalError as exc:
            self.log_store.append("debug", "telegram message ignored", chat=envelope.chat_name, reason=str(exc))
        except DailyTradeLimitError as exc:
            self.log_store.append("warning", str(exc), chat=envelope.chat_name)
        except ProtectionOrderError as exc:
            self.log_store.append("error", str(exc), chat=envelope.chat_name)
        except (SignalParserError, TraderError) as exc:
            self.log_store.append("error", str(exc), chat=envelope.chat_name)
        except Exception as exc:
            self.log_store.append(
                "error",
                "unhandled listener error",
                chat=envelope.chat_name,
                error=exc.__class__.__name__,
                detail=str(exc)[:240],
            )

    def _register_telegram_handler(self, client: TelegramClient, config: AppConfig) -> None:
        @client.on(events.NewMessage(chats=self._normalized_monitored_chat_refs(config.telegram.monitored_chats) or None))
        async def handle_signal(event: events.NewMessage.Event) -> None:
            try:
                await self._handle_signal_event(event, "new_message")
            except Exception as exc:
                self.log_store.append(
                    "error",
                    "failed to process telegram message",
                    error_type=exc.__class__.__name__,
                    error_detail=str(exc)[:240],
                )

        @client.on(events.MessageEdited(chats=self._normalized_monitored_chat_refs(config.telegram.monitored_chats) or None))
        async def handle_signal_edit(event: events.MessageEdited.Event) -> None:
            try:
                await self._handle_signal_event(event, "edited_message")
            except Exception as exc:
                self.log_store.append(
                    "error",
                    "failed to process telegram message edit",
                    error_type=exc.__class__.__name__,
                    error_detail=str(exc)[:240],
                )

    async def _build_signal_event_envelope(
        self,
        event: Any,
        event_type: Literal["new_message", "edited_message"],
    ) -> SignalEventEnvelope:
        raw_text = event.raw_text or ""
        message = getattr(event, "message", None)
        message_id = getattr(message, "id", None) or getattr(event, "id", None)
        reply_to_message_id = (
            getattr(message, "reply_to_msg_id", None)
            or getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
            or getattr(event, "reply_to_msg_id", None)
        )
        reply_to_text = ""
        if reply_to_message_id is not None:
            with contextlib.suppress(Exception):
                reply_message = await event.get_reply_message()
                reply_to_text = (getattr(reply_message, "raw_text", None) or "").strip()

        chat = await event.get_chat()
        chat_name = getattr(chat, "username", None) or getattr(chat, "title", None) or str(event.chat_id)
        chat_key = str(getattr(event, "chat_id", None) or getattr(chat, "id", None) or chat_name)

        return SignalEventEnvelope(
            event_type=event_type,
            chat_key=chat_key,
            chat_name=chat_name,
            message_id=message_id,
            reply_to_message_id=reply_to_message_id,
            raw_text=raw_text,
            reply_to_text=reply_to_text,
        )

    async def _resolve_executable_signal(
        self,
        trader: CcxtFuturesTrader,
        parsed: ParsedSignal,
        tracked_signal: TrackedSignal | None,
        envelope: SignalEventEnvelope,
    ) -> tuple[ParsedSignal, str, TrackedSignal | None, str | None]:
        action = parsed.action

        if tracked_signal is None:
            if parsed.is_signal_update or action in {SignalAction.CLOSE, SignalAction.CANCEL, SignalAction.AMEND, "close", "cancel", "amend"}:
                return await self._resolve_untracked_update(trader, parsed, envelope)
            if parsed.symbol is None:
                raise SignalValidationError("signal symbol could not be resolved")
            return parsed, "direct", None, None

        if tracked_signal.signal.symbol is None:
            raise SignalValidationError("tracked signal is missing a symbol and cannot be managed")

        tracked_symbol = tracked_signal.signal.symbol

        if action == SignalAction.CLOSE or action == "close":
            if parsed.symbol is not None and parsed.symbol != tracked_symbol:
                raise SignalValidationError("close update resolved to a tracked signal but specified a different symbol")
            return self._with_missing_symbol(parsed, tracked_symbol), "direct", tracked_signal, None

        if action == SignalAction.CANCEL or action == "cancel":
            if parsed.symbol is not None and parsed.symbol != tracked_symbol:
                raise SignalValidationError("cancel update resolved to a tracked signal but specified a different symbol")
            return self._with_missing_symbol(parsed, tracked_symbol), "direct", tracked_signal, None

        if action == SignalAction.AMEND or action == "amend":
            if parsed.symbol is not None and parsed.symbol != tracked_symbol:
                raise SignalValidationError("amend update resolved to a tracked signal but specified a different symbol")
            amend_signal = self._with_missing_symbol(parsed, tracked_symbol)
            if await trader.has_open_position(tracked_symbol):
                full_amend_signal = amend_signal_from_open_update(tracked_signal.signal, amend_signal)
                if full_amend_signal is None:
                    raise NoTradeSignalError("amend update did not change an executable protective field")
                return full_amend_signal, "direct", tracked_signal, None
            return merged_open_signal(tracked_signal.signal, amend_signal), "replace_pending_open", tracked_signal, tracked_symbol

        if action == SignalAction.OPEN or action == "open":
            updated_open = self._with_missing_symbol(parsed, tracked_symbol)
            if await trader.has_open_position(tracked_symbol):
                if updated_open.symbol != tracked_symbol:
                    raise SignalValidationError("edited live signal changed symbol; manual review is required")
                amend_signal = amend_signal_from_open_update(tracked_signal.signal, updated_open)
                if amend_signal is None:
                    raise NoTradeSignalError("edited signal did not change an executable protective field")
                return amend_signal, "open_update_to_amend", tracked_signal, None
            return merged_open_signal(tracked_signal.signal, updated_open), "replace_pending_open", tracked_signal, tracked_symbol

        raise SignalValidationError(f"unsupported signal action: {action}")

    async def _execute_signal_with_mode(
        self,
        trader: CcxtFuturesTrader,
        executable_signal: ParsedSignal,
        execution_mode: str,
        replacement_cancel_symbol: str | None,
    ):
        try:
            if execution_mode != "replace_pending_open":
                return await trader.execute_signal(executable_signal)

            if executable_signal.action == SignalAction.OPEN or executable_signal.action == "open":
                await trader._prepare_exchange()
                await trader.calculate_position_size(executable_signal)

            cancel_signal = ParsedSignal.model_validate(
                {
                    "action": SignalAction.CANCEL,
                    "symbol": replacement_cancel_symbol or executable_signal.symbol,
                    "side": executable_signal.side,
                    "entry_type": None,
                    "entry_price": None,
                    "stop_loss": None,
                    "take_profit": None,
                    "take_profit_targets": [],
                    "leverage": None,
                    "close_fraction": None,
                    "move_stop_to_entry": False,
                    "confidence": executable_signal.confidence,
                    "notes": executable_signal.notes,
                    "is_signal_update": True,
                    "reference_signal_key": executable_signal.reference_signal_key,
                    "source_message": executable_signal.source_message,
                }
            )
            cancel_result = await trader.execute_signal(cancel_signal)
            open_result = await trader.execute_signal(executable_signal)
            merged_canceled_order_ids = [*cancel_result.canceled_order_ids, *open_result.canceled_order_ids]
            merged_message = open_result.message
            if cancel_result.canceled_order_ids:
                merged_message = f"pending setup replaced; {open_result.message}"
            return open_result.model_copy(
                update={
                    "canceled_order_ids": merged_canceled_order_ids,
                    "message": merged_message,
                }
            )
        except RiskCalculationError as exc:
            if self._is_reply_market_activation(executable_signal):
                raise SignalValidationError(
                    "market activation request was understood, but the referenced signal's original SL/TP are no longer valid at the current market price"
                ) from exc
            raise

    def _snapshot_after_execution(
        self,
        parsed: ParsedSignal,
        executable_signal: ParsedSignal,
        result: Any,
        tracked_signal: TrackedSignal | None,
        execution_mode: str,
    ) -> ParsedSignal:
        action = result.action

        if action == SignalAction.OPEN or action == "open":
            return executable_signal

        if action == SignalAction.AMEND or action == "amend":
            if tracked_signal is None:
                return executable_signal
            if execution_mode == "open_update_to_amend":
                return merged_open_signal(tracked_signal.signal, parsed)
            return merged_open_signal(tracked_signal.signal, executable_signal)

        if tracked_signal is not None:
            return tracked_signal.signal
        return executable_signal

    async def _resolve_untracked_update(
        self,
        trader: CcxtFuturesTrader,
        parsed: ParsedSignal,
        envelope: SignalEventEnvelope,
    ) -> tuple[ParsedSignal, str, TrackedSignal | None, str | None]:
        snapshot = await trader.fetch_exchange_snapshot()
        recovered_symbol = self._resolve_recovery_symbol(parsed, envelope, snapshot)
        if recovered_symbol is None:
            raise SignalValidationError(
                "signal update could not be matched to a prior tracked signal or an active exchange symbol"
            )

        has_position = self._snapshot_has_open_position(snapshot, recovered_symbol)
        has_orders = self._snapshot_has_open_orders(snapshot, recovered_symbol)
        recovered_signal = self._with_missing_symbol(parsed, recovered_symbol)
        action = parsed.action

        if action == SignalAction.CLOSE or action == "close":
            if has_position:
                return recovered_signal, "restart_recovery_close", None, None
            if has_orders:
                cancel_signal = self._with_action(recovered_signal, SignalAction.CANCEL)
                return cancel_signal, "restart_recovery_close_to_cancel", None, None
            raise SignalValidationError(
                f"close update could not be applied because no live position or pending order was found for {recovered_symbol}"
            )

        if action == SignalAction.CANCEL or action == "cancel":
            if has_orders:
                return self._with_action(recovered_signal, SignalAction.CANCEL), "restart_recovery_cancel", None, None
            raise SignalValidationError(
                f"cancel update could not be applied because no pending order was found for {recovered_symbol}"
            )

        if action == SignalAction.AMEND or action == "amend":
            if has_position:
                if not self._has_complete_protective_plan(recovered_signal):
                    if recovered_signal.move_stop_to_entry:
                        recovered_signal = self._with_preserved_take_profits(recovered_signal, snapshot)
                    else:
                        raise SignalValidationError(
                            "amend update could not be applied after restart because it does not restate both stop loss and take profit"
                        )
                if not self._has_complete_protective_plan(recovered_signal):
                    raise SignalValidationError(
                        "amend update could not be applied after restart because no take-profit orders were found to preserve"
                    )
                return recovered_signal, "restart_recovery_amend", None, None
            raise SignalValidationError(
                "amend update could not be applied after restart because the original tracked signal is unavailable "
                f"and no live position was found for {recovered_symbol}"
            )

        if action == SignalAction.OPEN or action == "open":
            if has_position:
                amend_signal = self._coerce_open_update_to_amend(recovered_signal)
                return amend_signal, "restart_recovery_open_to_amend", None, None
            if has_orders:
                return recovered_signal, "replace_pending_open", None, recovered_symbol
            raise SignalValidationError(
                "signal update could not be matched to a prior tracked signal, open position, or pending order"
            )

        raise SignalValidationError(f"unsupported signal action: {action}")

    @staticmethod
    def _with_missing_symbol(parsed: ParsedSignal, symbol: str) -> ParsedSignal:
        payload = parsed.model_dump(mode="python")
        payload["symbol"] = payload.get("symbol") or symbol
        return ParsedSignal.model_validate(payload)

    @staticmethod
    def _with_action(parsed: ParsedSignal, action: SignalAction | str) -> ParsedSignal:
        payload = parsed.model_dump(mode="python")
        payload["action"] = action
        if action == SignalAction.CANCEL or action == "cancel":
            payload["entry_type"] = None
            payload["entry_price"] = None
            payload["stop_loss"] = None
            payload["take_profit"] = None
            payload["take_profit_targets"] = []
            payload["close_fraction"] = None
            payload["move_stop_to_entry"] = False
        return ParsedSignal.model_validate(payload)

    @staticmethod
    def _is_reply_market_activation(parsed: ParsedSignal) -> bool:
        return (parsed.notes or "").startswith(REPLY_MARKET_ACTIVATION_NOTE)

    @staticmethod
    def _coerce_open_update_to_amend(parsed: ParsedSignal) -> ParsedSignal:
        targets = list(parsed.take_profit_targets)
        if not targets and parsed.take_profit is not None:
            targets = [parsed.take_profit]

        payload = {
            "action": SignalAction.AMEND,
            "symbol": parsed.symbol,
            "side": parsed.side,
            "entry_type": None,
            "entry_price": None,
            "stop_loss": parsed.stop_loss,
            "take_profit": targets[0] if targets else None,
            "take_profit_targets": targets,
            "leverage": None,
            "close_fraction": None,
            "move_stop_to_entry": parsed.move_stop_to_entry,
            "confidence": parsed.confidence,
            "notes": parsed.notes,
            "is_signal_update": True,
            "reference_signal_key": parsed.reference_signal_key,
            "source_message": parsed.source_message,
        }
        return ParsedSignal.model_validate(payload)

    @staticmethod
    def _has_complete_protective_plan(parsed: ParsedSignal) -> bool:
        has_stop = parsed.stop_loss is not None or parsed.move_stop_to_entry
        has_take_profit = parsed.take_profit is not None or bool(parsed.take_profit_targets)
        return has_stop and has_take_profit

    @staticmethod
    def _with_preserved_take_profits(parsed: ParsedSignal, snapshot: ExchangeSnapshot) -> ParsedSignal:
        """Fill in take-profit targets from the live exchange state for breakeven-only updates."""

        position = next((item for item in snapshot.open_positions if item.symbol == parsed.symbol and item.contracts > 0), None)
        if position is None or position.entry_price is None:
            return parsed

        entry_price = position.entry_price
        side = parsed.side
        if side == SignalAction.CLOSE:
            return parsed

        if str(side).lower() == "buy":
            preserved_targets = [order.trigger_price for order in snapshot.open_orders if order.symbol == parsed.symbol and order.reduce_only and order.trigger_price is not None and order.trigger_price > entry_price]
        elif str(side).lower() == "sell":
            preserved_targets = [order.trigger_price for order in snapshot.open_orders if order.symbol == parsed.symbol and order.reduce_only and order.trigger_price is not None and order.trigger_price < entry_price]
        else:
            preserved_targets = [order.trigger_price for order in snapshot.open_orders if order.symbol == parsed.symbol and order.reduce_only and order.trigger_price is not None]

        preserved_targets = sorted({float(target) for target in preserved_targets})
        if not preserved_targets:
            return parsed

        payload = parsed.model_dump(mode="python")
        payload["take_profit_targets"] = preserved_targets
        payload["take_profit"] = preserved_targets[0]
        return ParsedSignal.model_validate(payload)

    def _resolve_recovery_symbol(
        self,
        parsed: ParsedSignal,
        envelope: SignalEventEnvelope,
        snapshot: ExchangeSnapshot,
    ) -> str | None:
        preferred_symbols: list[str] = []
        for candidate in (
            parsed.symbol,
            *self._extract_symbols_from_text(envelope.reply_to_text),
            *self._extract_symbols_from_text(envelope.raw_text),
        ):
            if candidate and candidate not in preferred_symbols:
                preferred_symbols.append(candidate)

        active_symbols = self._snapshot_active_symbols(snapshot)
        active_matches = [symbol for symbol in preferred_symbols if symbol in active_symbols]
        if len(active_matches) == 1:
            return active_matches[0]
        if len(preferred_symbols) == 1:
            return preferred_symbols[0]
        if preferred_symbols:
            return None
        if len(active_symbols) == 1:
            return next(iter(active_symbols))
        return None

    @staticmethod
    def _extract_symbols_from_text(text: str) -> list[str]:
        if not text:
            return []

        candidates: list[str] = []
        patterns = (
            r"\b[A-Z0-9]{2,20}/USDT:USDT\b",
            r"\b[A-Z0-9]{2,20}/USDT\b",
            r"\b[A-Z0-9]{2,20}USDT\b",
            r"[$#]([A-Z0-9]{2,20})\b",
        )
        upper_text = text.upper()
        for pattern in patterns:
            for match in re.finditer(pattern, upper_text):
                raw_symbol = match.group(1) if match.groups() else match.group(0)
                normalized = normalize_bybit_linear_symbol(raw_symbol)
                if normalized and normalized not in candidates:
                    candidates.append(normalized)
        return candidates

    @staticmethod
    def _snapshot_active_symbols(snapshot: ExchangeSnapshot) -> set[str]:
        symbols = {position.symbol for position in snapshot.open_positions if position.symbol}
        symbols.update(order.symbol for order in snapshot.open_orders if order.symbol)
        return symbols

    @staticmethod
    def _snapshot_has_open_position(snapshot: ExchangeSnapshot, symbol: str) -> bool:
        return any(position.symbol == symbol and position.contracts > 0 for position in snapshot.open_positions)

    @staticmethod
    def _snapshot_has_open_orders(snapshot: ExchangeSnapshot, symbol: str) -> bool:
        return any(order.symbol == symbol for order in snapshot.open_orders)

    def _load_or_initialize_config(self) -> AppConfig:
        try:
            if self.config_manager.config_exists():
                return self.config_manager.load_config()
            config = self.config_manager.initialize_empty_config()
            self.config_manager.save_config(config)
            self.log_store.append("warning", "created empty config.json; complete configuration before starting the bot")
            return config
        except ConfigManagerError as exc:
            raise RuntimeConfigurationError(str(exc)) from exc

    def _load_runtime_secrets(self, config: AppConfig) -> RuntimeSecrets:
        try:
            return self.config_manager.decrypt_runtime_secrets(config)
        except ConfigManagerError as exc:
            raise RuntimeConfigurationError(str(exc)) from exc

    def _assert_trading_ready(self, config: AppConfig, runtime_secrets: RuntimeSecrets) -> None:
        if not self.config_manager.telegram_app_configured(config) or not config.telegram.phone_number:
            raise RuntimeConfigurationError("Telegram phone login is required")
        if not config.telegram.monitored_chats:
            raise RuntimeConfigurationError("telegram.monitored_chats must include at least one channel or chat")
        if not runtime_secrets.openai_api_key:
            raise RuntimeConfigurationError("an encrypted OpenAI API key is required")
        if not runtime_secrets.exchange_api_key or not runtime_secrets.exchange_api_secret:
            raise RuntimeConfigurationError("encrypted exchange API credentials are required")

    def _build_telegram_client(self, config: AppConfig) -> TelegramClient:
        api_id = self.config_manager.resolve_telegram_api_id(config)
        api_hash = self.config_manager.resolve_telegram_api_hash(config)
        if not api_id or not api_hash:
            raise RuntimeConfigurationError("Telegram app credentials are not configured on the server")
        return TelegramClient(self.config_manager.resolve_telegram_session_name(config), api_id, api_hash)

    @staticmethod
    def _normalized_monitored_chat_refs(chat_refs: list[str]) -> list[int | str]:
        normalized: list[int | str] = []
        for value in chat_refs:
            stripped = value.strip()
            if not stripped:
                continue
            if stripped.startswith("@"):
                stripped = stripped[1:]
            if re.fullmatch(r"-?\d+", stripped):
                normalized.append(int(stripped))
            else:
                normalized.append(stripped)
        return normalized

    async def _clear_pending_login(self) -> None:
        pending = self._pending_login
        self._pending_login = None
        if pending is not None:
            await pending.client.disconnect()

    def _session_file_path(self, config: AppConfig) -> Path:
        return Path(f"{self.config_manager.resolve_telegram_session_name(config)}.session")

    def _session_related_paths(self, config: AppConfig) -> list[Path]:
        base = self._session_file_path(config)
        return [
            base,
            Path(f"{base}.journal"),
            Path(f"{base}-journal"),
        ]
