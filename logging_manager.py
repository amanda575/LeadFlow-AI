"""Rotating, category-aware logging for LeadFlow AI.

Every subsystem logs through a named logger obtained from :func:`get_logger`.
All loggers share a single timed-rotating file handler (rotates daily, keeps
``LOG_RETENTION_DAYS`` days) plus a console handler, and they additionally
persist structured rows into the ``Log`` database table so the dashboard "Logs"
page can search and filter them.

Categories are free-form strings but the app standardises on:
``import``, ``smtp``, ``reply``, ``error``, ``database``, ``scheduler``,
``dashboard`` and ``auth``.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

from config import config

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s"
_configured = False

# Optional hook installed by the database layer so log records are also written
# to the Log table. Set via :func:`set_db_sink`. Kept loosely coupled to avoid a
# hard import cycle between logging and the database modules.
_db_sink = None  # type: ignore[var-annotated]


def set_db_sink(sink) -> None:
    """Register a callable ``sink(category, level, message)`` for DB persistence."""
    global _db_sink
    _db_sink = sink


class _DBHandler(logging.Handler):
    """Forwards log records to the registered database sink, if any."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        if _db_sink is None:
            return
        try:
            category = getattr(record, "category", record.name.split(".")[-1])
            _db_sink(category, record.levelname, record.getMessage())
        except Exception:  # pragma: no cover - logging must never crash the app
            # Deliberately swallow: a failing log sink should not raise.
            pass


def configure_logging(logs_dir: Optional[Path] = None) -> None:
    """Idempotently configure root logging handlers."""
    global _configured
    if _configured:
        return

    logs_dir = logs_dir or config.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("leadflow")
    root.setLevel(logging.INFO)
    root.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT)

    file_handler = TimedRotatingFileHandler(
        filename=str(logs_dir / "leadflow.log"),
        when="midnight",
        interval=1,
        backupCount=config.log_retention_days,
        encoding="utf-8",
        utc=False,
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    db_handler = _DBHandler()
    db_handler.setLevel(logging.INFO)

    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.addHandler(db_handler)

    _configured = True


class CategoryLoggerAdapter(logging.LoggerAdapter):
    """Injects a ``category`` field used by the DB handler and dashboard."""

    def process(self, msg, kwargs):  # type: ignore[override]
        extra = kwargs.setdefault("extra", {})
        extra.setdefault("category", self.extra.get("category", "general"))
        return msg, kwargs


def get_logger(category: str) -> CategoryLoggerAdapter:
    """Return a logger bound to a category (e.g. ``"smtp"``, ``"reply"``)."""
    if not _configured:
        configure_logging()
    base = logging.getLogger(f"leadflow.{category}")
    return CategoryLoggerAdapter(base, {"category": category})
