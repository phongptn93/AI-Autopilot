"""Discover, load, and run plugins (ported from ``PluginManager``/``PluginLoader``)."""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from typing import Any

from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.plugins.base import Plugin, PostProcessor, PreProcessor, SkillProvider


class PluginManager:
    def __init__(self) -> None:
        self._plugins: list[Plugin] = []
        self._log = get_logger("plugins.manager")

    @property
    def plugins(self) -> list[Plugin]:
        return list(self._plugins)

    @property
    def pre_processors(self) -> list[PreProcessor]:
        return [p for p in self._plugins if isinstance(p, PreProcessor)]

    @property
    def post_processors(self) -> list[PostProcessor]:
        return [p for p in self._plugins if isinstance(p, PostProcessor)]

    @property
    def skill_providers(self) -> list[SkillProvider]:
        return [p for p in self._plugins if isinstance(p, SkillProvider)]

    async def load_and_init(self, plugins_directory: str, services: Any) -> None:
        for plugin in self._discover(plugins_directory):
            try:
                await plugin.initialize(services)
                self._plugins.append(plugin)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("plugin failed to initialize", name=plugin.name, error=str(exc))
        self._log.info("plugin manager ready", count=len(self._plugins))

    def _discover(self, plugins_directory: str) -> list[Plugin]:
        directory = Path(plugins_directory)
        if not plugins_directory or not directory.is_dir():
            self._log.debug("no plugins directory", dir=plugins_directory)
            return []

        discovered: list[Plugin] = []
        for file in sorted(directory.glob("*.py")):
            if file.name.startswith("_"):
                continue
            try:
                module = self._import_file(file)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("failed to load plugin file", file=str(file), error=str(exc))
                continue
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, Plugin)
                    and obj not in (Plugin, PreProcessor, PostProcessor, SkillProvider)
                    and obj.__module__ == module.__name__
                ):
                    instance = obj()
                    discovered.append(instance)
                    self._log.info(
                        "loaded plugin", name=instance.name, version=instance.version
                    )
        return discovered

    @staticmethod
    def _import_file(file: Path):
        spec = importlib.util.spec_from_file_location(f"ai_autopilot_plugin_{file.stem}", file)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot import {file}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    async def run_pre_processors(self, item: WorkItemInfo) -> WorkItemInfo:
        for pp in self.pre_processors:
            try:
                item = await pp.pre_process(item)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("pre-processor failed", name=pp.name, error=str(exc))
        return item

    async def run_post_processors(self, item: WorkItemInfo, result: ExecutionResult) -> None:
        for pp in self.post_processors:
            try:
                await pp.post_process(item, result)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("post-processor failed", name=pp.name, error=str(exc))
