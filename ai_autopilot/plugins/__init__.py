"""Plugin system (ported from the .NET ``IPlugin`` model).

The .NET version loaded plugin DLLs via reflection. The Python equivalent imports
``*.py`` modules from the configured plugins directory and instantiates any class
that subclasses one of the plugin base classes below.
"""

from ai_autopilot.plugins.base import (
    Plugin,
    PostProcessor,
    PreProcessor,
    SkillProvider,
)
from ai_autopilot.plugins.manager import PluginManager

__all__ = [
    "Plugin",
    "PluginManager",
    "PostProcessor",
    "PreProcessor",
    "SkillProvider",
]
