"""Core business logic for LeadFlow AI.

:class:`LeadService` is the orchestration layer that ties Gmail (read), the
follow-up sequence, the template engine and SMTP (send) together. The scheduler
(:mod:`scheduler`) and the dashboard (:mod:`dashboard`) both call into it; it is
the single source of truth for *what happens to a lead and when*.

Responsibilities:
  * import_leads      - pull labelled Gmail threads into the DB as leads.
  * send_due_emails   - send any follow-ups whose time has come.
  * send_lead_now     - operator-triggered immediate send.
  * retry_failed      - re-queue failed sends that still have retries left.
  * (reply detection lives in :mod:`reply_detector`, which calls back here.)

All datetimes persisted are naive UTC; business-hours math converts through the
configured timezone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

import pytz

from ai_provider import PersonalizationContext, get_ai_provider
from config import BusinessHoursConfig, config
from database import (
    get_setting_bool,
    get_setting_int,
    session_scope,
)
from gmail_client import GmailThread, gmail_client
from logging_manager import get_logger
from models import (
    ActivityHistory,
    FollowUpSequence,
    Lead,
    LeadStatus,
)
from smtp_client import OutgoingEmail
from sender import get_sender
from template_engine import RenderContext, engine
from utils import (
    first_name,
    guess_company,
    is_valid_email,
    next_business_window,
    parse_from_header,
)

log = get_logger("import")
send_log = get_logger("smtp")


def _business_hours() -> BusinessHoursConfig:
    """Read the current (DB-editable) business-hours settings."""
    from database import get_setting

    return BusinessHoursConfig(
        start_hour=get_setting_int("business_start_hour", config.business_hours.start_hour),
        end_hour=get_setting_int("business_end_hour", config.business_hours.end_hour),
        timezone=get_setting("business_timezone", config.business_hours.timezone),
        weekdays_only=get_setting_bool(
            "business_weekdays_only", config.business_hours.weekdays_only
        ),
    )


def _to_utc_naive(aware: datetime) -> datetime:
    return aware.astimezone(pytz.utc).replace(tzinfo=None)


def _schedule_in_business_hours(base_utc_naive: datetime) -> datetime:
    """Clamp a target UTC time to the next valid business-hours window."""
    hours = _business_hours()
    aware = pytz.utc.localize(base_utc_naive)
    window = next_business_window(aware, hours)
    return _to_utc_naive(window)


class LeadService:
    """Stateless orchestration over the persistence + IO layers."""

    def __init__(self) -> None:
        self._ai = get_ai_provider()

    # ------------------------------------------------------------------ #
    # Import
    # ------------------------------------------------------------------ #

    def import_leads(self) -> int:
        """Import new labelled threads as leads. Returns count of new leads."""
        thread_ids = gmail_client.list_label_threads()
        if not thread_ids:
            return 0

        created = 0
        for thread_id in thread_ids:
            try:
                if self._lead_exists(thread_id):
                    continue
                thread = gmail_client.get_thread(thread_id)
                if thread is None or thread.first is None:
                    continue
                if self._create_lead_from_thread(thread):
                    created += 1
            except Exception as exc:  # never let one bad thread stop the batch
                log.error("Failed importing thread %s: %s", thread_id, exc)

        if created:
            log.info("Imported %d new lead(s)", created)
        return created

    @staticmethod
    def _lead_exists(thread_id: str) -> bool:
        with session_scope() as session:
            return (
                session.query(Lead.id).filter_by(thread_id=thread_id).first()
                is not None
            )

    def _our_addresses(self) -> set:
        return {
            a.lower()
            for a in (
                config.smtp.from_email,
                config.smtp.username,
            )
            if a
        }

    def _create_lead_from_thread(self, thread: GmailThread) -> bool:
        """Create a Lead from the outreach thread. Returns True if created."""
        first = thread.first
        last = thread.last
        ours = self._our_addresses()

        # The prospect is the counterparty on the thread — the participant who
        # isn't us. This works whether the labelled email is one we SENT (the
        # prospect is the recipient) or one we RECEIVED (the prospect is the
        # sender), so the operator can label conversations either way.
        prospect_name, prospect_email = self._find_prospect(thread, ours)
        if not is_valid_email(prospect_email):
            log.warning("Skipping thread %s: no external participant found", thread.id)
            return False

        company = guess_company(prospect_email, prospect_name)
        extracted = self._ai.extract_company_website(first.body_text or "")
        website = extracted.get("website")
        company = extracted.get("company") or company

        # Start the follow-up clock from import time (now), so labelling an older
        # email doesn't fire a follow-up immediately.
        base_dt = datetime.utcnow()
        step = self._first_enabled_step()
        next_at: Optional[datetime] = None
        if step is not None:
            target = base_dt + timedelta(days=step.delay_days)
            next_at = _schedule_in_business_hours(target)

        with session_scope() as session:
            lead = Lead(
                thread_id=thread.id,
                message_id=last.id if last else None,
                rfc_message_id=last.rfc_message_id if last else None,
                references=self._build_references(thread),
                subject=first.subject,
                email=prospect_email,
                name=prospect_name or first_name(prospect_email),
                company=company,
                website=website,
                current_stage=0,
                status=LeadStatus.PENDING,
                next_followup_at=next_at,
            )
            session.add(lead)
            session.flush()
            session.add(
                ActivityHistory(
                    lead_id=lead.id,
                    action="imported",
                    detail=f"Imported from Gmail label; next follow-up {next_at}",
                )
            )

        log.info("Created lead %s (%s)", prospect_email, thread.id)
        return True

    @staticmethod
    def _find_prospect(thread: GmailThread, ours: set):
        """Return ``(name, email)`` of the first thread participant who isn't us.

        Scans each message's recipient then sender, so a thread we started
        resolves to its recipient and a thread we received resolves to its
        sender. Returns ``("", "")`` if the only participants are our own
        addresses (e.g. a note to yourself), which the caller treats as a skip.
        """
        for msg in thread.messages:
            for header in (msg.to_header, msg.from_header):
                name, addr = parse_from_header(header)
                if is_valid_email(addr) and addr.lower() not in ours:
                    return name, addr
        return "", ""

    @staticmethod
    def _build_references(thread: GmailThread) -> Optional[str]:
        ids = [m.rfc_message_id for m in thread.messages if m.rfc_message_id]
        return " ".join(ids) if ids else None

    @staticmethod
    def _message_datetime(message) -> Optional[datetime]:
        if message and message.internal_date_ms:
            return datetime.utcfromtimestamp(message.internal_date_ms / 1000.0)
        return None

    @staticmethod
    def _first_enabled_step() -> Optional[FollowUpSequence]:
        with session_scope() as session:
            return (
                session.query(FollowUpSequence)
                .filter_by(enabled=True)
                .order_by(FollowUpSequence.step_number.asc())
                .first()
            )

    @staticmethod
    def _step_after(stage: int) -> Optional[FollowUpSequence]:
        """Return the next enabled step strictly greater than *stage*."""
        with session_scope() as session:
            return (
                session.query(FollowUpSequence)
                .filter(FollowUpSequence.enabled.is_(True))
                .filter(FollowUpSequence.step_number > stage)
                .order_by(FollowUpSequence.step_number.asc())
                .first()
            )

    # ------------------------------------------------------------------ #
    # Reply detection helpers (shared with reply_detector)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _thread_has_reply(thread: GmailThread, ours: set, after: Optional[datetime]) -> bool:
        for msg in thread.messages:
            _, sender = parse_from_header(msg.from_header)
            if sender and sender not in ours:
                if after is None:
                    return True
                ts = LeadService._message_datetime(msg)
                if ts is None or ts >= after:
                    return True
        return False

    def _apply_reply(self, session, lead: Lead, thread: GmailThread, ours: set) -> None:
        """Mark *lead* replied inside an open session and cancel follow-ups."""
        reply_msg = None
        for msg in reversed(thread.messages):
            _, sender = parse_from_header(msg.from_header)
            if sender and sender not in ours:
                reply_msg = msg
                break
        lead.replied = True
        lead.status = LeadStatus.REPLIED
        lead.next_followup_at = None
        lead.reply_at = self._message_datetime(reply_msg) or datetime.utcnow()
        if reply_msg:
            _, reply_from = parse_from_header(reply_msg.from_header)
            lead.reply_from = reply_from
            lead.reply_body = reply_msg.body_text or reply_msg.snippet
            lead.reply_sentiment = self._ai.analyze_sentiment(lead.reply_body or "")
        session.add(
            ActivityHistory(
                lead_id=lead.id,
                action="replied",
                detail=f"Reply detected from {lead.reply_from}; follow-ups cancelled",
            )
        )

    # ------------------------------------------------------------------ #
    # Sending
    # ------------------------------------------------------------------ #

    def send_due_emails(self) -> int:
        """Send all follow-ups that are due and within business hours."""
        hours = _business_hours()
        now = datetime.utcnow()
        aware_now = pytz.utc.localize(now)
        from utils import is_within_business_hours

        if not is_within_business_hours(aware_now, hours):
            # Outside the window — defer everything to the next open.
            return 0

        with session_scope() as session:
            due_ids = [
                row.id
                for row in session.query(Lead.id)
                .filter(Lead.replied.is_(False))
                .filter(
                    Lead.status.in_(
                        [LeadStatus.PENDING, LeadStatus.WAITING, LeadStatus.SENDING]
                    )
                )
                .filter(Lead.next_followup_at.isnot(None))
                .filter(Lead.next_followup_at <= now)
                .all()
            ]

        sent = 0
        for lead_id in due_ids:
            if self._process_lead_send(lead_id):
                sent += 1
        if sent:
            send_log.info("Sent %d due follow-up(s)", sent)
        return sent

    def send_lead_now(self, lead_id: int) -> bool:
        """Operator-triggered immediate send (ignores schedule, honours reply)."""
        return self._process_lead_send(lead_id, force=True)

    def _process_lead_send(self, lead_id: int, force: bool = False) -> bool:
        # Re-check reply state right before sending (defensive).
        self._refresh_single_reply(lead_id)

        with session_scope() as session:
            lead = session.get(Lead, lead_id)
            if lead is None or lead.replied or lead.status in (
                LeadStatus.REPLIED, LeadStatus.PAUSED, LeadStatus.COMPLETED
            ):
                return False

            # Hard schedule guard: never send before the follow-up is actually
            # due (unless an operator forces it via "Send now"). This defends
            # against premature sends from overlapping scheduler runs during a
            # redeploy, restarts, or any stale/duplicated queue entry — the exact
            # cause of multiple follow-ups going out minutes apart.
            if not force and (
                lead.next_followup_at is None
                or lead.next_followup_at > datetime.utcnow()
            ):
                return False

            step = self._step_after(lead.current_stage)
            if step is None:
                lead.status = LeadStatus.COMPLETED
                lead.next_followup_at = None
                session.add(
                    ActivityHistory(
                        lead_id=lead.id,
                        action="completed",
                        detail="No further enabled steps; sequence complete",
                    )
                )
                self._maybe_remove_label(lead.thread_id)
                return False

            # Snapshot the data we need outside the session for the IO call.
            outgoing = self._build_outgoing(lead, step)
            stage_being_sent = step.step_number

        # --- network IO happens OUTSIDE the DB transaction ---------------- #
        result = get_sender().send(outgoing)

        with session_scope() as session:
            lead = session.get(Lead, lead_id)
            if lead is None:
                return False
            if result.success:
                lead.current_stage = stage_being_sent
                lead.last_sent_at = datetime.utcnow()
                lead.retry_count = 0
                lead.last_error = None
                # Thread the next reply under the message we just sent.
                if result.message_id:
                    refs = (lead.references or "")
                    lead.references = (refs + " " + result.message_id).strip()
                    lead.rfc_message_id = result.message_id
                nxt = self._step_after(stage_being_sent)
                if nxt is None:
                    lead.status = LeadStatus.COMPLETED
                    lead.next_followup_at = None
                    self._maybe_remove_label(lead.thread_id)
                    detail = f"Sent step {stage_being_sent}; sequence complete"
                else:
                    base = datetime.utcnow() + timedelta(days=nxt.delay_days)
                    lead.next_followup_at = _schedule_in_business_hours(base)
                    lead.status = LeadStatus.WAITING
                    detail = (
                        f"Sent step {stage_being_sent}; next step {nxt.step_number} "
                        f"at {lead.next_followup_at}"
                    )
                session.add(
                    ActivityHistory(lead_id=lead.id, action="sent", detail=detail)
                )
                return True
            else:
                lead.retry_count += 1
                lead.last_error = result.error
                max_retries = get_setting_int("max_retry_count", config.smtp.max_retries)
                if lead.retry_count >= max_retries:
                    lead.status = LeadStatus.FAILED
                    detail = f"Send failed permanently after {lead.retry_count} tries: {result.error}"
                else:
                    # Back off and try again on the next retry sweep.
                    lead.status = LeadStatus.WAITING
                    base = datetime.utcnow() + timedelta(
                        minutes=config.scheduler.retry_interval_minutes
                    )
                    lead.next_followup_at = _schedule_in_business_hours(base)
                    detail = f"Send failed (retry {lead.retry_count}): {result.error}"
                session.add(
                    ActivityHistory(lead_id=lead.id, action="send_failed", detail=detail)
                )
                send_log.warning("Lead %s send failed: %s", lead_id, result.error)
                return False

    def _build_outgoing(self, lead: Lead, step: FollowUpSequence) -> OutgoingEmail:
        ctx = RenderContext(
            name=first_name(lead.name or ""),
            company=lead.company,
            website=lead.website,
            industry=None,
            city=None,
        )
        rendered = engine.render(step.template_name, ctx)
        html, text = self._ai.personalize_email(
            rendered.html,
            rendered.text,
            PersonalizationContext(
                name=ctx.name,
                company=lead.company,
                website=lead.website,
                previous_subject=lead.subject,
                step_number=step.step_number,
            ),
        )
        # Keep the original subject prefixed with Re: to stay in-thread visually.
        subject = rendered.subject or (f"Re: {lead.subject}" if lead.subject else "Following up")
        if lead.subject and not subject.lower().startswith("re:"):
            subject = f"Re: {lead.subject}"
        return OutgoingEmail(
            to_email=lead.email,
            to_name=lead.name,
            subject=subject,
            html_body=html,
            text_body=text,
            in_reply_to=lead.rfc_message_id,
            references=lead.references,
            thread_id=lead.thread_id,
        )

    # ------------------------------------------------------------------ #
    # Retry
    # ------------------------------------------------------------------ #

    def retry_failed(self) -> int:
        """Re-queue FAILED leads that still have retry budget."""
        max_retries = get_setting_int("max_retry_count", config.smtp.max_retries)
        requeued = 0
        with session_scope() as session:
            failed = (
                session.query(Lead)
                .filter(Lead.status == LeadStatus.FAILED)
                .filter(Lead.replied.is_(False))
                .filter(Lead.retry_count < max_retries)
                .all()
            )
            for lead in failed:
                lead.status = LeadStatus.WAITING
                lead.next_followup_at = datetime.utcnow()
                session.add(
                    ActivityHistory(
                        lead_id=lead.id, action="retry_queued", detail="Re-queued for retry"
                    )
                )
                requeued += 1
        return requeued

    def retry_lead(self, lead_id: int) -> bool:
        """Operator-triggered retry of a single failed lead."""
        with session_scope() as session:
            lead = session.get(Lead, lead_id)
            if lead is None or lead.replied:
                return False
            lead.status = LeadStatus.WAITING
            lead.retry_count = 0
            lead.next_followup_at = datetime.utcnow()
            session.add(
                ActivityHistory(lead_id=lead.id, action="retry", detail="Manual retry")
            )
        return True

    # ------------------------------------------------------------------ #
    # Pause / resume / delete
    # ------------------------------------------------------------------ #

    def pause_lead(self, lead_id: int) -> bool:
        with session_scope() as session:
            lead = session.get(Lead, lead_id)
            if lead is None or lead.replied:
                return False
            lead.status = LeadStatus.PAUSED
            session.add(
                ActivityHistory(lead_id=lead.id, action="paused", detail="Paused by operator")
            )
        return True

    def resume_lead(self, lead_id: int) -> bool:
        with session_scope() as session:
            lead = session.get(Lead, lead_id)
            if lead is None or lead.replied or lead.status != LeadStatus.PAUSED:
                return False
            lead.status = LeadStatus.WAITING if lead.current_stage else LeadStatus.PENDING
            if lead.next_followup_at is None:
                lead.next_followup_at = datetime.utcnow()
            session.add(
                ActivityHistory(lead_id=lead.id, action="resumed", detail="Resumed by operator")
            )
        return True

    def delete_lead(self, lead_id: int) -> bool:
        with session_scope() as session:
            lead = session.get(Lead, lead_id)
            if lead is None:
                return False
            session.delete(lead)
        return True

    # ------------------------------------------------------------------ #
    # Reply refresh (single)
    # ------------------------------------------------------------------ #

    def _refresh_single_reply(self, lead_id: int) -> None:
        with session_scope() as session:
            lead = session.get(Lead, lead_id)
            if lead is None or lead.replied:
                return
            thread_id = lead.thread_id
            # See ReplyDetector._check_lead: count only messages after we began
            # tracking, so a pre-existing inbound email isn't read as a reply.
            baseline = lead.last_sent_at or lead.date_added

        thread = gmail_client.get_thread(thread_id)
        if thread is None:
            return
        ours = self._our_addresses()
        if not self._thread_has_reply(thread, ours, after=baseline):
            return

        with session_scope() as session:
            lead = session.get(Lead, lead_id)
            if lead is None or lead.replied:
                return
            self._apply_reply(session, lead, thread, ours)
        self._notify_reply(lead_id)

    def _notify_reply(self, lead_id: int) -> None:
        from notifications import notifier

        with session_scope() as session:
            lead = session.get(Lead, lead_id)
            if lead is not None:
                notifier.notify_reply(lead)

    # ------------------------------------------------------------------ #
    # Label cleanup
    # ------------------------------------------------------------------ #

    @staticmethod
    def _maybe_remove_label(thread_id: str) -> None:
        if get_setting_bool("auto_remove_label", config.gmail.auto_remove_label):
            gmail_client.remove_label(thread_id)


# Module-level singleton.
lead_service = LeadService()
