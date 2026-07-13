"""Test configuration.

Isolate the test suite from any developer ``config.yaml`` sitting in the repo
root: point ``AUTOPILOT_CONFIG_FILE`` at a non-existent path *before*
``ai_autopilot.config`` is imported, so ``Settings()`` falls back to model
defaults and tests stay deterministic regardless of local config.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ["AUTOPILOT_CONFIG_FILE"] = str(Path(__file__).parent / "__no_such_config__.yaml")
