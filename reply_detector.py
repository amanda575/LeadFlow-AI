"""Reply detection for LeadFlow AI.

Sweeps every non-terminal lead, fetches its Gmail thread and checks for any
message from someone other than us that arrived after our last send. The moment
a reply is found the lead is marked REPLIED, all pending follow-ups are
cancelled, a notification is fired and the activity is logged. This is the
"stop everything" guarantee that makes the platform behave like Instantly /
Smartlead / Reply.io.
"""

from __future__ import annotations

from datetime import datetime
from typing import List

from database import session_scope
from gmail_client import gmail_client
from logging_manager import get_logger
from models import Lead, LeadStatus
from notifications import notifier
from services import lead_service

log = get_logger("reply")

# Statuses that can still receive follow-ups and therefore must be watched.
_WATCHED = (
    LeadStatus.PENDING,
    LeadStatus.WAITING,
    LeadStatus.SENDING,
    LeadStatus.FAILED,
    LeadStatus.PAUSED,
)


class ReplyDetector:
    """Polls Gmail threads for replies to active leads."""

    def detect(self) -> int:
        """Scan watched leads. Returns the number of new replies detected."""
        if not gmail_client.available:
            return 0

        watched = self._watched_lead_ids()
        new_replies = 0
        for lead_id in watched:
            try:
                if self._check_lead(lead_id):
                    new_replies += 1
            except Exception as exc:
                log.error("Reply check failed for lead %s: %s", lead_id, exc)

        if new_replies:
            log.info("Detected %d new repl(y/ies)", new_replies)
        return new_replies

    @staticmethod
    def _watched_lead_ids() -> List[int]:
        with session_scope() as session:
            return [
                row.id
                for row in session.query(Lead.id)
                .filter(Lead.replied.is_(False))
                .filter(Lead.status.in_(list(_WATCHED)))
                .all()
            ]

    def _check_lead(self, lead_id: int) -> bool:
        with session_scope() as session:
            lead = session.get(Lead, lead_id)
            if lead is None or lead.replied:
                return False
            thread_id = lead.thread_id
            # Only messages that arrive AFTER we start tracking count as replies.
            # Baseline = our last send, or (before any send) the import time — so
            # an inbound email that was already in the thread when we imported it
            # is not mistaken for a fresh reply.
            baseline = lead.last_sent_at or lead.date_added

        thread = gmail_client.get_thread(thread_id)
        if thread is None:
            return False

        ours = lead_service._our_addresses()  # reuse identity logic
        if not lead_service._thread_has_reply(thread, ours, after=baseline):
            return False

        with session_scope() as session:
            lead = session.get(Lead, lead_id)
            if lead is None or lead.replied:
                return False
            lead_service._apply_reply(session, lead, thread, ours)

        # Fire notification with a freshly loaded (committed) lead.
        with session_scope() as session:
            lead = session.get(Lead, lead_id)
            if lead is not None:
                notifier.notify_reply(lead)
        return True


# Module-level singleton.
reply_detector = ReplyDetector()
