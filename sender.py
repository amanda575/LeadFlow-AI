"""Outbound-mail sender selection for LeadFlow AI.

Returns the active sender based on ``SEND_METHOD``:
  * ``"smtp"``      → :mod:`smtp_client` (default; used locally)
  * ``"gmail_api"`` → :mod:`gmail_sender` (HTTPS; used where SMTP is blocked)

Both senders share the same interface (``send`` / ``verify_connection``), so the
rest of the app is agnostic to which one is in use.
"""

from __future__ import annotations

from config import config
from gmail_sender import gmail_sender
from smtp_client import smtp_client


def get_sender():
    """Return the sender object selected by configuration."""
    if config.send_method == "gmail_api":
        return gmail_sender
    return smtp_client


def active_method() -> str:
    return "gmail_api" if config.send_method == "gmail_api" else "smtp"
