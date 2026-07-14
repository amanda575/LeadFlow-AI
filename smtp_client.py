"""SMTP sending for LeadFlow AI — the ONLY outbound mail path.

Gmail API is never used to send. This client builds a multipart/alternative
message, preserves Gmail conversation threading via ``In-Reply-To`` and
``References`` headers, and retries transient failures up to the configured
limit with a delay between attempts.
"""

from __future__ import annotations

import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import List, Optional

from config import SMTPConfig, config
from database import get_setting_int
from logging_manager import get_logger

log = get_logger("smtp")


@dataclass
class OutgoingEmail:
    """A message ready to be sent."""

    to_email: str
    to_name: Optional[str]
    subject: str
    html_body: str
    text_body: str
    # Threading headers (the RFC Message-IDs, angle-bracketed).
    in_reply_to: Optional[str] = None
    references: Optional[str] = None
    # Gmail thread id — used by the Gmail-API sender to keep the follow-up in the
    # original conversation. Ignored by the SMTP sender (which threads via headers).
    thread_id: Optional[str] = None


@dataclass
class SendResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0


class SMTPClient:
    """Connection-per-send SMTP client with retry and threading support."""

    def __init__(self, smtp_config: SMTPConfig = config.smtp) -> None:
        self._cfg = smtp_config

    # -- connection -------------------------------------------------------- #

    def _connect(self) -> smtplib.SMTP:
        if self._cfg.use_ssl:
            context = ssl.create_default_context()
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                self._cfg.host, self._cfg.port, timeout=30, context=context
            )
        else:
            server = smtplib.SMTP(self._cfg.host, self._cfg.port, timeout=30)
            server.ehlo()
            if self._cfg.use_tls:
                context = ssl.create_default_context()
                server.starttls(context=context)
                server.ehlo()
        if self._cfg.username and self._cfg.password:
            server.login(self._cfg.username, self._cfg.password)
        return server

    def verify_connection(self) -> bool:
        """Open and immediately close a connection to validate credentials."""
        if not self._cfg.configured:
            return False
        try:
            server = self._connect()
            try:
                server.noop()
            finally:
                server.quit()
            return True
        except Exception as exc:
            log.warning("SMTP verify failed: %s", exc)
            return False

    # -- message building -------------------------------------------------- #

    def _build_message(self, email: OutgoingEmail) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = formataddr((self._cfg.from_name, self._cfg.from_email))
        msg["To"] = formataddr((email.to_name or "", email.to_email))
        msg["Subject"] = email.subject
        msg["Date"] = formatdate(localtime=True)
        # Generate our own Message-ID so the next step can thread under it.
        domain = (self._cfg.from_email.split("@", 1)[-1]) or "leadflow.local"
        msg["Message-ID"] = make_msgid(domain=domain)

        if email.in_reply_to:
            msg["In-Reply-To"] = email.in_reply_to
            refs: List[str] = []
            if email.references:
                refs.extend(email.references.split())
            if email.in_reply_to not in refs:
                refs.append(email.in_reply_to)
            msg["References"] = " ".join(refs)

        # multipart/alternative: text first, HTML second (clients prefer last).
        msg.set_content(email.text_body or "")
        if email.html_body:
            msg.add_alternative(email.html_body, subtype="html")
        return msg

    # -- sending ----------------------------------------------------------- #

    def send(self, email: OutgoingEmail) -> SendResult:
        """Send *email*, retrying transient errors. Returns a :class:`SendResult`."""
        if not self._cfg.configured:
            return SendResult(False, error="SMTP not configured")

        max_retries = max(1, get_setting_int("max_retry_count", self._cfg.max_retries))
        delay = self._cfg.retry_delay_seconds
        last_error: Optional[str] = None

        for attempt in range(1, max_retries + 1):
            try:
                message = self._build_message(email)
                server = self._connect()
                try:
                    server.send_message(message)
                finally:
                    server.quit()
                msg_id = message["Message-ID"]
                log.info(
                    "Sent to %s (attempt %d) message-id=%s",
                    email.to_email, attempt, msg_id,
                )
                return SendResult(True, message_id=msg_id, attempts=attempt)
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
                    ssl.SSLError, OSError) as exc:
                # Transient/network errors -> retry.
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "Transient SMTP error to %s (attempt %d/%d): %s",
                    email.to_email, attempt, max_retries, last_error,
                )
            except smtplib.SMTPRecipientsRefused as exc:
                # Permanent: bad recipient. Do not retry.
                last_error = f"Recipient refused: {exc}"
                log.error(last_error)
                return SendResult(False, error=last_error, attempts=attempt)
            except smtplib.SMTPException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "SMTP error to %s (attempt %d/%d): %s",
                    email.to_email, attempt, max_retries, last_error,
                )
            except Exception as exc:  # pragma: no cover - defensive
                last_error = f"Unexpected: {exc}"
                log.error(last_error)

            if attempt < max_retries:
                time.sleep(delay)

        return SendResult(False, error=last_error, attempts=max_retries)


# Module-level singleton.
smtp_client = SMTPClient()
