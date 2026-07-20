"""
SignalBridge AI signal parser.

The parser converts unstructured Telegram calls into a strict, validated trade
instruction. It supports new entries plus management actions such as close,
cancel, and amend so downstream execution code can respond to the full signal
lifecycle instead of treating every message like a fresh market order.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Literal

from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from config_manager import ModelProvider, OpenAIConfig


class SignalParserError(RuntimeError):
    """Base error for signal parsing failures."""


class NoTradeSignalError(SignalParserError):
    """Raised when a message is not an actionable trade signal."""


class SignalValidationError(SignalParserError):
    """Raised when extracted signal data is internally inconsistent."""


class SignalParserAPIError(SignalParserError):
    """Raised when the OpenAI API call fails."""


REPLY_MARKET_ACTIVATION_NOTE = "reply_market_activation"


class TradeSide(str, Enum):
    """Normalized execution side."""

    BUY = "buy"
    SELL = "sell"


class SignalAction(str, Enum):
    """Top-level instruction types understood by SignalBridge."""

    OPEN = "open"
    CLOSE = "close"
    CANCEL = "cancel"
    AMEND = "amend"


class EntryType(str, Enum):
    """Entry styles supported by the execution engine."""

    MARKET = "market"
    LIMIT = "limit"


class ParserModel(BaseModel):
    """Base model for parser contracts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, use_enum_values=True)


class SignalCandidateContext(ParserModel):
    """Tracked-signal summary passed to the model for reference resolution."""

    key: str = Field(min_length=1)
    origin_message_id: int | None = None
    latest_message_id: int | None = None
    lifecycle: str = ""
    symbol: str | None = None
    side: TradeSide | None = None
    entry_type: EntryType | None = None
    entry_price: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit_targets: list[float] = Field(default_factory=list)
    leverage: int | None = Field(default=None, ge=1, le=125)
    excerpt: str = ""


class SignalContext(ParserModel):
    """Optional per-message parsing context from the Telegram runtime."""

    event_type: Literal["new_message", "edited_message"] = "new_message"
    chat_name: str = ""
    message_id: int | None = None
    reply_to_message_id: int | None = None
    reply_to_text: str = ""
    same_message_signal_key: str | None = None
    reply_signal_key: str | None = None
    candidate_signals: list[SignalCandidateContext] = Field(default_factory=list)


