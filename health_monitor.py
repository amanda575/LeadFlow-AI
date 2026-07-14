"""System health monitoring for LeadFlow AI.

Aggregates the live status of every subsystem into a single dataclass the
dashboard "Health" page renders: database connectivity, scheduler state, SMTP
reachability, Gmail availability, disk usage, log size and the timestamps of the
last scan/send/reply. Heavyweight checks (SMTP/Gmail network round-trips) are
cached briefly so the health endpoint stays cheap to poll.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from sqlalchemy import func, text

from config import config
from database import get_session
from gmail_client import gmail_client
from logging_manager import get_logger
from models import ActivityHistory, Lead, Log
from smtp_client import smtp_client

log = get_logger("scheduler")


@dataclass
class HealthReport:
    database_ok: bool = False
    scheduler_running: bool = False
    smtp_ok: bool = False
    gmail_ok: bool = False
    disk_total_gb: float = 0.0
    disk_used_gb: float = 0.0
    disk_free_gb: float = 0.0
    disk_percent: float = 0.0
    log_size_mb: float = 0.0
    log_rows: int = 0
    last_scan: Optional[datetime] = None
    last_send: Optional[datetime] = None
    last_reply: Optional[datetime] = None
    generated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, object]:
        def iso(dt: Optional[datetime]) -> Optional[str]:
            return dt.isoformat() if dt else None

        return {
            "database_ok": self.database_ok,
            "scheduler_running": self.scheduler_running,
            "smtp_ok": self.smtp_ok,
            "gmail_ok": self.gmail_ok,
            "disk_total_gb": round(self.disk_total_gb, 2),
            "disk_used_gb": round(self.disk_used_gb, 2),
            "disk_free_gb": round(self.disk_free_gb, 2),
            "disk_percent": round(self.disk_percent, 1),
            "log_size_mb": round(self.log_size_mb, 3),
            "log_rows": self.log_rows,
            "last_scan": iso(self.last_scan),
            "last_send": iso(self.last_send),
            "last_reply": iso(self.last_reply),
            "generated_at": iso(self.generated_at),
        }


class HealthMonitor:
    """Builds :class:`HealthReport`s, caching expensive network checks."""

    def __init__(self, scheduler_ref=None, cache_seconds: int = 60) -> None:
        self._scheduler = scheduler_ref
        self._cache_seconds = cache_seconds
        self._smtp_cache: tuple[float, bool] = (0.0, False)
        self._gmail_cache: tuple[float, bool] = (0.0, False)
        self._last_scan: Optional[datetime] = None

    def bind_scheduler(self, scheduler_ref) -> None:
        self._scheduler = scheduler_ref

    def mark_scan(self) -> None:
        self._last_scan = datetime.utcnow()

    # -- individual checks ------------------------------------------------- #

    def _check_database(self) -> bool:
        session = get_session()
        try:
            session.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            log.error("Database health check failed: %s", exc)
            return False
        finally:
            session.close()

    def _check_smtp(self) -> bool:
        # "SMTP" on the dashboard means "can we send" — verify whichever sender
        # (SMTP or Gmail API) is active.
        now = time.time()
        ts, value = self._smtp_cache
        if now - ts < self._cache_seconds:
            return value
        from sender import get_sender

        value = get_sender().verify_connection()
        self._smtp_cache = (now, value)
        return value

    def _check_gmail(self) -> bool:
        now = time.time()
        ts, value = self._gmail_cache
        if now - ts < self._cache_seconds:
            return value
        value = gmail_client.available
        self._gmail_cache = (now, value)
        return value

    def _disk(self) -> tuple[float, float, float, float]:
        total, used, free = shutil.disk_usage(str(config.base_dir))
        gb = 1024 ** 3
        percent = (used / total * 100) if total else 0.0
        return total / gb, used / gb, free / gb, percent

    def _log_stats(self) -> tuple[float, int]:
        size_mb = 0.0
        try:
            for f in config.logs_dir.glob("leadflow.log*"):
                size_mb += f.stat().st_size
            size_mb /= 1024 ** 2
        except Exception:
            pass
        rows = 0
        session = get_session()
        try:
            rows = session.query(func.count(Log.id)).scalar() or 0
        except Exception:
            pass
        finally:
            session.close()
        return size_mb, rows

    def _timestamps(self) -> tuple[Optional[datetime], Optional[datetime]]:
        """Return (last_send, last_reply) from the data."""
        session = get_session()
        try:
            last_send = (
                session.query(func.max(Lead.last_sent_at)).scalar()
            )
            last_reply = session.query(func.max(Lead.reply_at)).scalar()
            return last_send, last_reply
        except Exception:
            return None, None
        finally:
            session.close()

    # -- public ------------------------------------------------------------ #

    def report(self) -> HealthReport:
        total, used, free, percent = self._disk()
        log_mb, log_rows = self._log_stats()
        last_send, last_reply = self._timestamps()
        scheduler_running = bool(self._scheduler and self._scheduler.running)
        return HealthReport(
            database_ok=self._check_database(),
            scheduler_running=scheduler_running,
            smtp_ok=self._check_smtp(),
            gmail_ok=self._check_gmail(),
            disk_total_gb=total,
            disk_used_gb=used,
            disk_free_gb=free,
            disk_percent=percent,
            log_size_mb=log_mb,
            log_rows=log_rows,
            last_scan=self._last_scan,
            last_send=last_send,
            last_reply=last_reply,
        )


# Module-level singleton (scheduler binds itself to it at startup).
health_monitor = HealthMonitor()
