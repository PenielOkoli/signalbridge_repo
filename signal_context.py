"""
SignalBridge in-memory signal tracking and reference resolution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from signal_parser import ParsedSignal, SignalAction, SignalCandidateContext, SignalContext
from trader import ExecutionResult


SignalLifecycle = Literal["pending_entry", "executed", "amended", "closed", "canceled"]
ACTIVE_LIFECYCLES: set[SignalLifecycle] = {"pending_entry", "executed", "amended"}


@dataclass(slots=True)
class TrackedSignal:
    """Single tracked signal plus its latest resolved state."""

    key: str
    chat_key: str
    chat_name: str
    origin_message_id: int
    latest_message_id: int
    lifecycle: SignalLifecycle
    signal: ParsedSignal
    raw_excerpt: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    related_message_ids: set[int] = field(default_factory=set)
    last_execution_message: str = ""

    def attach_message(self, message_id: int | None, raw_message: str, chat_name: str | None = None) -> None:
        """Associate another Telegram post or edit with this tracked signal."""

        if chat_name:
            self.chat_name = chat_name
        if message_id is not None:
            self.latest_message_id = message_id
            self.related_message_ids.add(message_id)
        self.raw_excerpt = _excerpt(raw_message)
        self.updated_at = datetime.now(timezone.utc)

    def to_candidate(self) -> SignalCandidateContext:
        """Render a compact model-facing candidate summary."""

        return SignalCandidateContext(
            key=self.key,
            origin_message_id=self.origin_message_id,
            latest_message_id=self.latest_message_id,
            lifecycle=self.lifecycle,
            symbol=self.signal.symbol,
            side=self.signal.side,
            entry_type=self.signal.entry_type,
            entry_price=self.signal.entry_price,
            stop_loss=self.signal.stop_loss,
            take_profit_targets=list(self.signal.take_profit_targets),
            leverage=self.signal.leverage,
            excerpt=self.raw_excerpt,
        )

    @property
    def is_active(self) -> bool:
        return self.lifecycle in ACTIVE_LIFECYCLES


class SignalRegistry:
    """Small in-memory registry for recently tracked signals per chat."""

    def __init__(self, max_signals_per_chat: int = 200) -> None:
        self.max_signals_per_chat = max_signals_per_chat
        self._signals_by_chat: dict[str, dict[str, TrackedSignal]] = {}
        self._message_index_by_chat: dict[str, dict[int, str]] = {}

    def reset(self) -> None:
        self._signals_by_chat.clear()
        self._message_index_by_chat.clear()

    def count(self) -> int:
        return sum(len(signals) for signals in self._signals_by_chat.values())

    def build_parser_context(
        self,
        *,
        chat_key: str,
        chat_name: str,
        message_id: int | None,
        reply_to_message_id: int | None,
        reply_to_text: str,
        event_type: Literal["new_message", "edited_message"],
        candidate_limit: int = 8,
    ) -> SignalContext:
        """Build structured context for the parser from tracked chat state."""

        message_index = self._message_index_by_chat.get(chat_key, {})
        same_message_signal_key = message_index.get(message_id) if message_id is not None else None
        reply_signal_key = message_index.get(reply_to_message_id) if reply_to_message_id is not None else None
        preferred_keys = {key for key in (same_message_signal_key, reply_signal_key) if key}
        candidates = [signal.to_candidate() for signal in self._sorted_signals(chat_key, preferred_keys)[:candidate_limit]]

        return SignalContext(
            event_type=event_type,
            chat_name=chat_name,
            message_id=message_id,
            reply_to_message_id=reply_to_message_id,
            reply_to_text=reply_to_text,
            same_message_signal_key=same_message_signal_key,
            reply_signal_key=reply_signal_key,
            candidate_signals=candidates,
        )

    def get_by_key(self, chat_key: str, key: str | None) -> TrackedSignal | None:
        if not key:
            return None
        return self._signals_by_chat.get(chat_key, {}).get(key)

    def get_by_message_id(self, chat_key: str, message_id: int | None) -> TrackedSignal | None:
        if message_id is None:
            return None
        key = self._message_index_by_chat.get(chat_key, {}).get(message_id)
        return self.get_by_key(chat_key, key)

    def resolve_reference(
        self,
        *,
        chat_key: str,
        parsed: ParsedSignal,
        message_id: int | None,
        reply_to_message_id: int | None,
    ) -> TrackedSignal | None:
        """Resolve the tracked signal that the current message most likely targets."""

        same_message_match = self.get_by_message_id(chat_key, message_id)
        if same_message_match is not None:
            return same_message_match

        explicit_reference = self.get_by_key(chat_key, parsed.reference_signal_key)
        if explicit_reference is not None:
            return explicit_reference

        reply_match = self.get_by_message_id(chat_key, reply_to_message_id)
        if reply_match is not None:
            return reply_match

        should_fallback = parsed.action != SignalAction.OPEN and parsed.action != "open"
        should_fallback = should_fallback or parsed.is_signal_update
        if not should_fallback:
            return None

        symbol = parsed.symbol
        side = parsed.side
        active_signals = [signal for signal in self._sorted_signals(chat_key) if signal.is_active]

        if symbol:
            same_symbol = [signal for signal in active_signals if signal.signal.symbol == symbol]
            if side:
                same_symbol_side = [signal for signal in same_symbol if signal.signal.side == side]
                if len(same_symbol_side) == 1:
                    return same_symbol_side[0]
            if len(same_symbol) == 1:
                return same_symbol[0]

        if len(active_signals) == 1:
            return active_signals[0]

        return None

    def find_duplicate_open(self, chat_key: str, parsed: ParsedSignal) -> TrackedSignal | None:
        """Detect a verbatim repost of an already-active signal -- same symbol,
        side, entry, stop loss, and take-profit targets -- so a duplicate post
        doesn't open a second position or a second pending order for a setup
        that's already live. Deliberately strict (every field must match) so a
        genuinely new signal on the same symbol is never mistaken for a repost.
        """

        if parsed.symbol is None or parsed.side is None:
            return None

        for signal in self._sorted_signals(chat_key):
            if not signal.is_active:
                continue
            existing = signal.signal
            if existing.symbol != parsed.symbol or existing.side != parsed.side:
                continue
            if existing.entry_type != parsed.entry_type:
                continue
            if not _values_match(existing.entry_price, parsed.entry_price):
                continue
            if not _values_match(existing.stop_loss, parsed.stop_loss):
                continue
            if not _target_lists_match(existing.take_profit_targets, parsed.take_profit_targets):
                continue
            return signal

        return None

    def record_execution(
        self,
        *,
        chat_key: str,
        chat_name: str,
        message_id: int | None,
        raw_message: str,
        signal_snapshot: ParsedSignal,
        result: ExecutionResult,
        tracked_signal: TrackedSignal | None = None,
    ) -> TrackedSignal | None:
        """Create or update tracked state after a successful exchange action."""

        if tracked_signal is None:
            if (result.action != SignalAction.OPEN and result.action != "open") or signal_snapshot.symbol is None or message_id is None:
                return None

            key = f"sig_{uuid4().hex[:10]}"
            tracked_signal = TrackedSignal(
                key=key,
                chat_key=chat_key,
                chat_name=chat_name,
                origin_message_id=message_id,
                latest_message_id=message_id,
                lifecycle=_lifecycle_after_execution(signal_snapshot, result, previous="executed"),
                signal=signal_snapshot,
                raw_excerpt=_excerpt(raw_message),
            )
            tracked_signal.related_message_ids.add(message_id)
            tracked_signal.last_execution_message = result.message

            self._signals_by_chat.setdefault(chat_key, {})[key] = tracked_signal
            self._index_message(chat_key, message_id, key)
            self._prune_chat(chat_key)
            return tracked_signal

        tracked_signal.signal = signal_snapshot
        tracked_signal.lifecycle = _lifecycle_after_execution(signal_snapshot, result, previous=tracked_signal.lifecycle)
        tracked_signal.last_execution_message = result.message
        tracked_signal.attach_message(message_id, raw_message, chat_name)
        if message_id is not None:
            self._index_message(chat_key, message_id, tracked_signal.key)
        return tracked_signal

    def attach_reference_message(
        self,
        *,
        tracked_signal: TrackedSignal,
        chat_key: str,
        chat_name: str,
        message_id: int | None,
        raw_message: str,
    ) -> None:
        """Link a follow-up message to an already tracked signal without changing lifecycle."""

        tracked_signal.attach_message(message_id, raw_message, chat_name)
        if message_id is not None:
            self._index_message(chat_key, message_id, tracked_signal.key)

    def _sorted_signals(self, chat_key: str, preferred_keys: set[str] | None = None) -> list[TrackedSignal]:
        preferred_keys = preferred_keys or set()
        signals = list(self._signals_by_chat.get(chat_key, {}).values())
        return sorted(
            signals,
            key=lambda signal: (
                0 if signal.key in preferred_keys else 1,
                0 if signal.is_active else 1,
                -signal.updated_at.timestamp(),
            ),
        )

    def _index_message(self, chat_key: str, message_id: int, signal_key: str) -> None:
        self._message_index_by_chat.setdefault(chat_key, {})[message_id] = signal_key

    def _prune_chat(self, chat_key: str) -> None:
        signals = self._signals_by_chat.get(chat_key, {})
        overflow = len(signals) - self.max_signals_per_chat
        if overflow <= 0:
            return

        for stale_signal in reversed(self._sorted_signals(chat_key)):
            if overflow <= 0:
                break
            self._remove_signal(chat_key, stale_signal.key)
            overflow -= 1

    def _remove_signal(self, chat_key: str, signal_key: str) -> None:
        signal = self._signals_by_chat.get(chat_key, {}).pop(signal_key, None)
        if signal is None:
            return
        message_index = self._message_index_by_chat.get(chat_key, {})
        for related_message_id in list(signal.related_message_ids):
            if message_index.get(related_message_id) == signal_key:
                message_index.pop(related_message_id, None)


def _lifecycle_after_execution(
    signal_snapshot: ParsedSignal,
    result: ExecutionResult,
    *,
    previous: SignalLifecycle,
) -> SignalLifecycle:
    action = result.action

    if action == SignalAction.CANCEL or action == "cancel":
        return "canceled"
    if action == SignalAction.CLOSE or action == "close":
        return "closed" if (result.close_fraction or 1.0) >= 1.0 else "executed"
    if action == SignalAction.AMEND or action == "amend":
        return "amended"
    if action == SignalAction.OPEN or action == "open":
        raw_status = str(result.raw_entry_status or "").strip().lower()
        if signal_snapshot.entry_type == "limit" and raw_status in {"open", "new", "pending"}:
            return "pending_entry"
        if signal_snapshot.entry_type == "limit" and not raw_status:
            return "pending_entry"
        return "executed"
    return previous

def _values_match(left: float | None, right: float | None, relative_tolerance: float = 1e-6) -> bool:
    if left is None or right is None:
        return left is None and right is None
    scale = max(abs(left), abs(right), 1e-12)
    return abs(left - right) <= scale * relative_tolerance


def _target_lists_match(left: list[float], right: list[float]) -> bool:
    if len(left) != len(right):
        return False
    return all(_values_match(item_left, item_right) for item_left, item_right in zip(left, right))


def _excerpt(text: str, limit: int = 180) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."