class SignalExtraction(ParserModel):
    """Raw structured output returned by the model."""

    is_trade_signal: bool = Field(
        description="True only when the message contains an actionable crypto futures instruction."
    )
    rejection_reason: str = Field(default="", description="Short reason when is_trade_signal is false.")
    is_signal_update: bool = Field(
        default=False,
        description="True when the message modifies, closes, or cancels a previously issued signal.",
    )
    reference_signal_key: str | None = Field(
        default=None,
        description="Signal candidate key from context when this message refers to a prior signal.",
    )
    action: SignalAction = Field(
        default=SignalAction.OPEN,
        description="open for new entries, close to reduce/exit, cancel to cancel orders, amend to update stops/targets.",
    )
    symbol: str | None = Field(default=None, description="CCXT USDT perpetual symbol, e.g. BTC/USDT:USDT.")
    side: TradeSide | None = Field(
        default=None,
        description="buy for long signals, sell for short signals. Usually omitted for cancel/close/amend.",
    )
    entry_type: EntryType | None = Field(
        default=None,
        description="market when the signal says market/now/current price, limit when a concrete entry price/range is given.",
    )
    entry_price: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    take_profit_targets: list[float] = Field(
        default_factory=list,
        description="All concrete take-profit targets found in the message, in the same order.",
    )
    leverage: int | None = Field(default=None, ge=1, le=125)
    close_fraction: float | None = Field(
        default=None,
        gt=0,
        le=1,
        description="1.0 for full close, 0.5 for close half, etc.",
    )
    move_stop_to_entry: bool = Field(
        default=False,
        description="True when the message means move stop loss to break-even / entry.",
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str = Field(default="", description="Brief parse note for logs.")


class ParsedSignal(ParserModel):
    """Validated instruction consumed by the trading engine."""

    action: SignalAction
    symbol: str | None = None
    side: TradeSide | None = None
    entry_type: EntryType | None = None
    entry_price: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    take_profit_targets: list[float] = Field(default_factory=list)
    leverage: int | None = Field(default=None, ge=1, le=125)
    close_fraction: float | None = Field(default=None, gt=0, le=1)
    move_stop_to_entry: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str = ""
    is_signal_update: bool = False
    reference_signal_key: str | None = None
    source_message: str = Field(default="", repr=False)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        normalized = normalize_bybit_linear_symbol(value)
        if normalized is None:
            raise ValueError("symbol must use CCXT USDT perpetual format, e.g. BTC/USDT:USDT")
        return normalized

    @field_validator("take_profit_targets")
    @classmethod
    def validate_take_profit_targets(cls, value: list[float]) -> list[float]:
        normalized: list[float] = []
        seen: set[float] = set()
        for target in value:
            clean_target = float(target)
            if clean_target <= 0:
                raise ValueError("take_profit_targets values must be greater than zero")
            if clean_target not in seen:
                normalized.append(clean_target)
                seen.add(clean_target)
        return normalized

    @model_validator(mode="after")
    def validate_instruction(self) -> "ParsedSignal":
        targets = list(self.take_profit_targets)

        if self.take_profit is not None and self.take_profit not in targets:
            targets.insert(0, self.take_profit)
        if targets and self.take_profit is None:
            self.take_profit = targets[0]
        self.take_profit_targets = targets

        action = self.action
        if action == SignalAction.OPEN or action == "open":
            self._validate_open_signal()
        elif action == SignalAction.CLOSE or action == "close":
            if self.close_fraction is None:
                self.close_fraction = 1.0
        elif action == SignalAction.CANCEL or action == "cancel":
            pass
        elif action == SignalAction.AMEND or action == "amend":
            if self.stop_loss is None and not self.take_profit_targets and not self.move_stop_to_entry:
                raise ValueError("amend signals require a new stop loss, take profit, or move_stop_to_entry=true")
        else:
            raise ValueError(f"unsupported signal action: {action}")

        return self

    def _validate_open_signal(self) -> None:
        missing_fields: list[str] = []
        if self.symbol in (None, ""):
            missing_fields.append("symbol")
        if self.side in (None, ""):
            missing_fields.append("side")
        if self.entry_type in (None, ""):
            missing_fields.append("entry_type")
        if self.stop_loss is None:
            missing_fields.append("stop_loss")
        if self.take_profit is None and not self.take_profit_targets:
            missing_fields.append("take_profit")
        if missing_fields:
            raise ValueError(f"open signal is missing required fields: {', '.join(missing_fields)}")

        if self.entry_type == EntryType.LIMIT or self.entry_type == "limit":
            if self.entry_price is None:
                raise ValueError("limit entry signals require entry_price")

        if self.entry_price is not None:
            reference_price = self.entry_price
            targets = self.take_profit_targets or ([self.take_profit] if self.take_profit is not None else [])
            if self.side == TradeSide.BUY or self.side == "buy":
                if self.stop_loss is None or self.stop_loss >= reference_price:
                    raise ValueError("long signals require stop_loss below entry_price")
                if any(target <= reference_price for target in targets):
                    raise ValueError("long signals require all take-profit targets above entry_price")
            elif self.side == TradeSide.SELL or self.side == "sell":
                if self.stop_loss is None or self.stop_loss <= reference_price:
                    raise ValueError("short signals require stop_loss above entry_price")
                if any(target >= reference_price for target in targets):
                    raise ValueError("short signals require all take-profit targets below entry_price")


SYSTEM_PROMPT = """
You are SignalBridge's crypto futures signal extraction engine.

Return JSON that matches the provided schema. Extract only actionable crypto
USDT perpetual futures instructions. Do not explain the trade. Do not include
prose outside the JSON.

Rules:
- Convert symbols to CCXT USDT perpetual format: BTC -> BTC/USDT:USDT,
  BTCUSDT -> BTC/USDT:USDT, ETH/USDT -> ETH/USDT:USDT.
- action must be one of:
  - "open" for a new trade entry
  - "close" for close / close half / take partial profit / exit now
  - "cancel" for cancel entry / cancel order / abort setup
  - "amend" for move stop loss, change stop loss, change take profit, break-even
- side must be "buy" for LONG/BULLISH/BUY calls and "sell" for SHORT/BEARISH/SELL calls.
- entry_type must be "market" when the signal says market, now, CMP, current price,
  instant entry, or gives no concrete limit price. Use "limit" when the signal gives
  an explicit entry price or entry range.
- For entry ranges, choose the single best representative entry_price from the range.
- For market entries without a concrete price, leave entry_price as null.
- stop_loss must be the protective invalidation price for open signals. For amend
  signals, set stop_loss only when the message explicitly changes it.
- take_profit_targets must include every concrete TP in the message, in order.
- take_profit must be the first concrete TP target. If there is only one TP, both
  take_profit and take_profit_targets[0] should match.
- For a brand-new open signal, if stop loss or any take-profit target is missing,
  set is_trade_signal=false. Do not treat a bare "buy now" or "short BTC now"
  message as actionable unless the protective levels are explicitly present.
- leverage is null when absent. Never invent leverage.
- close_fraction should be 0.5 for "close half", 0.25 for "close 25%", and 1.0 for
  "close now" / "close all" / full exit.
- move_stop_to_entry must be true for phrases like "move SL to entry", "BE", or
  "break-even stop".
- If the message is commentary, a result update, vague hype, or lacks enough
  information to act, set is_trade_signal=false.
- Ignore referral codes, PnL screenshots, VIP ads, and copied disclaimers.
- Messages may be in any language or mixed language. Infer the trading meaning
  anyway and normalize the JSON fields into English.
- When parsing with context:
  - candidate_signals are previously tracked signals. If the message refers to a
    prior signal, set reference_signal_key to the best matching candidate key.
  - same_message_signal_key means the current Telegram post is an edit of that
    tracked signal and usually refers to it.
  - reply_signal_key means the current Telegram post is replying to that tracked
    signal and should normally refer to it unless the text clearly points to
    something else.
  - If the message updates an existing setup's entry, stop, target, or leverage,
    prefer action="amend" and is_signal_update=true instead of treating it as a
    brand-new open signal.
  - Reply messages like "activate this in market price", "enter now", "CMP",
    "market now", or "same SL and TP" usually mean: inherit the referenced
    signal's symbol, side, stop loss, take-profit targets, and leverage, but
    switch the entry to action="open", entry_type="market", entry_price=null,
    and set is_signal_update=true.
  - For amend messages, include only the fields that are being changed unless
    the message clearly restates the full setup.
""".strip()


class SignalParser:
    """Hosted-model parser for Telegram trading messages."""

    def __init__(self, api_key: str, config: OpenAIConfig | None = None) -> None:
        if not api_key:
            raise ValueError("A parser API key is required for SignalParser")
        self.config = config or OpenAIConfig()
        client_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": self.config.request_timeout_seconds,
        }
        base_url = _provider_base_url(self.config.provider)
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**client_kwargs)

    async def parse(self, message: str, context: SignalContext | None = None) -> ParsedSignal:
        """Parse one Telegram message into an executable instruction."""

        clean_message = self._normalize_message(message)
        if not clean_message:
            raise NoTradeSignalError("message is empty")

        shortcut_signal = await self._maybe_parse_trader_shortcut(clean_message, context)
        if shortcut_signal is not None:
            return shortcut_signal

        parser_input = self._build_parser_input(clean_message, context)

        if self.config.provider == ModelProvider.GROQ or self.config.provider == "groq":
            extraction = await self._parse_with_groq_json_mode(parser_input)
        else:
            extraction = await self._parse_with_responses_api(parser_input)

        if not extraction.is_trade_signal:
            reason = extraction.rejection_reason or "message is not an actionable trade signal"
            raise NoTradeSignalError(reason)

        if context is not None and extraction.reference_signal_key:
            valid_keys = {candidate.key for candidate in context.candidate_signals}
            if extraction.reference_signal_key not in valid_keys:
                extraction.reference_signal_key = None

        try:
            return ParsedSignal(
                action=extraction.action,
                symbol=extraction.symbol,
                side=extraction.side,
                entry_type=extraction.entry_type,
                entry_price=extraction.entry_price,
                stop_loss=extraction.stop_loss,
                take_profit=extraction.take_profit,
                take_profit_targets=extraction.take_profit_targets,
                leverage=extraction.leverage,
                close_fraction=extraction.close_fraction,
                move_stop_to_entry=extraction.move_stop_to_entry,
                confidence=extraction.confidence,
                notes=extraction.notes,
                is_signal_update=extraction.is_signal_update,
                reference_signal_key=extraction.reference_signal_key,
                source_message=clean_message,
            )
        except ValidationError as exc:
            fallback_reason = _non_trade_reason_from_validation(extraction, exc)
            if fallback_reason is not None:
                raise NoTradeSignalError(fallback_reason) from exc
            raise SignalValidationError(str(exc)) from exc

    async def _parse_with_responses_api(self, parser_input: str) -> SignalExtraction:
        try:
            response = await self._client.responses.parse(
                model=self.config.model,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": parser_input},
                ],
                text_format=SignalExtraction,
                temperature=_provider_temperature(self.config.provider),
            )
        except OpenAIError as exc:
            raise SignalParserAPIError(_describe_openai_error(exc, self.config.provider)) from exc

        extraction = getattr(response, "output_parsed", None)
        if extraction is None:
            raise SignalValidationError("OpenAI response did not include parsed structured output")
        if not isinstance(extraction, SignalExtraction):
            try:
                extraction = _validate_signal_extraction_payload(extraction)
            except ValidationError as exc:
                raise SignalValidationError(str(exc)) from exc
        return extraction

    async def _parse_with_groq_json_mode(self, parser_input: str) -> SignalExtraction:
        try:
            response = await self._client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            SYSTEM_PROMPT
                            + "\nReturn exactly one JSON object. Use null for unknown scalar fields, [] for unknown target lists,"
                            + ' and always include "is_trade_signal".'
                        ),
                    },
                    {"role": "user", "content": parser_input},
                ],
                response_format={"type": "json_object"},
                temperature=_provider_temperature(self.config.provider),
            )
        except OpenAIError as exc:
            raise SignalParserAPIError(_describe_openai_error(exc, self.config.provider)) from exc

        content = response.choices[0].message.content if response.choices else ""
        if not content:
            raise SignalValidationError("GROQ response did not include JSON content")

        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise SignalValidationError("GROQ response did not return valid JSON") from exc

        try:
            return _validate_signal_extraction_payload(payload)
        except ValidationError as exc:
            raise SignalValidationError(str(exc)) from exc

    @staticmethod
    def _normalize_message(message: str) -> str:
        compact = re.sub(r"[ \t]+", " ", message.replace("\r\n", "\n").replace("\r", "\n"))
        compact = re.sub(r"\n{3,}", "\n\n", compact)
        return compact.strip()

    @staticmethod
    def _build_parser_input(message: str, context: SignalContext | None) -> str:
        if context is None:
            return message

        payload = {
            "current_message": message,
            "context": context.model_dump(mode="json"),
        }
        return (
            "Parse the current Telegram message using the structured context below. "
            "If the message refers to a prior signal, choose reference_signal_key from the provided candidates.\n"
            + json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
        )

    async def _maybe_parse_trader_shortcut(
        self,
        message: str,
        context: SignalContext | None,
    ) -> ParsedSignal | None:
        """Handle short trader-style reply commands deterministically.

        These messages are often too small or colloquial for consistent model
        extraction, but a trader would interpret them from the replied-to signal.
        """

        if context is None:
            return None
        if not self._looks_like_market_activation(message):
            return None

        reference_signal = await self._resolve_reference_signal_for_shortcut(context)
        if reference_signal is None:
            return None

        if reference_signal.action not in (SignalAction.OPEN, "open"):
            return None

        targets = list(reference_signal.take_profit_targets)
        if not targets and reference_signal.take_profit is not None:
            targets = [reference_signal.take_profit]

        try:
            return ParsedSignal.model_validate(
                {
                    "action": SignalAction.OPEN,
                    "symbol": reference_signal.symbol,
                    "side": reference_signal.side,
                    "entry_type": EntryType.MARKET,
                    "entry_price": None,
                    "stop_loss": reference_signal.stop_loss,
                    "take_profit": targets[0] if targets else reference_signal.take_profit,
                    "take_profit_targets": targets,
                    "leverage": reference_signal.leverage,
                    "close_fraction": None,
                    "move_stop_to_entry": False,
                    "confidence": max(float(reference_signal.confidence or 0.0), 0.92),
                    "notes": REPLY_MARKET_ACTIVATION_NOTE,
                    "is_signal_update": True,
                    "reference_signal_key": context.reply_signal_key or context.same_message_signal_key,
                    "source_message": message,
                }
            )
        except ValidationError:
            return None

    async def _resolve_reference_signal_for_shortcut(self, context: SignalContext) -> ParsedSignal | None:
        for preferred_key in (context.same_message_signal_key, context.reply_signal_key):
            if not preferred_key:
                continue
            candidate = next((item for item in context.candidate_signals if item.key == preferred_key), None)
            parsed_candidate = self._parsed_signal_from_candidate(candidate) if candidate is not None else None
            if parsed_candidate is not None:
                return parsed_candidate

        if len(context.candidate_signals) == 1:
            parsed_candidate = self._parsed_signal_from_candidate(context.candidate_signals[0])
            if parsed_candidate is not None:
                return parsed_candidate

        if context.reply_to_text:
            try:
                parsed_reply = await self.parse(context.reply_to_text, None)
            except SignalParserError:
                return None
            if parsed_reply.action in (SignalAction.OPEN, "open"):
                return parsed_reply

        return None

    @staticmethod
    def _parsed_signal_from_candidate(candidate: SignalCandidateContext | None) -> ParsedSignal | None:
        if candidate is None:
            return None

        targets = list(candidate.take_profit_targets)
        if (
            candidate.symbol is None
            or candidate.side is None
            or candidate.entry_type is None
            or candidate.stop_loss is None
            or not targets
        ):
            return None

        try:
            return ParsedSignal.model_validate(
                {
                    "action": SignalAction.OPEN,
                    "symbol": candidate.symbol,
                    "side": candidate.side,
                    "entry_type": candidate.entry_type,
                    "entry_price": candidate.entry_price,
                    "stop_loss": candidate.stop_loss,
                    "take_profit": targets[0],
                    "take_profit_targets": targets,
                    "leverage": candidate.leverage,
                    "close_fraction": None,
                    "move_stop_to_entry": False,
                    "confidence": 0.9,
                    "notes": candidate.excerpt,
                    "is_signal_update": False,
                    "reference_signal_key": candidate.key,
                    "source_message": candidate.excerpt,
                }
            )
        except ValidationError:
            return None

    @staticmethod
    def _looks_like_market_activation(message: str) -> bool:
        lowered = message.lower()
        compact = re.sub(r"[^a-z0-9%/+# ]+", " ", lowered)

        activation_phrases = (
            "activate",
            "enter now",
            "entry now",
            "execute now",
            "take now",
            "take this",
            "open now",
            "go now",
            "fill now",
            "activate this",
        )
        market_phrases = (
            "market",
            "market price",
            "current price",
            "current market",
            "cmp",
            "at market",
            "in market price",
            "now",
        )
        inheritance_phrases = (
            "same sl",
            "same tp",
            "same stop",
            "same target",
            "same stop loss",
            "same take profit",
        )

        has_activation_phrase = any(phrase in compact for phrase in activation_phrases)
        has_market_phrase = any(phrase in compact for phrase in market_phrases)
        has_inheritance_phrase = any(phrase in compact for phrase in inheritance_phrases)

        return (has_activation_phrase and has_market_phrase) or (has_market_phrase and has_inheritance_phrase)


