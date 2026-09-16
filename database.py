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
_plain_sessionmaker: Optional[sessionmaker] = None


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
    global _engine, _SessionFactory, _plain_sessionmaker
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
    # A plain (non-scoped) factory for session_scope, so each transactional scope
    # is an INDEPENDENT session. This is essential: helpers like get_setting() and
    # _step_after() open their own session_scope, and if scopes shared one
    # thread-local session, an inner scope's commit/close would prematurely end an
    # outer transaction — silently dropping later writes (e.g. next_followup_at).
    _plain_sessionmaker = sessionmaker(
        bind=_engine, autoflush=False, expire_on_commit=False
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
    """Transactional scope on an INDEPENDENT session (safe to nest).

    Commit on success, rollback on error, always close. Because each call gets
    its own session, opening one scope inside another (directly, or via a helper)
    never disturbs the outer transaction.
    """
    if _plain_sessionmaker is None:
        init_engine()
    assert _plain_sessionmaker is not None
    session = _plain_sessionmaker()
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
        "subject": "",
        "description": "Step 1 - immediate gentle follow-up",
        "html_body": (
            "<p>Hi {{ name }},</p>\n"
            "<p>Just following up to see if you had a chance to review the "
            "information I sent over.</p>\n"
            "<p>If you have any questions about our lead quality, niches, "
            "delivery process, or pricing, I'd be happy to answer them.</p>\n"
            "<p>Whenever you're ready, we're here to help you get started.</p>\n"
            "<p>Looking forward to hearing from you.</p>\n"
            "<p>Regards,<br>Amanda</p>"
        ),
        "text_body": (
            "Hi {{ name }},\n\n"
            "Just following up to see if you had a chance to review the "
            "information I sent over.\n"
            "If you have any questions about our lead quality, niches, delivery "
            "process, or pricing, I'd be happy to answer them.\n\n"
            "Whenever you're ready, we're here to help you get started.\n\n"
            "Looking forward to hearing from you.\n\n"
            "Regards,\nAmanda"
        ),
    },
    {
        "name": "followup2.html",
        "subject": "",
        "description": "Step 2 - after 2 days",
        "html_body": (
            "<p>Hi {{ name }},</p>\n"
            "<p>I just wanted to follow up and ask if there's anything holding "
            "you back from moving forward.</p>\n"
            "<p>Is it the pricing, the lead quality, or is there something else "
            "on your mind?</p>\n"
            "<p>Let me know—I'm happy to answer any questions or clear up any "
            "concerns.</p>\n"
            "<p>Regards,<br>Amanda</p>"
        ),
        "text_body": (
            "Hi {{ name }},\n\n"
            "I just wanted to follow up and ask if there's anything holding you "
            "back from moving forward.\n\n"
            "Is it the pricing, the lead quality, or is there something else on "
            "your mind?\n\n"
            "Let me know—I'm happy to answer any questions or clear up any "
            "concerns.\n\n"
            "Regards,\nAmanda"
        ),
    },
    {
        "name": "followup3.html",
        "subject": "",
        "description": "Step 3 - after 2 more days",
        "html_body": (
            "<p>Hi {{ name }},</p>\n"
            "<p>Is everything alright?</p>\n"
            "<p>I noticed I haven't heard back from you in a while, so I just "
            "wanted to check in.</p>\n"
            "<p>If this isn't something you need right now, just let me know. "
            "Even a quick yes or no would be appreciated.</p>\n"
            "<p>Regards,<br>Amanda</p>"
        ),
        "text_body": (
            "Hi {{ name }},\n\n"
            "Is everything alright?\n\n"
            "I noticed I haven't heard back from you in a while, so I just wanted "
            "to check in.\n\n"
            "If this isn't something you need right now, just let me know. Even a "
            "quick yes or no would be appreciated.\n\n"
            "Regards,\nAmanda"
        ),
    },
    {
        "name": "firstmessage.html",
        "subject": "",
        "description": "One-off first message (First Message label)",
        "html_body": (
            "<p>Hi {{ name }},</p>\n"
            "<p>Thank you so much for your time.</p>\n"
            "<p>Let me break this down simply so you can see exactly how we work "
            "and what you're getting.</p>\n"
            "<p><strong>How Our Leads Work</strong></p>\n"
            "<p>We generate exclusive, real-time leads from business owners who are "
            "actively looking for SEO or web design services.</p>\n"
            "<p>No recycled lists.<br>No shared data.<br>No low-quality scraped "
            "contacts.</p>\n"
            "<p><strong>Pricing</strong></p>\n"
            "<p><strong>Exclusive Leads — $15 per lead</strong><br>Inbound or "
            "engaged prospects who have shown genuine interest.</p>\n"
            "<p><strong>Appointment-Ready Leads — $25 per lead</strong><br>Warmer "
            "prospects who have already requested a callback or proposal, and we've "
            "confirmed their intent.</p>\n"
            "<p><strong>Every Lead Includes</strong></p>\n"
            "<ul><li>U.S.-based businesses</li><li>Manually screened (not scraped)"
            "</li><li>Filtered by service need and urgency</li><li>Delivered in real "
            "time as they come in</li><li>100% exclusive to your business</li></ul>\n"
            "<p><strong>Test Pack — $399</strong><br>A great way to start without "
            "overcommitting.</p>\n"
            "<p>Includes:</p>\n"
            "<ul><li>10 Appointment-Ready Leads</li><li>12 High-Intent Exclusive "
            "Leads</li><li>100% Exclusive</li><li>Free replacements if anything is "
            "off</li><li>Delivery over approximately 10–15 days</li></ul>\n"
            "<p><strong>What You'll Receive</strong></p>\n"
            "<p>Every lead includes:</p>\n"
            "<ul><li>Contact Name</li><li>Company Name</li><li>Phone Number</li>"
            "<li>Email Address</li><li>Website</li><li>Services they're looking for"
            "</li><li>Context (e.g. \"Requested proposal\", \"Needs callback ASAP\")"
            "</li></ul>\n"
            "<p><strong>Industries We Currently Serve</strong></p>\n"
            "<ul><li>Home Services</li><li>Real Estate</li><li>Legal</li><li>Health "
            "&amp; Wellness</li><li>Trades</li><li>Events</li></ul>\n"
            "<p>We can also dial into specific niches based on your requirements.</p>\n"
            "<p><strong>Scaling</strong></p>\n"
            "<p>Once we identify what converts best for you, scaling to 40+ "
            "exclusive leads per week is straightforward.</p>\n"
            "<p><strong>Recent Improvements</strong></p>\n"
            "<p>We've waived the setup fee.</p>\n"
            "<p>We're also leveraging a newly expanded data source, allowing us to "
            "consistently deliver higher-quality and more reliable leads.</p>\n"
            "<p>If everything looks good, I can have the invoice sent over and "
            "prepare your first delivery.</p>\n"
            "<p>Looking forward to working with you!</p>\n"
            "<p>Regards,<br>Amanda</p>"
        ),
        "text_body": (
            "Hi {{ name }},\n\n"
            "Thank you so much for your time.\n\n"
            "Let me break this down simply so you can see exactly how we work and "
            "what you're getting.\n\n"
            "How Our Leads Work\n"
            "We generate exclusive, real-time leads from business owners who are "
            "actively looking for SEO or web design services.\n"
            "No recycled lists.\nNo shared data.\nNo low-quality scraped contacts.\n\n"
            "Pricing\n"
            "Exclusive Leads — $15 per lead: Inbound or engaged prospects who have "
            "shown genuine interest.\n"
            "Appointment-Ready Leads — $25 per lead: Warmer prospects who have "
            "already requested a callback or proposal, and we've confirmed their "
            "intent.\n\n"
            "Every Lead Includes\n"
            "- U.S.-based businesses\n- Manually screened (not scraped)\n"
            "- Filtered by service need and urgency\n"
            "- Delivered in real time as they come in\n"
            "- 100% exclusive to your business\n\n"
            "Test Pack — $399: A great way to start without overcommitting.\n"
            "Includes:\n- 10 Appointment-Ready Leads\n- 12 High-Intent Exclusive "
            "Leads\n- 100% Exclusive\n- Free replacements if anything is off\n"
            "- Delivery over approximately 10–15 days\n\n"
            "What You'll Receive — every lead includes:\n"
            "- Contact Name\n- Company Name\n- Phone Number\n- Email Address\n"
            "- Website\n- Services they're looking for\n"
            "- Context (e.g. \"Requested proposal\", \"Needs callback ASAP\")\n\n"
            "Industries We Currently Serve\n"
            "- Home Services\n- Real Estate\n- Legal\n- Health & Wellness\n- Trades\n"
            "- Events\n"
            "We can also dial into specific niches based on your requirements.\n\n"
            "Scaling\n"
            "Once we identify what converts best for you, scaling to 40+ exclusive "
            "leads per week is straightforward.\n\n"
            "Recent Improvements\n"
            "We've waived the setup fee.\n"
            "We're also leveraging a newly expanded data source, allowing us to "
            "consistently deliver higher-quality and more reliable leads.\n\n"
            "If everything looks good, I can have the invoice sent over and prepare "
            "your first delivery.\n\n"
            "Looking forward to working with you!\n\n"
            "Regards,\nAmanda"
        ),
    },
]

_DEFAULT_SEQUENCE = [
    {"step_number": 1, "delay_days": 0, "template_name": "followup1.html"},
    {"step_number": 2, "delay_days": 2, "template_name": "followup2.html"},
    {"step_number": 3, "delay_days": 2, "template_name": "followup3.html"},
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


def _run_lightweight_migrations() -> None:
    """Apply additive schema tweaks that create_all can't (new columns on an
    existing SQLite table). Safe to run repeatedly."""
    from sqlalchemy import text

    assert _engine is not None
    with _engine.begin() as conn:
        cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(leads)")).fetchall()
        }
        if "campaign" not in cols:
            conn.execute(
                text("ALTER TABLE leads ADD COLUMN campaign VARCHAR(32) "
                     "DEFAULT 'followup'")
            )


def init_db(cfg: Config = config) -> None:
    """Create tables, wire logging sink and seed defaults. Safe to call twice."""
    init_engine(cfg)
    logging_manager.configure_logging(cfg.logs_dir)
    logging_manager.set_db_sink(_db_log_sink)
    assert _engine is not None
    Base.metadata.create_all(_engine)
    _run_lightweight_migrations()
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
