"""
Central logging setup — single place that owns log level and format.

APP_ENV=dev  → INFO  (local development, verbose audit trail)
APP_ENV=prod → WARNING (production, errors/warnings only)

Usage: `setup_logging()` once at startup (main.py). Per-request tracing via
`set_request_id()` in middleware — every record carries [request_id].
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


def set_request_id(value: str) -> None:
    request_id_ctx.set(value or "-")


def _inject_request_id() -> None:
    """Stamp request_id on every LogRecord at creation.

    A factory (not a logger Filter): filters attached to a logger only run
    for records originating there, so child-logger records would miss the
    field and crash %(request_id)s formatters. The factory covers all
    handlers universally.
    """
    factory = logging.getLogRecordFactory()

    def _record_factory(*args, **kwargs):
        record = factory(*args, **kwargs)
        record.request_id = request_id_ctx.get()
        return record

    logging.setLogRecordFactory(_record_factory)


def setup_logging() -> None:
    """Configure root logging from APP_ENV. Safe to call once at startup."""
    from app.core.config import settings

    level = logging.INFO if settings.APP_ENV == "dev" else logging.WARNING
    _inject_request_id()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s — %(message)s",
    )
    if settings.APP_ENV != "dev":
        # Silence per-request access noise in prod; audit-relevant app logs
        # at WARNING+ still flow.
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
