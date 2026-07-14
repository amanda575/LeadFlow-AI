"""Desktop, sound and console notifications for LeadFlow AI.

The primary trigger is a newly detected reply, but the manager is generic. Each
channel is independently toggleable via settings and fails soft: a missing
``plyer`` backend or an unavailable sound device must never interrupt the
pipeline. Every notification is also persisted to the ``notifications`` table so
the dashboard can show history and unread badges.
"""

from __future__ import annotations

import platform
import subprocess
from typing import Optional

from database import get_setting_bool, session_scope
from logging_manager import get_logger
from models import Notification
from utils import truncate

log = get_logger("reply")


class NotificationManager:
    """Dispatches notifications across the configured channels."""

    def __init__(self) -> None:
        self._plyer = self._try_import_plyer()

    @staticmethod
    def _try_import_plyer():
        try:
            from plyer import notification  # type: ignore

            return notification
        except Exception:
            return None

    # -- public API -------------------------------------------------------- #

    def notify(
        self,
        title: str,
        body: str,
        kind: str = "info",
        play_sound: Optional[bool] = None,
    ) -> None:
        """Send a notification across all enabled channels and persist it."""
        self._persist(title, body, kind)

        if get_setting_bool("notify_console", True):
            self._console(title, body, kind)
        if get_setting_bool("notify_desktop", True):
            self._desktop(title, body)
        want_sound = get_setting_bool("notify_sound", True) if play_sound is None else play_sound
        if want_sound:
            self._sound()

    def notify_reply(self, lead) -> None:
        """Convenience formatter for the headline NEW REPLY event."""
        title = "📨 NEW REPLY RECEIVED"
        body = (
            f"Lead: {lead.name or 'Unknown'}\n"
            f"Email: {lead.email}\n"
            f"Company: {lead.company or '—'}\n"
            f"Time: {lead.reply_at}\n"
            f"Preview: {truncate(lead.reply_body or '', 160)}"
        )
        self.notify(title, body, kind="reply", play_sound=True)

    # -- channels ---------------------------------------------------------- #

    def _console(self, title: str, body: str, kind: str) -> None:
        banner = "=" * 60
        log.info("%s\n%s\n%s\n%s", banner, title, body, banner)

    def _desktop(self, title: str, body: str) -> None:
        if self._plyer is None:
            return
        try:
            self._plyer.notify(
                title=title,
                message=truncate(body, 240),
                app_name="LeadFlow AI",
                timeout=10,
            )
        except Exception as exc:  # pragma: no cover - platform dependent
            log.debug("Desktop notification unavailable: %s", exc)

    def _sound(self) -> None:
        """Play a short system sound. Best-effort, platform-specific."""
        system = platform.system()
        try:
            if system == "Darwin":
                subprocess.Popen(
                    ["afplay", "/System/Library/Sounds/Glass.aiff"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif system == "Windows":  # pragma: no cover
                import winsound

                winsound.MessageBeep()
            else:  # Linux / other -> terminal bell as a last resort
                print("\a", end="", flush=True)
        except Exception as exc:  # pragma: no cover
            log.debug("Sound notification unavailable: %s", exc)

    # -- persistence ------------------------------------------------------- #

    @staticmethod
    def _persist(title: str, body: str, kind: str) -> None:
        try:
            with session_scope() as session:
                session.add(Notification(title=title, body=body, kind=kind))
        except Exception as exc:  # pragma: no cover
            log.debug("Could not persist notification: %s", exc)


# Module-level singleton.
notifier = NotificationManager()
