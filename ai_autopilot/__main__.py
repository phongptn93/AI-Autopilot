"""CLI entry point: ``python -m ai_autopilot`` / ``ai-autopilot``."""

from __future__ import annotations

import uvicorn

from ai_autopilot.config import load_settings


def main() -> None:
    config = load_settings()
    uvicorn.run(
        "ai_autopilot.app:create_app",
        factory=True,
        host=config.health_host,
        port=config.health_port,
        log_config=None,  # logging is configured inside create_app()
    )


if __name__ == "__main__":
    main()