def normalize_bybit_linear_symbol(value: str | None) -> str | None:
    """Normalize common human symbol forms into CCXT USDT perpetual format."""

    if value is None:
        return None

    symbol = value.strip().upper().replace(" ", "")
    symbol = symbol.replace("-", "/").replace("_", "/")

    exact_match = re.fullmatch(r"([A-Z0-9]{2,20})/USDT:USDT", symbol)
    if exact_match:
        return f"{exact_match.group(1)}/USDT:USDT"

    slash_match = re.fullmatch(r"([A-Z0-9]{2,20})/USDT", symbol)
    if slash_match:
        return f"{slash_match.group(1)}/USDT:USDT"

    compact_match = re.fullmatch(r"([A-Z0-9]{2,20})USDT", symbol)
    if compact_match:
        return f"{compact_match.group(1)}/USDT:USDT"

    bare_match = re.fullmatch(r"[A-Z0-9]{2,20}", symbol)
    if bare_match:
        return f"{symbol}/USDT:USDT"

    return None


def _provider_base_url(provider: ModelProvider | str) -> str | None:
    if provider == ModelProvider.GROQ:
        return "https://api.groq.com/openai/v1"
    return None


def _provider_temperature(provider: ModelProvider | str) -> float:
    if provider == ModelProvider.GROQ:
        return 1e-8
    return 0


