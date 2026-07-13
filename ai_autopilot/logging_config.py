"""Structured logging setup (replaces Serilog).

Console output is human-friendly; everything is also routed through the stdlib
``logging`` module so uvicorn/FastAPI logs share the same configuration. A daily
rotating file handler mirrors the .NET ``logs/autopilot-.log`` sink.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import structlog


def configure_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    """Configure stdlib + structlog logging once at startup."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    timestamper = structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S")
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        timestamper,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    console_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        foreign_pre_chain=shared_processors,
    )
    file_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )

    console = logging.StreamHandler()
    console.setFormatter(console_formatter)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_path / "autopilot.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    file_handler.setFormatter(file_formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Quiet noisy third-party loggers (parity with Serilog overrides).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.stdlib.get_logger(name)
