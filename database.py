"""Database engine, session management and first-run seeding for LeadFlow AI.

This module owns the single SQLAlchemy :class:`Engine` and a thread-safe
``scoped_session`` factory (the scheduler and Flask request handlers run on
different threads). It also seeds default settings, the default follow-up
sequence and starter templates, and wires the logging DB sink so application
logs are persisted to the ``logs`` table.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, scoped_session, sessionmaker

import logging_manager
from config import Config, config
from models import (
    Base,
    FollowUpSequence,
    Log,
    Setting,
    Template,
)

# --------------------------------------------------------------------------- #
# Engine / session factory
# --------------------------------------------------------------------------- #

_engine: Optional[Engine] = None
_SessionFactory: Optional[scoped_session] = None


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001
    """Enable foreign keys and WAL for better concurrency under SQLite."""
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
    except Exception:
        # Non-SQLite backends will ignore these; never block a connection.
        pass


def init_engine(cfg: Config = config) -> Engine:
    """Create (once) and return the global engine."""
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine

    _engine = create_engine(
        cfg.database_url,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )
    _SessionFactory = scoped_session(
        sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    )
    return _engine


def get_session() -> Session:
    """Return a session from the scoped factory (caller manages lifecycle)."""
    if _SessionFactory is None:
        init_engine()
    assert _SessionFactory is not None
    return _SessionFactory()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error, always close."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Logging DB sink
# --------------------------------------------------------------------------- #

def _db_log_sink(category: str, level: str, message: str) -> None:
    """Persist a log line to the ``logs`` table (best-effort)."""
    try:
        session = get_session()
        try:
            session.add(Log(category=category, level=level, message=message))
            session.commit()
        finally:
            session.close()
    except Exception:
        # Never let logging failures bubble up.
        pass


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #

DEFAULT_SETTINGS = {
    "business_start_hour": str(config.business_hours.start_hour),
    "business_end_hour": str(config.business_hours.end_hour),
    "business_timezone": config.business_hours.timezone,
    "business_weekdays_only": "true" if config.business_hours.weekdays_only else "false",
    "notify_desktop": "true" if config.notifications.desktop else "false",
    "notify_sound": "true" if config.notifications.sound else "false",
    "notify_console": "true" if config.notifications.console else "false",
    "theme": "dark",
    "scheduler_import_minutes": str(config.scheduler.import_interval_minutes),
    "scheduler_reply_minutes": str(config.scheduler.reply_interval_minutes),
    "scheduler_send_minutes": str(config.scheduler.send_interval_minutes),
    "max_retry_count": str(config.smtp.max_retries),
    "auto_remove_label": "true" if config.gmail.auto_remove_label else "false",
}

_DEFAULT_TEMPLATES = [
    {
        "name": "followup1.html",
        "subject": "Following up, {{ name }}",
        "description": "First gentle follow-up (default step 1).",
        "html_body": (
            "<p>Hi {{ name }},</p>\n"
            "<p>I wanted to gently follow up on my previous email regarding "
            "how {{ company or 'your business' }} could attract more qualified "
            "leads through SEO.</p>\n"
            "<p>Would you be open to a quick 15-minute chat this week?</p>\n"
            "<p>Best,<br>Amanda<br>SeoLeads.Me</p>"
        ),
        "text_body": (
            "Hi {{ name }},\n\n"
            "I wanted to gently follow up on my previous email regarding how "
            "{{ company or 'your business' }} could attract more qualified leads "
            "through SEO.\n\n"
            "Would you be open to a quick 15-minute chat this week?\n\n"
            "Best,\nAmanda\nSeoLeads.Me"
        ),
    },
    {
        "name": "followup2.html",
        "subject": "One more note for you, {{ name }}",
        "description": "Second, final follow-up (default step 2).",
        "html_body": (
            "<p>Hi {{ name }},</p>\n"
            "<p>I don't want to be a pest, so this will be my last note for now. "
            "If growing {{ company or 'your' }} organic traffic is a priority "
            "this quarter, I'd love to share a few quick wins I spotted.</p>\n"
            "<p>Just reply \"yes\" and I'll send them over.</p>\n"
            "<p>Warm regards,<br>Amanda<br>SeoLeads.Me</p>"
        ),
        "text_body": (
            "Hi {{ name }},\n\n"
            "I don't want to be a pest, so this will be my last note for now. If "
            "growing {{ company or 'your' }} organic traffic is a priority this "
            "quarter, I'd love to share a few quick wins I spotted.\n\n"
            "Just reply \"yes\" and I'll send them over.\n\n"
            "Warm regards,\nAmanda\nSeoLeads.Me"
        ),
    },
]

_DEFAULT_SEQUENCE = [
    {"step_number": 1, "delay_days": 2, "template_name": "followup1.html"},
    {"step_number": 2, "delay_days": 4, "template_name": "followup2.html"},
]


def seed_defaults() -> None:
    """Insert default settings, templates and sequence steps if missing."""
    with session_scope() as session:
        for key, value in DEFAULT_SETTINGS.items():
            if session.get(Setting, key) is None:
                session.add(Setting(key=key, value=value))

        for tpl in _DEFAULT_TEMPLATES:
            exists = (
                session.query(Template).filter_by(name=tpl["name"]).first()
            )
            if exists is None:
                session.add(Template(**tpl))

        for step in _DEFAULT_SEQUENCE:
            exists = (
                session.query(FollowUpSequence)
                .filter_by(step_number=step["step_number"])
                .first()
            )
            if exists is None:
                session.add(FollowUpSequence(enabled=True, **step))


def init_db(cfg: Config = config) -> None:
    """Create tables, wire logging sink and seed defaults. Safe to call twice."""
    init_engine(cfg)
    logging_manager.configure_logging(cfg.logs_dir)
    logging_manager.set_db_sink(_db_log_sink)
    assert _engine is not None
    Base.metadata.create_all(_engine)
    seed_defaults()


# --------------------------------------------------------------------------- #
# Settings convenience helpers
# --------------------------------------------------------------------------- #

def get_setting(key: str, default: str = "") -> str:
    session = get_session()
    try:
        row = session.get(Setting, key)
        return row.value if row else default
    finally:
        session.close()


def get_setting_bool(key: str, default: bool = False) -> bool:
    raw = get_setting(key, "true" if default else "false")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_setting_int(key: str, default: int) -> int:
    try:
        return int(get_setting(key, str(default)))
    except (ValueError, TypeError):
        return default


def set_setting(key: str, value: str) -> None:
    with session_scope() as session:
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
