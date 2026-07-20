"""
Replay historical Telegram channel messages through the SignalBridge parser.

This is a dry-run analysis tool. It does not create exchange clients, place
orders, cancel orders, or mutate live trading state. It uses the existing local
Telegram session and backend-owned parser key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telethon import TelegramClient

from config_manager import ConfigManager
from signal_context import SignalRegistry
from signal_parser import NoTradeSignalError, ParsedSignal, SignalParser, SignalParserError
from trader import ExecutionResult


@dataclass(slots=True)
class ReplayStats:
    scanned: int = 0
    parsed: int = 0
    ignored: int = 0
    failed: int = 0
    opens: int = 0
    updates: int = 0
    closes: int = 0
    cancels: int = 0


async def main() -> None:
    load_dotenv()
    args = parse_args()
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    manager = ConfigManager()
    config = manager.load_config()
    parser_key = manager.resolve_parser_api_key(config)
    if not parser_key:
        raise SystemExit("Parser API key is not configured. Set SIGNALBRIDGE_PARSER_API_KEY or equivalent.")

    api_id = manager.resolve_telegram_api_id(config)
    api_hash = manager.resolve_telegram_api_hash(config)
    if not api_id or not api_hash:
        raise SystemExit("Telegram app credentials are not configured.")

    parser = SignalParser(parser_key, config.openai)
    registry = SignalRegistry(max_signals_per_chat=args.context_size)
    stats = ReplayStats()

    session_name = args.session_name or manager.resolve_telegram_session_name(config)
    client = TelegramClient(session_name, api_id, api_hash)
    try:
        await client.connect()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise SystemExit(
                "Telegram session database is locked. Stop the running bot before replaying history, "
                "or use a dedicated replay session with --session-name signalbridge_replay."
            ) from exc
        raise
    try:
        if not await client.is_user_authorized():
            raise SystemExit(
                f"Telegram session '{session_name}' is not authenticated. "
                "Use the dashboard session after stopping the bot, or create/authenticate a dedicated replay session."
            )

        chat_ref = normalize_chat_ref(args.chat)
        entity = await client.get_entity(chat_ref)
        chat_name = getattr(entity, "username", None) or getattr(entity, "title", None) or str(chat_ref)
        chat_key = str(getattr(entity, "id", None) or chat_ref)

        messages = await collect_messages(
            client=client,
            entity=entity,
            limit=args.limit,
            oldest=args.oldest,
        )

        with output_path.open("w", encoding="utf-8") as output:
            for message in messages:
                record = await replay_message(
                    parser=parser,
                    registry=registry,
                    chat_key=chat_key,
                    chat_name=chat_name,
                    message=message,
                    stats=stats,
                    include_text=args.include_text,
                )
                output.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                output.write("\n")

        summary = {
            "chat": chat_name,
            "chat_ref": str(chat_ref),
            "limit": args.limit,
            "output": str(output_path),
            "stats": asdict(stats),
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
    finally:
        await client.disconnect()


async def replay_message(
    *,
    parser: SignalParser,
    registry: SignalRegistry,
    chat_key: str,
    chat_name: str,
    message: Any,
    stats: ReplayStats,
    include_text: bool,
) -> dict[str, Any]:
    stats.scanned += 1
    message_id = getattr(message, "id", None)
    raw_text = getattr(message, "raw_text", "") or ""
    reply_to_message_id = getattr(message, "reply_to_msg_id", None)
    reply_to_text = ""

    if reply_to_message_id is not None:
        try:
            reply_message = await message.get_reply_message()
            reply_to_text = (getattr(reply_message, "raw_text", None) or "").strip()
        except Exception:
            reply_to_text = ""

    context = registry.build_parser_context(
        chat_key=chat_key,
        chat_name=chat_name,
        message_id=message_id,
        reply_to_message_id=reply_to_message_id,
        reply_to_text=reply_to_text,
        event_type="new_message",
    )

    base_record: dict[str, Any] = {
        "message_id": message_id,
        "date": iso_datetime(getattr(message, "date", None)),
        "reply_to_message_id": reply_to_message_id,
        "text_excerpt": excerpt(raw_text),
    }
    if include_text:
        base_record["text"] = raw_text

    try:
        parsed = await parser.parse(raw_text, context)
    except NoTradeSignalError as exc:
        stats.ignored += 1
        return {**base_record, "status": "ignored", "reason": str(exc)}
    except SignalParserError as exc:
        stats.failed += 1
        return {**base_record, "status": "failed", "error": exc.__class__.__name__, "detail": str(exc)}
    except Exception as exc:
        stats.failed += 1
        return {**base_record, "status": "failed", "error": exc.__class__.__name__, "detail": str(exc)[:300]}

    stats.parsed += 1
    update_action_stats(stats, parsed)
    tracked_signal = registry.resolve_reference(
        chat_key=chat_key,
        parsed=parsed,
        message_id=message_id,
        reply_to_message_id=reply_to_message_id,
    )
    fake_result = fake_execution_result(parsed)
    if fake_result is not None:
        registry.record_execution(
            chat_key=chat_key,
            chat_name=chat_name,
            message_id=message_id,
            raw_message=raw_text,
            signal_snapshot=parsed,
            result=fake_result,
            tracked_signal=tracked_signal,
        )

    return {
        **base_record,
        "status": "parsed",
        "parsed": parsed.model_dump(mode="json", exclude={"source_message"}),
        "referenced_signal_key": tracked_signal.key if tracked_signal is not None else None,
    }


def fake_execution_result(parsed: ParsedSignal) -> ExecutionResult | None:
    if parsed.symbol is None:
        return None
    return ExecutionResult(
        action=parsed.action,
        symbol=parsed.symbol,
        side=parsed.side,
        amount=0.0,
        raw_entry_status="open" if parsed.entry_type == "limit" else "closed",
        message="dry-run parser replay",
    )


def update_action_stats(stats: ReplayStats, parsed: ParsedSignal) -> None:
    action = str(parsed.action)
    if action == "open":
        stats.opens += 1
    elif action == "close":
        stats.closes += 1
    elif action == "cancel":
        stats.cancels += 1
    else:
        stats.updates += 1


def normalize_chat_ref(value: str) -> int | str:
    stripped = value.strip()
    if stripped.startswith("@"):
        stripped = stripped[1:]
    if stripped.lstrip("-").isdigit():
        return int(stripped)
    return stripped


def iso_datetime(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def excerpt(value: str, limit: int = 220) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run Telegram channel history through SignalBridge parser.")
    parser.add_argument("--chat", required=True, help="Telegram username, @username, channel ID, or selected chat reference.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum historical messages to fetch.")
    parser.add_argument("--output", default="tmp/history_replay.jsonl", help="JSONL output path.")
    parser.add_argument("--context-size", type=int, default=200, help="Tracked parser context size per chat.")
    parser.add_argument("--include-text", action="store_true", help="Include full raw message text in output.")
    parser.add_argument("--oldest", action="store_true", help="Replay the oldest messages instead of the latest messages.")
    parser.add_argument(
        "--session-name",
        default="",
        help="Optional Telegram session name for replay. Use a dedicated session to avoid locking the live bot session.",
    )
    return parser.parse_args()


async def collect_messages(*, client: TelegramClient, entity: Any, limit: int, oldest: bool) -> list[Any]:
    """Collect messages in chronological order.

    Telethon returns newest-first by default. For replay we want chronological
    order so replies and follow-up updates can build context correctly.
    """

    if oldest:
        return [
            message
            async for message in client.iter_messages(entity, limit=limit, reverse=True)
            if getattr(message, "raw_text", None)
        ]

    newest_first = [
        message
        async for message in client.iter_messages(entity, limit=limit)
        if getattr(message, "raw_text", None)
    ]
    return list(reversed(newest_first))


if __name__ == "__main__":
    asyncio.run(main())
