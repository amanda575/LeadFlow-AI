"""Shared utility helpers for LeadFlow AI.

Pure, side-effect-free helpers used across the codebase: email header parsing,
business-hours arithmetic, HTML-to-text conversion and lightweight contact
field extraction. Keeping these here enforces DRY and keeps the heavier modules
focused on orchestration.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Optional, Tuple

import pytz

from config import BusinessHoursConfig

# --------------------------------------------------------------------------- #
# Email / contact parsing
# --------------------------------------------------------------------------- #

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
# Common free / generic mailbox providers we don't treat as company domains.
_FREE_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "live.com", "aol.com", "icloud.com", "me.com", "proton.me", "protonmail.com",
    "msn.com", "yandex.com", "gmx.com", "mail.com", "zoho.com",
}


def parse_from_header(from_header: str) -> Tuple[str, str]:
    """Split a ``From:`` header into ``(display_name, email)``.

    Falls back to deriving a name from the local-part of the address when the
    header has no display name.
    """
    name, email = parseaddr(from_header or "")
    email = (email or "").strip().lower()
    name = (name or "").strip().strip('"')
    if not name and email:
        local = email.split("@", 1)[0]
        name = re.sub(r"[._\-]+", " ", local).title()
    return name, email


def extract_email(text: str) -> Optional[str]:
    """Return the first email address found in *text*, if any."""
    match = _EMAIL_RE.search(text or "")
    return match.group(0).lower() if match else None


def extract_website(text: str, sender_email: str = "") -> Optional[str]:
    """Best-effort extraction of a company website.

    Prefers an explicit URL in the body; otherwise derives one from a
    non-free-provider sender domain.
    """
    match = _URL_RE.search(text or "")
    if match:
        return match.group(0).rstrip(".,);")
    domain = domain_of(sender_email)
    if domain and domain not in _FREE_DOMAINS:
        return f"https://{domain}"
    return None


def domain_of(email: str) -> Optional[str]:
    if email and "@" in email:
        return email.split("@", 1)[1].strip().lower()
    return None


def guess_company(sender_email: str, display_name: str = "") -> Optional[str]:
    """Derive a human-friendly company name from the sender's domain."""
    domain = domain_of(sender_email)
    if not domain or domain in _FREE_DOMAINS:
        return None
    # Strip the public suffix (.com, .co.uk, …) and title-case the core label.
    core = domain.split(".")[0]
    return core.replace("-", " ").title() or None


def first_name(full_name: str) -> str:
    """Return just the first token of a name, defaulting to "there"."""
    full_name = (full_name or "").strip()
    if not full_name:
        return "there"
    return full_name.split()[0]


# --------------------------------------------------------------------------- #
# HTML -> text
# --------------------------------------------------------------------------- #

class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"br", "p", "div", "tr", "li"}:
            self._chunks.append("\n")

    def text(self) -> str:
        joined = "".join(self._chunks)
        # Collapse excessive whitespace while preserving paragraph breaks.
        lines = [line.strip() for line in joined.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    """Convert an HTML fragment into readable plain text."""
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        # On malformed HTML, strip tags crudely as a fallback.
        return re.sub(r"<[^>]+>", "", html)
    return parser.text()


def truncate(text: str, length: int = 280) -> str:
    text = (text or "").strip()
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Business-hours arithmetic
# --------------------------------------------------------------------------- #

def get_timezone(name: str) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(name)
    except Exception:
        return pytz.utc


def now_in_tz(tz_name: str) -> datetime:
    return datetime.now(get_timezone(tz_name))


def is_within_business_hours(
    moment: datetime, hours: BusinessHoursConfig
) -> bool:
    """Return True if *moment* (tz-aware) falls inside the sending window."""
    tz = get_timezone(hours.timezone)
    local = moment.astimezone(tz)
    if hours.weekdays_only and local.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    return hours.start_hour <= local.hour < hours.end_hour


def next_business_window(
    moment: datetime, hours: BusinessHoursConfig
) -> datetime:
    """Return the earliest valid send time at or after *moment*.

    If *moment* is already inside the window it is returned unchanged (converted
    to the business timezone). Otherwise it is advanced to the next window open.
    """
    tz = get_timezone(hours.timezone)
    local = moment.astimezone(tz)

    # Guard against a misconfigured 0-length window to avoid infinite loops.
    if hours.start_hour >= hours.end_hour:
        return local

    for _ in range(14):  # at most two weeks of look-ahead
        if is_within_business_hours(local, hours):
            return local
        if local.hour < hours.start_hour:
            local = local.replace(
                hour=hours.start_hour, minute=0, second=0, microsecond=0
            )
        else:
            # Past the window today (or weekend) -> jump to next day's open.
            local = (local + timedelta(days=1)).replace(
                hour=hours.start_hour, minute=0, second=0, microsecond=0
            )
    return local


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def is_valid_email(email: str) -> bool:
    return bool(email) and bool(_EMAIL_RE.fullmatch(email.strip()))


def safe_filename(name: str) -> str:
    """Sanitise a string for use as a filename component."""
    return re.sub(r"[^A-Za-z0-9._\-]+", "_", name).strip("_") or "file"
