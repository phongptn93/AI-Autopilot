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
    error_log = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _MalformedRequestFilter) for f in error_log.filters):
        error_log.addFilter(_MalformedRequestFilter())


class _MalformedRequestFilter(logging.Filter):
    """Demote uvicorn's "Invalid HTTP request received." from warning to debug.

    It means bytes arrived that are not HTTP — a TLS handshake against the plain port,
    a scanner's probe. Nothing failed and nothing can be done about it, yet each one
    was a warning line next to real problems. It is counted instead
    (``autopilot_http_client_dropped_total{stage="invalid_request"}``) and still shown
    when the log level is DEBUG.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.WARNING or not record.getMessage().startswith(
            "Invalid HTTP request received"
        ):
            return True
        from ai_autopilot import metrics

        metrics.HTTP_CLIENT_DROPPED_TOTAL.labels(stage="invalid_request").inc()
        record.levelno, record.levelname = logging.DEBUG, "DEBUG"
        return logging.getLogger().isEnabledFor(logging.DEBUG)


def describe_exc(exc: BaseException) -> str:
    """An exception as a log value: its type, and its message when it has one.

    ``str(exc)`` alone is empty for every common httpx transport failure —
    ReadTimeout, ConnectTimeout, ConnectError, ReadError, RemoteProtocolError are
    all raised with no message — so a line reading ``error=`` told the reader
    neither what failed nor that it was a timeout at all.
    """
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.stdlib.get_logger(name)
