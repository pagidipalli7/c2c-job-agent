"""structlog JSON logging. Every submission binds a trace_id via bind_trace()."""
from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(json_logs: bool = True, level: int = logging.INFO) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    for noisy in ("httpx", "httpcore", "apscheduler", "anthropic._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if json_logs:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str = "jobagent"):
    return structlog.get_logger(name)


def bind_trace(**kwargs) -> None:
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_trace() -> None:
    structlog.contextvars.clear_contextvars()
