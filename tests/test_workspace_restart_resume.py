from __future__ import annotations

import asyncio
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_runtime import BotSupervisor
from bridge_logging import LogStore
from config_manager import ConfigManager


class TrackingWorkspaceRegistry:
    def __init__(self) -> None:
        self.resume_started = threading.Event()
        self.shutdown_called = False

    async def resume_all(self) -> None:
        self.resume_started.set()

    async def shutdown_all(self) -> None:
        self.shutdown_called = True

    def workspace_count(self) -> int:
        return 0


class WorkspaceRestartResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        workspace_dir = Path(self.temp_dir.name) / "workspace"
        self.config_manager = ConfigManager(
            config_path=workspace_dir / "config.json",
            master_key_path=workspace_dir / "master.key",
        )
        config = self.config_manager.initialize_empty_config()
        config.bot_should_run = True
        self.config_manager.save_config(config)
        self.runtime = BotSupervisor(self.config_manager, LogStore(file_path=workspace_dir / "activity.log"))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_process_shutdown_preserves_the_workspace_restart_intent(self) -> None:
        asyncio.run(self.runtime.shutdown())

        self.assertTrue(self.config_manager.load_config().bot_should_run)

    def test_user_stop_clears_the_workspace_restart_intent(self) -> None:
        asyncio.run(self.runtime.stop_bot())

        self.assertFalse(self.config_manager.load_config().bot_should_run)

    def test_api_starts_workspace_resume_in_the_background(self) -> None:
        try:
            from api_server import create_app
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            self.skipTest(f"API lifecycle dependencies are not installed: {exc.name}")

        registry = TrackingWorkspaceRegistry()
        with patch.dict("os.environ", {"SIGNALBRIDGE_ENV": "test", "ENVIRONMENT": "test"}, clear=False):
            app = create_app(workspace_registry=registry)  # type: ignore[arg-type]
            with TestClient(app):
                self.assertTrue(registry.resume_started.wait(timeout=1))

        self.assertTrue(registry.shutdown_called)