def _describe_openai_error(exc: OpenAIError, provider: ModelProvider | str) -> str:
    """Convert SDK exceptions into operator-friendly runtime errors."""

    message = " ".join(str(exc).split()).strip()
    lowered = message.lower()
    provider_name = provider.value if isinstance(provider, ModelProvider) else str(provider or "openai")
    provider_label = provider_name.upper()

    if "insufficient_quota" in lowered or "exceeded your current quota" in lowered:
        return f"{provider_label} API quota exceeded; check billing and usage limits for the configured API key"
    if "invalid_api_key" in lowered or "incorrect api key" in lowered:
        return f"{provider_label} API key was rejected; update the configured API key"
    if "rate limit" in lowered:
        return f"{provider_label} rate limit reached; retry shortly or reduce request volume"
    if message:
        return f"{provider_label} request failed: {message}"
    return f"{provider_label} request failed: {exc.__class__.__name__}"


def parsed_signal_from_dict(data: dict[str, Any]) -> ParsedSignal:
    """Validate a dict as ParsedSignal; useful for tests and API boundaries."""

    try:
        return ParsedSignal.model_validate(data)
    except ValidationError as exc:
        raise SignalValidationError(str(exc)) from exc


def _validate_signal_extraction_payload(payload: Any) -> SignalExtraction:
    """Validate model output while dropping unexpected top-level keys."""

    if isinstance(payload, SignalExtraction):
        return payload

    if isinstance(payload, dict):
        allowed_fields = set(SignalExtraction.model_fields)
        payload = {key: value for key, value in payload.items() if key in allowed_fields}
        _normalize_signal_extraction_payload(payload)
        if payload.get("take_profit_targets") is None:
            payload["take_profit_targets"] = []
        if payload.get("move_stop_to_entry") is None:
            payload["move_stop_to_entry"] = False
        if payload.get("rejection_reason") is None:
            payload["rejection_reason"] = ""
        if payload.get("notes") is None:
            payload["notes"] = ""
        if payload.get("is_signal_update") is None:
            payload["is_signal_update"] = False
        if payload.get("confidence") is None:
            payload["confidence"] = 0.0
        if payload.get("action") is None:
            payload["action"] = SignalAction.OPEN

    return SignalExtraction.model_validate(payload)


