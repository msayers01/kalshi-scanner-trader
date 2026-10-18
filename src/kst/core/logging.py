"""structlog configuration: pretty console + JSON lines file (data/logs/kst.jsonl)."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog

_configured = False

_SECRET_KEYS = {"token", "api_key", "private_key", "secret", "authorization", "password", "signature"}
_SECRET_SUFFIXES = ("_token", "_secret", "_password", "private_key", "api_key")


def _redact(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for k in list(event_dict):
        kl = k.lower()
        if kl in _SECRET_KEYS or kl.endswith(_SECRET_SUFFIXES):
            event_dict[k] = "***"
    return event_dict


def configure_logging(level: str = "INFO", log_dir: Path | None = None, json_console: bool = False) -> None:
    global _configured
    lvl = getattr(logging, level.upper(), logging.INFO)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
        structlog.processors.StackInfoRenderer(),
    ]

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(lvl)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer()
            if json_console
            else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
            foreign_pre_chain=shared,
        )
    )
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "kst.jsonl", maxBytes=50_000_000, backupCount=10, encoding="utf-8"
        )
        fh.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(),
                foreign_pre_chain=[*shared, structlog.processors.format_exc_info],
            )
        )
        root.addHandler(fh)

    for noisy in ("httpx", "httpcore", "websockets", "discord", "uvicorn.access", "aiosqlite", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(max(lvl, logging.WARNING))

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
