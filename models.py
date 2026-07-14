"""SQLAlchemy ORM models for LeadFlow AI.

Eight tables back the application:

* :class:`Lead`            - imported Gmail threads being followed up.
* :class:`Template`        - reusable Jinja2 email templates.
* :class:`FollowUpSequence`- ordered, data-driven follow-up steps.
* :class:`Log`             - structured application logs (mirrors files).
* :class:`Setting`         - mutable key/value settings editable in the UI.
* :class:`Notification`    - desktop/console notification history.
* :class:`ActivityHistory` - per-lead audit trail of every action taken.
* (indexes are declared inline on the hot columns).

All timestamps are stored in UTC.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Timezone-naive UTC timestamp (SQLite stores naive datetimes)."""
    return datetime.utcnow()


class Base(DeclarativeBase):
    """Declarative base for all models."""


class LeadStatus(str, enum.Enum):
    """Lifecycle states of a lead."""

    PENDING = "pending"        # imported, first follow-up not yet due
    WAITING = "waiting"        # follow-up sent, waiting out the delay
    SENDING = "sending"        # a send is due / in progress today
    REPLIED = "replied"        # reply detected -> all follow-ups cancelled
    COMPLETED = "completed"    # exhausted the sequence with no reply
    PAUSED = "paused"          # manually paused by the operator
    FAILED = "failed"          # last send failed past retry limit


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Gmail identity / threading.
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    message_id: Mapped[Optional[str]] = mapped_column(String(255))
    rfc_message_id: Mapped[Optional[str]] = mapped_column(String(512))
    references: Mapped[Optional[str]] = mapped_column(Text)

    # Contact details.
    subject: Mapped[Optional[str]] = mapped_column(String(512))
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    company: Mapped[Optional[str]] = mapped_column(String(255))
    website: Mapped[Optional[str]] = mapped_column(String(512))

    # Scheduling / lifecycle.
    date_added: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    current_stage: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[LeadStatus] = mapped_column(
        Enum(LeadStatus), default=LeadStatus.PENDING, index=True
    )
    next_followup_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    last_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Retry bookkeeping.
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)

    # Reply tracking.
    replied: Mapped[bool] = mapped_column(Boolean, default=False)
    reply_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    reply_from: Mapped[Optional[str]] = mapped_column(String(255))
    reply_body: Mapped[Optional[str]] = mapped_column(Text)
    reply_sentiment: Mapped[Optional[str]] = mapped_column(String(32))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    activities: Mapped[list["ActivityHistory"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("thread_id", name="uq_leads_thread_id"),
        Index("ix_leads_email", "email"),
        Index("ix_leads_status_next", "status", "next_followup_at"),
    )

    @property
    def gmail_url(self) -> str:
        return f"https://mail.google.com/mail/u/0/#all/{self.thread_id}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "subject": self.subject,
            "email": self.email,
            "name": self.name,
            "company": self.company,
            "website": self.website,
            "status": self.status.value if self.status else None,
            "current_stage": self.current_stage,
            "date_added": self.date_added.isoformat() if self.date_added else None,
            "next_followup_at": (
                self.next_followup_at.isoformat() if self.next_followup_at else None
            ),
            "last_sent_at": (
                self.last_sent_at.isoformat() if self.last_sent_at else None
            ),
            "replied": self.replied,
            "reply_at": self.reply_at.isoformat() if self.reply_at else None,
            "reply_from": self.reply_from,
            "reply_sentiment": self.reply_sentiment,
            "retry_count": self.retry_count,
            "gmail_url": self.gmail_url,
        }


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(512), default="")
    html_body: Mapped[str] = mapped_column(Text, default="")
    text_body: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[Optional[str]] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class FollowUpSequence(Base):
    __tablename__ = "followup_sequence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    step_number: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    delay_days: Mapped[int] = mapped_column(Integer, default=2)
    template_name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "step_number": self.step_number,
            "delay_days": self.delay_days,
            "template_name": self.template_name,
            "enabled": self.enabled,
        }


class Log(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    category: Mapped[str] = mapped_column(String(32), default="general", index=True)
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    message: Mapped[str] = mapped_column(Text, default="")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(32), default="info")
    read: Mapped[bool] = mapped_column(Boolean, default=False)


class ActivityHistory(Base):
    __tablename__ = "activity_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    action: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[Optional[str]] = mapped_column(Text)

    lead: Mapped["Lead"] = relationship(back_populates="activities")