def _normalize_signal_extraction_payload(payload: dict[str, Any]) -> None:
    action, implied_side = _normalize_action(payload.get("action"))
    payload["action"] = action
    if implied_side is not None and not payload.get("side"):
        payload["side"] = implied_side

    payload["side"] = _normalize_side(payload.get("side"))
    payload["entry_type"] = _normalize_entry_type(payload.get("entry_type"))
    payload["move_stop_to_entry"] = _normalize_bool(payload.get("move_stop_to_entry"))

    raw_take_profit = payload.get("take_profit")
    for field_name in ("entry_price", "stop_loss", "confidence", "close_fraction"):
        payload[field_name] = _coerce_numeric_field(payload.get(field_name), field_name)
    payload["take_profit"] = _coerce_numeric_field(raw_take_profit, "take_profit")

    payload["leverage"] = _coerce_leverage(payload.get("leverage"))

    take_profit_targets = _coerce_numeric_list(payload.get("take_profit_targets"))
    take_profit_as_list = _coerce_numeric_list(raw_take_profit) if isinstance(raw_take_profit, list) else []
    if take_profit_as_list and not take_profit_targets:
        take_profit_targets = take_profit_as_list
    if take_profit_targets:
        payload["take_profit_targets"] = take_profit_targets
        if payload.get("take_profit") is None or isinstance(payload.get("take_profit"), list):
            payload["take_profit"] = take_profit_targets[0]

    if payload.get("is_trade_signal") is None:
        signal_fields = ("symbol", "side", "entry_type", "entry_price", "stop_loss", "take_profit", "take_profit_targets")
        payload["is_trade_signal"] = any(bool(payload.get(field_name)) for field_name in signal_fields)

    for optional_text_field in ("reference_signal_key", "rejection_reason", "notes", "symbol"):
        if payload.get(optional_text_field) == "":
            payload[optional_text_field] = None if optional_text_field in {"reference_signal_key", "symbol"} else ""


