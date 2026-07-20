"""
SignalBridge runtime entry point.

The API bridge now owns the bot supervisor lifecycle. The dashboard uses the
API to authenticate Telegram, start and stop the worker, and inspect logs and
runtime status.
"""

from __future__ import annotations

import os

import uvicorn

from api_server import create_app
from bot_runtime import BotSupervisor
from bridge_logging import LogStore
from config_manager import ConfigManager


def build_app():
    manager = ConfigManager()
    logs = LogStore(max_entries=1_000, file_path="activity.log")
    runtime = BotSupervisor(manager, logs)
    return create_app(manager, logs, runtime)


app = build_app()


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=os.getenv("SIGNALBRIDGE_HOST", "0.0.0.0"),
        port=int(os.getenv("SIGNALBRIDGE_PORT", "8000")),
        log_level=os.getenv("SIGNALBRIDGE_LOG_LEVEL", "info"),
        reload=False,
    )
