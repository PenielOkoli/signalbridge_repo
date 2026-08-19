"""
Per-workspace runtime service registry.

Each authenticated workspace gets its own configuration, logs, and bot runtime
under a dedicated directory so user accounts remain isolated.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from bridge_logging import LogStore
from bot_runtime import BotSupervisor
from config_manager import ConfigManager


@dataclass(slots=True)
class WorkspaceServices:
    workspace_id: str
    workspace_dir: Path
    config_manager: ConfigManager
    log_store: LogStore
    runtime: BotSupervisor


class WorkspaceServiceRegistry:
    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self.workspace_root = Path(workspace_root or os.getenv("SIGNALBRIDGE_WORKSPACE_ROOT", "workspaces"))
        self._services: dict[str, WorkspaceServices] = {}
        self._lock = asyncio.Lock()

    async def get(self, workspace_id: str) -> WorkspaceServices:
        async with self._lock:
            existing = self._services.get(workspace_id)
            if existing is not None:
                return existing

            workspace_dir = self.workspace_root / workspace_id
            workspace_dir.mkdir(parents=True, exist_ok=True)
            config_manager = ConfigManager(
                config_path=workspace_dir / "config.json",
                master_key_path=workspace_dir / "master.key",
            )
            log_store = LogStore(max_entries=1_000, file_path=workspace_dir / "activity.log")
            runtime = BotSupervisor(config_manager, log_store)
            services = WorkspaceServices(
                workspace_id=workspace_id,
                workspace_dir=workspace_dir,
                config_manager=config_manager,
                log_store=log_store,
                runtime=runtime,
            )
            self._services[workspace_id] = services
            return services

    async def shutdown_all(self) -> None:
        async with self._lock:
            services = list(self._services.values())
            self._services.clear()

        for service in services:
            await service.runtime.shutdown()

    async def resume_all(self) -> None:
        """Re-start every workspace's bot that was actively running when the
        process last stopped. Meant to be run as a background task right
        after startup so it never blocks the API from accepting requests --
        one workspace's stale credentials or a slow Telegram reconnect
        shouldn't hold up every other workspace, or the server itself.
        """
        if not self.workspace_root.exists():
            return
        workspace_ids = sorted(
            entry.name
            for entry in self.workspace_root.iterdir()
            if entry.is_dir() and (entry / "config.json").exists()
        )
        for workspace_id in workspace_ids:
            try:
                services = await self.get(workspace_id)
                await services.runtime.resume_if_should_run()
            except Exception as exc:
                services = self._services.get(workspace_id)
                if services is not None:
                    services.log_store.append(
                        "warning",
                        "resume-on-startup failed for this workspace",
                        error=exc.__class__.__name__,
                        detail=str(exc)[:240],
                    )
            await asyncio.sleep(0.75)

    def workspace_count(self) -> int:
        return len(self._services)