def _normalize_action(value: Any) -> tuple[Any, TradeSide | None]:
    if value is None:
        return SignalAction.OPEN, None
    if isinstance(value, SignalAction):
        return value, None
    lowered = str(value).strip().lower().replace("-", " ").replace("_", " ")
    action_aliases: dict[str, SignalAction] = {
        "open": SignalAction.OPEN,
        "entry": SignalAction.OPEN,
        "enter": SignalAction.OPEN,
        "buy": SignalAction.OPEN,
        "sell": SignalAction.OPEN,
        "long": SignalAction.OPEN,
        "short": SignalAction.OPEN,
        "close": SignalAction.CLOSE,
        "exit": SignalAction.CLOSE,
        "close all": SignalAction.CLOSE,
        "close now": SignalAction.CLOSE,
        "take profit": SignalAction.CLOSE,
        "tp hit": SignalAction.CLOSE,
        "cancel": SignalAction.CANCEL,
        "abort": SignalAction.CANCEL,
        "delete": SignalAction.CANCEL,
        "amend": SignalAction.AMEND,
        "modify": SignalAction.AMEND,
        "update": SignalAction.AMEND,
        "move sl": SignalAction.AMEND,
        "move stop": SignalAction.AMEND,
        "breakeven": SignalAction.AMEND,
        "break even": SignalAction.AMEND,
    }
    implied_side = _normalize_side(lowered) if lowered in {"buy", "sell", "long", "short"} else None
    return action_aliases.get(lowered, value), implied_side


