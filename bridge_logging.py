"""
SignalBridge runtime logging primitives.
"""

from __future__ import annotations

import glob
import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class LogEntry(BaseModel):
    """Single log entry safe for dashboard rendering."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    level: Literal["debug", "info", "warning", "error", "trade"] = "info"
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class LogStore:
    """Ring buffer with optional JSONL persistence and rotation."""

    # Log rotation: rotate when file exceeds 10MB, keep last 5 rotated files
    _ROTATION_SIZE_BYTES = 10 * 1024 * 1024  # 10MB
    _RETENTION_COUNT = 5

    def __init__(self, max_entries: int = 500, file_path: str | Path | None = "activity.log") -> None:
        self._entries: deque[LogEntry] = deque(maxlen=max_entries)
        self._lock = Lock()
        self.file_path = Path(file_path) if file_path else None

    def append(
        self,
        level: Literal["debug", "info", "warning", "error", "trade"],
        message: str,
        **context: Any,
    ) -> LogEntry:
        entry = LogEntry(level=level, message=message, context=_scrub_context(context))
        with self._lock:
            self._entries.append(entry)
            if self.file_path is not None:
                self._write_entry_to_file(entry)

    def _write_entry_to_file(self, entry: LogEntry) -> None:
        """Write entry to file, rotating if necessary."""
        if not self.file_path:
            return

        self.file_path.parent.mkdir(parents=True, exist_ok=True)

        # Check if rotation is needed before writing
        self._check_and_rotate_if_needed()

        # Append the entry
        try:
            with self.file_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(entry.model_dump(mode="json"), sort_keys=True))
                handle.write("\n")
        except OSError:
            # Silently fail if file operations don't work (e.g., permissions)
            pass

    def _check_and_rotate_if_needed(self) -> None:
        """Rotate log file if it exceeds size threshold."""
        if not self.file_path or not self.file_path.exists():
            return

        try:
            file_size = self.file_path.stat().st_size
            if file_size < self._ROTATION_SIZE_BYTES:
                return

            # Rotate the current file
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            parent = self.file_path.parent
            stem = self.file_path.stem
            suffix = self.file_path.suffix
            rotated_name = f"{stem}.{timestamp}{suffix}"
            rotated_path = parent / rotated_name

            self.file_path.rename(rotated_path)

            # Clean up old rotated files
            self._cleanup_old_rotations()
        except OSError:
            # Silently fail if rotation operations don't work
            pass

    def _cleanup_old_rotations(self) -> None:
        """Remove old rotated log files, keeping only the most recent ones."""
        if not self.file_path:
            return

        try:
            parent = self.file_path.parent
            stem = self.file_path.stem
            suffix = self.file_path.suffix

            # Find all rotated log files (e.g., activity.20240719_150230.log)
            pattern = f"{stem}.????????_??????{suffix}"
            rotated_files = sorted(glob.glob(str(parent / pattern)))

            # Remove files beyond retention count
            if len(rotated_files) > self._RETENTION_COUNT:
                for old_file in rotated_files[: -self._RETENTION_COUNT]:
                    try:
                        Path(old_file).unlink()
                    except OSError:
                        pass
        except Exception:
            # Silently fail if cleanup doesn't work
            pass

    def list(self, limit: int = 200) -> list[LogEntry]:
        safe_limit = max(1, min(limit, len(self._entries) or 1))
        with self._lock:
            return list(reversed(list(self._entries)[-safe_limit:]))

    def count(self) -> int:
        with self._lock:
            return len(self._entries)


def _scrub_context(context: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    secret_markers = ("key", "secret", "token", "password")
    for key, value in context.items():
        if any(marker in key.lower() for marker in secret_markers):
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted
