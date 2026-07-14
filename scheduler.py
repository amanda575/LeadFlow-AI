"""APScheduler-based job orchestration for LeadFlow AI.

Wires the recurring background jobs that drive the platform:

  * import_leads     - pull new labelled Gmail threads        (every N min)
  * detect_replies   - stop campaigns on any reply            (every N min)
  * send_due_emails  - send follow-ups that are due           (every N min)
  * retry_failed     - re-queue failed sends with budget left (every N min)
  * cleanup_logs     - prune old DB log rows                  (daily)
  * backup_database  - consistent nightly SQLite backup       (daily)
  * health_check     - refresh cached health + mark last scan (every N min)

Jobs are wrapped so an exception in one run is logged but never kills the job or
the scheduler. ``max_instances=1`` + ``coalesce=True`` prevent overlap/backlog.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from backup_manager import backup_manager
from config import config
from database import get_setting_int, session_scope
from health_monitor import health_monitor
from logging_manager import get_logger
from models import Log
from reply_detector import reply_detector
from services import lead_service

log = get_logger("scheduler")


def _safe(job_name: str, func) -> None:
    """Run *func*, logging any exception without propagating."""
    try:
        func()
    except Exception as exc:  # pragma: no cover - defensive guardrail
        log.error("Job '%s' raised: %s", job_name, exc)


class SchedulerService:
    """Owns the APScheduler instance and the job definitions."""

    def __init__(self) -> None:
        self._scheduler = BackgroundScheduler(
            timezone="UTC",
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 120,
            },
        )
        health_monitor.bind_scheduler(self)

    @property
    def running(self) -> bool:
        return self._scheduler.running

    @property
    def scheduler(self) -> BackgroundScheduler:
        return self._scheduler

    # -- jobs -------------------------------------------------------------- #

    def _job_import(self) -> None:
        _safe("import_leads", lead_service.import_leads)

    def _job_detect_replies(self) -> None:
        _safe("detect_replies", reply_detector.detect)

    def _job_send(self) -> None:
        _safe("send_due_emails", lead_service.send_due_emails)

    def _job_retry(self) -> None:
        _safe("retry_failed", lead_service.retry_failed)

    def _job_cleanup_logs(self) -> None:
        def _cleanup() -> None:
            cutoff = datetime.utcnow() - timedelta(days=config.log_retention_days)
            with session_scope() as session:
                deleted = (
                    session.query(Log).filter(Log.created_at < cutoff).delete()
                )
            if deleted:
                log.info("Cleaned up %d old log row(s)", deleted)

        _safe("cleanup_logs", _cleanup)

    def _job_backup(self) -> None:
        _safe("backup_database", backup_manager.create_backup)

    def _job_health(self) -> None:
        def _health() -> None:
            health_monitor.mark_scan()
            health_monitor.report()

        _safe("health_check", _health)

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        if self._scheduler.running:
            return

        s = self._scheduler
        # Intervals are read from settings (with config defaults) at wiring time.
        import_min = get_setting_int(
            "scheduler_import_minutes", config.scheduler.import_interval_minutes
        )
        reply_min = get_setting_int(
            "scheduler_reply_minutes", config.scheduler.reply_interval_minutes
        )
        send_min = get_setting_int(
            "scheduler_send_minutes", config.scheduler.send_interval_minutes
        )

        s.add_job(self._job_import, "interval", minutes=import_min, id="import_leads",
                  next_run_time=datetime.utcnow() + timedelta(seconds=15))
        s.add_job(self._job_detect_replies, "interval", minutes=reply_min,
                  id="detect_replies",
                  next_run_time=datetime.utcnow() + timedelta(seconds=25))
        s.add_job(self._job_send, "interval", minutes=send_min, id="send_due_emails",
                  next_run_time=datetime.utcnow() + timedelta(seconds=35))
        s.add_job(self._job_retry, "interval",
                  minutes=config.scheduler.retry_interval_minutes, id="retry_failed")
        s.add_job(self._job_health, "interval",
                  minutes=config.scheduler.health_interval_minutes, id="health_check")
        s.add_job(self._job_cleanup_logs, "cron", hour=2, minute=30, id="cleanup_logs")
        s.add_job(self._job_backup, "cron", hour=3, minute=0, id="backup_database")

        s.start()
        log.info(
            "Scheduler started (import=%dm reply=%dm send=%dm)",
            import_min, reply_min, send_min,
        )

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            log.info("Scheduler stopped")

    def jobs_info(self) -> list[dict]:
        info = []
        for job in self._scheduler.get_jobs():
            info.append(
                {
                    "id": job.id,
                    "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                }
            )
        return info


# Module-level singleton.
scheduler_service = SchedulerService()