def _normalize_side(value: Any) -> TradeSide | None:
    if value is None:
        return None
    if isinstance(value, TradeSide):
        return value
    lowered = str(value).strip().lower().replace("-", " ").replace("_", " ")
    if lowered in {"buy", "long", "bull", "bullish"}:
        return TradeSide.BUY
    if lowered in {"sell", "short", "bear", "bearish"}:
        return TradeSide.SELL
    return value


def _normalize_entry_type(value: Any) -> EntryType | None:
    if value is None:
        return None
    if isinstance(value, EntryType):
        return value
    lowered = str(value).strip().lower().replace("-", " ").replace("_", " ")
    if lowered in {"market", "now", "cmp", "current", "current price", "market price", "instant"}:
        return EntryType.MARKET
    if lowered in {"limit", "pending", "limit order", "entry range"}:
        return EntryType.LIMIT
    return value


def _normalize_bool(value: Any) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"true", "yes", "y", "1", "be", "breakeven", "break even"}:
        return True
    if lowered in {"false", "no", "n", "0", "none", "null"}:
        return False
    return value


def _coerce_numeric_field(value: Any, field_name: str) -> Any:
    if value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        values = _coerce_numeric_list(value)
        return values[0] if values else None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "n/a", "-"}:
        return None
    if field_name == "close_fraction" and text.endswith("%"):
        number = _first_number(text[:-1])
        return number / 100 if number is not None else value
    if field_name == "close_fraction" and text.lower() in {"half", "close half"}:
        return 0.5
    if field_name == "close_fraction" and text.lower() in {"all", "full", "close all"}:
        return 1.0
    return _first_number(text)


def _coerce_leverage(value: Any) -> Any:
    if value is None or isinstance(value, int):
        return value
    match = re.search(r"\d+", str(value).replace(",", ""))
    return int(match.group(0)) if match is not None else value


def _coerce_numeric_list(value: Any) -> list[float]:
    if value is None:
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, list):
        normalized: list[float] = []
        for item in value:
            number = _coerce_numeric_field(item, "take_profit")
            if isinstance(number, (int, float)):
                normalized.append(float(number))
        return normalized
    text = str(value)
    return [float(match.group(0).replace(",", "")) for match in _NUMBER_PATTERN.finditer(text)]


_NUMBER_PATTERN = re.compile(r"(?<![A-Za-z])-?\d[\d,]*(?:\.\d+)?(?![A-Za-z])")


