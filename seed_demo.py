"""Populate the database with realistic demo data for a live walkthrough.

Run once before starting the dashboard to see leads spread across every
lifecycle stage (pending, waiting, replied, completed, paused, failed) plus
activity history, logs and a notification. This touches ONLY the local SQLite
database — it never sends email or calls Gmail.

    python seed_demo.py
"""

from __future__ import annotations

from datetime import datetime, timedelta

from database import init_db, session_scope
from models import ActivityHistory, Lead, LeadStatus, Log, Notification

_NOW = datetime.utcnow()


def _lead(session, **kw):
    lead = Lead(**kw)
    session.add(lead)
    session.flush()
    return lead


def seed() -> None:
    init_db()
    with session_scope() as s:
        # Wipe any previous demo rows so re-running is idempotent.
        s.query(ActivityHistory).delete()
        s.query(Lead).delete()
        s.query(Notification).delete()

        demo = [
            dict(thread_id="thr_1001", email="maria@brightcafe.com", name="Maria Gonzalez",
                 company="Bright Cafe", website="https://brightcafe.com",
                 subject="Quick idea for Bright Cafe's traffic",
                 status=LeadStatus.PENDING, current_stage=0,
                 date_added=_NOW - timedelta(hours=3),
                 next_followup_at=_NOW + timedelta(days=2)),
            dict(thread_id="thr_1002", email="dan@peaklogistics.io", name="Dan Whitman",
                 company="Peak Logistics", website="https://peaklogistics.io",
                 subject="Re: SEO for Peak Logistics",
                 status=LeadStatus.WAITING, current_stage=1,
                 date_added=_NOW - timedelta(days=2),
                 last_sent_at=_NOW - timedelta(hours=6),
                 next_followup_at=_NOW + timedelta(days=3)),
            dict(thread_id="thr_1003", email="sophie@nordicdesign.se", name="Sophie Lind",
                 company="Nordic Design", website="https://nordicdesign.se",
                 subject="Helping Nordic Design rank higher",
                 status=LeadStatus.SENDING, current_stage=1,
                 date_added=_NOW - timedelta(days=2, hours=2),
                 last_sent_at=_NOW - timedelta(days=2),
                 next_followup_at=_NOW - timedelta(minutes=5)),
            dict(thread_id="thr_1004", email="raj@summitdental.com", name="Raj Patel",
                 company="Summit Dental", website="https://summitdental.com",
                 subject="More patients from Google for Summit Dental",
                 status=LeadStatus.REPLIED, current_stage=2, replied=True,
                 date_added=_NOW - timedelta(days=5),
                 last_sent_at=_NOW - timedelta(days=1),
                 reply_at=_NOW - timedelta(hours=4),
                 reply_from="raj@summitdental.com",
                 reply_sentiment="positive",
                 reply_body="Hi Amanda — yes! This is timely. Can we chat Thursday at 2pm? "
                            "We've been wanting to improve our local rankings."),
            dict(thread_id="thr_1005", email="lena@urbanyoga.co", name="Lena Brooks",
                 company="Urban Yoga", website="https://urbanyoga.co",
                 subject="A few SEO wins for Urban Yoga",
                 status=LeadStatus.REPLIED, current_stage=1, replied=True,
                 date_added=_NOW - timedelta(days=4),
                 last_sent_at=_NOW - timedelta(days=2),
                 reply_at=_NOW - timedelta(hours=20),
                 reply_from="lena@urbanyoga.co",
                 reply_sentiment="negative",
                 reply_body="Please remove me from your list. Not interested, thanks."),
            dict(thread_id="thr_1006", email="tomas@greenbuild.com", name="Tomas Reyes",
                 company="GreenBuild", website="https://greenbuild.com",
                 subject="SEO follow-up for GreenBuild",
                 status=LeadStatus.COMPLETED, current_stage=2,
                 date_added=_NOW - timedelta(days=10),
                 last_sent_at=_NOW - timedelta(days=4),
                 next_followup_at=None),
            dict(thread_id="thr_1007", email="amy@coastalrealty.com", name="Amy Chen",
                 company="Coastal Realty", website="https://coastalrealty.com",
                 subject="Ranking Coastal Realty's listings",
                 status=LeadStatus.PAUSED, current_stage=1,
                 date_added=_NOW - timedelta(days=3),
                 last_sent_at=_NOW - timedelta(days=1),
                 next_followup_at=None),
            dict(thread_id="thr_1008", email="invalid@bademaildomain", name="Test Bounce",
                 company=None, website=None,
                 subject="Follow-up that failed to send",
                 status=LeadStatus.FAILED, current_stage=0, retry_count=2,
                 last_error="Recipient refused: (550, 'No such user')",
                 date_added=_NOW - timedelta(days=1),
                 next_followup_at=_NOW - timedelta(hours=1)),
        ]

        for d in demo:
            lead = _lead(s, **d)
            s.add(ActivityHistory(lead_id=lead.id, action="imported",
                                  detail="Imported from Gmail label 'Follow Up'",
                                  created_at=lead.date_added))
            if lead.last_sent_at:
                s.add(ActivityHistory(lead_id=lead.id, action="sent",
                                      detail=f"Sent follow-up step {lead.current_stage}",
                                      created_at=lead.last_sent_at))
            if lead.replied:
                s.add(ActivityHistory(lead_id=lead.id, action="replied",
                                      detail=f"Reply detected from {lead.reply_from}; "
                                             "all follow-ups cancelled",
                                      created_at=lead.reply_at))
            if lead.status == LeadStatus.FAILED:
                s.add(ActivityHistory(lead_id=lead.id, action="send_failed",
                                      detail=lead.last_error,
                                      created_at=_NOW - timedelta(hours=1)))

        # A couple of notifications + log lines for realism.
        s.add(Notification(title="📨 NEW REPLY RECEIVED",
                           body="Raj Patel (Summit Dental) replied — positive.",
                           kind="reply"))
        s.add(Notification(title="📨 NEW REPLY RECEIVED",
                           body="Lena Brooks (Urban Yoga) replied — negative.",
                           kind="reply"))
        s.add(Log(category="import", level="INFO", message="Imported 8 new lead(s)"))
        s.add(Log(category="smtp", level="INFO", message="Sent follow-up to dan@peaklogistics.io"))
        s.add(Log(category="reply", level="INFO", message="Detected 2 new replies; campaigns stopped"))
        s.add(Log(category="smtp", level="WARNING",
                  message="Send failed to invalid@bademaildomain (Recipient refused)"))
        s.add(Log(category="scheduler", level="INFO", message="Scheduler started (import=5m reply=2m send=5m)"))

    print("Seeded 8 demo leads across all lifecycle stages.")


if __name__ == "__main__":
    seed()
