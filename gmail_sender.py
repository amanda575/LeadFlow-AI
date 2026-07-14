"""Gmail-API sender for LeadFlow AI (HTTPS transport).

An alternative to :mod:`smtp_client` that sends follow-ups through the Gmail API
over HTTPS instead of SMTP. This is required on hosts that block outbound SMTP
(Railway and most PaaS providers) and has the bonus of native Gmail threading
(via ``threadId``) and sending as the authenticated user with no app password.

It exposes the SAME interface as :class:`smtp_client.SMTPClient`
(``send(OutgoingEmail) -> SendResult`` and ``verify_connection() -> bool``), so
callers can use either interchangeably via :mod:`sender`.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import List

from config import Config, config
from gmail_client import gmail_client
from logging_manager import get_logger
from smtp_client import OutgoingEmail, SendResult

log = get_logger("smtp")  # same category so logs/health read consistently


class GmailSender:
    """Sends mail via the Gmail API using the existing OAuth credentials."""

    def __init__(self, cfg: Config = config) -> None:
        self._cfg = cfg

    def verify_connection(self) -> bool:
        """Healthy when the Gmail API is authorized (token present & refreshable)."""
        return gmail_client.available

    # -- message building -------------------------------------------------- #

    def _build_mime(self, email: OutgoingEmail) -> EmailMessage:
        msg = EmailMessage()
        # Gmail sets From to the authenticated account, but we set it explicitly
        # so the display name is right.
        msg["From"] = formataddr((self._cfg.smtp.from_name, self._cfg.smtp.from_email))
        msg["To"] = formataddr((email.to_name or "", email.to_email))
        msg["Subject"] = email.subject
        domain = (self._cfg.smtp.from_email.split("@", 1)[-1]) or "leadflow.local"
        msg["Message-ID"] = make_msgid(domain=domain)

        if email.in_reply_to:
            msg["In-Reply-To"] = email.in_reply_to
            refs: List[str] = []
            if email.references:
                refs.extend(email.references.split())
            if email.in_reply_to not in refs:
                refs.append(email.in_reply_to)
            msg["References"] = " ".join(refs)

        msg.set_content(email.text_body or "")
        if email.html_body:
            msg.add_alternative(email.html_body, subtype="html")
        return msg

    # -- sending ----------------------------------------------------------- #

    def send(self, email: OutgoingEmail) -> SendResult:
        service = gmail_client._build_service()  # reuse the authorized client
        if service is None:
            return SendResult(False, error="Gmail API not available/authorized")
        try:
            mime = self._build_mime(email)
            raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
            body = {"raw": raw}
            if email.thread_id:
                # Placing the message in the existing thread keeps the follow-up
                # inside the original Gmail conversation.
                body["threadId"] = email.thread_id
            sent = (
                service.users().messages().send(userId="me", body=body).execute()
            )
            log.info(
                "Sent via Gmail API to %s (message id=%s, thread=%s)",
                email.to_email, sent.get("id"), sent.get("threadId"),
            )
            return SendResult(True, message_id=mime["Message-ID"], attempts=1)
        except Exception as exc:  # noqa: BLE001 - report any API failure upward
            log.warning("Gmail API send to %s failed: %s", email.to_email, exc)
            return SendResult(False, error=str(exc), attempts=1)


# Module-level singleton.
gmail_sender = GmailSender()