def _first_number(value: str) -> float | None:
    match = _NUMBER_PATTERN.search(value)
    if match is None:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _non_trade_reason_from_validation(extraction: SignalExtraction, exc: ValidationError) -> str | None:
    """Downgrade incomplete model outputs into non-trade outcomes."""

    action = extraction.action
    if action == SignalAction.OPEN or action == "open":
        missing_fields: list[str] = []
        if extraction.symbol in (None, ""):
            missing_fields.append("symbol")
        if extraction.side in (None, ""):
            missing_fields.append("side")
        if extraction.entry_type in (None, ""):
            missing_fields.append("entry_type")
        if extraction.stop_loss is None:
            missing_fields.append("stop_loss")
        has_take_profit = extraction.take_profit is not None or bool(extraction.take_profit_targets)
        if not has_take_profit:
            missing_fields.append("take_profit")
        if missing_fields:
            return f"incomplete open signal ignored; missing {', '.join(missing_fields)}"

    error_text = str(exc).lower()
    if "unsupported signal action" in error_text:
        return "message is not an actionable trade signal"
    return None


def merged_open_signal(base: ParsedSignal, update: ParsedSignal) -> ParsedSignal:
    """Merge an update message into the latest open-signal snapshot."""

    payload = base.model_dump(mode="python")
    payload["action"] = SignalAction.OPEN
    payload["close_fraction"] = None
    payload["is_signal_update"] = False
    payload["reference_signal_key"] = None
    payload["move_stop_to_entry"] = False

    if update.symbol:
        payload["symbol"] = update.symbol
    if update.side:
        payload["side"] = update.side
    if update.entry_type:
        payload["entry_type"] = update.entry_type
        if update.entry_type == EntryType.MARKET or update.entry_type == "market":
            payload["entry_price"] = None
    if update.entry_price is not None:
        payload["entry_price"] = update.entry_price
    if update.stop_loss is not None:
        payload["stop_loss"] = update.stop_loss
    if update.leverage is not None:
        payload["leverage"] = update.leverage
    if update.take_profit_targets:
        payload["take_profit_targets"] = list(update.take_profit_targets)
        payload["take_profit"] = update.take_profit_targets[0]
    elif update.take_profit is not None:
        payload["take_profit_targets"] = [update.take_profit]
        payload["take_profit"] = update.take_profit
    if update.move_stop_to_entry and payload.get("entry_price") is not None:
        payload["stop_loss"] = payload["entry_price"]
    if update.source_message:
        payload["source_message"] = update.source_message
    if update.notes:
        payload["notes"] = update.notes
    payload["confidence"] = max(float(payload.get("confidence") or 0.0), float(update.confidence or 0.0))
    return ParsedSignal.model_validate(payload)


def amend_signal_from_open_update(base: ParsedSignal, update: ParsedSignal) -> ParsedSignal | None:
    """Translate a tracked signal update into a full protective-order amend.

    Protective orders are replaced as a set. If a channel says only
    "move SL" or only "change TP", keep the unchanged side from the tracked
    signal so execution does not accidentally remove valid protection.
    """

    candidate_targets = list(update.take_profit_targets)
    if not candidate_targets and update.take_profit is not None:
        candidate_targets = [update.take_profit]

    base_targets = list(base.take_profit_targets)
    if not base_targets and base.take_profit is not None:
        base_targets = [base.take_profit]

    stop_changed = update.move_stop_to_entry or (update.stop_loss is not None and update.stop_loss != base.stop_loss)
    targets_changed = bool(candidate_targets and candidate_targets != base_targets)
    if not stop_changed and not targets_changed:
        return None

    next_targets = candidate_targets if targets_changed else base_targets
    next_stop_loss = update.stop_loss if update.stop_loss is not None else base.stop_loss
    if update.move_stop_to_entry:
        next_stop_loss = None

    payload: dict[str, Any] = {
        "action": SignalAction.AMEND,
        "symbol": update.symbol or base.symbol,
        "side": update.side or base.side,
        "entry_type": None,
        "entry_price": None,
        "stop_loss": next_stop_loss,
        "take_profit": next_targets[0] if next_targets else None,
        "take_profit_targets": next_targets,
        "leverage": None,
        "close_fraction": None,
        "move_stop_to_entry": update.move_stop_to_entry,
        "confidence": max(float(base.confidence or 0.0), float(update.confidence or 0.0)),
        "notes": update.notes or base.notes,
        "is_signal_update": True,
        "reference_signal_key": update.reference_signal_key,
        "source_message": update.source_message or base.source_message,
    }

    return ParsedSignal.model_validate(payload)
