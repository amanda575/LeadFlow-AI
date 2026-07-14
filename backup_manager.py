"""Nightly SQLite backups for LeadFlow AI.

Uses SQLite's online backup API (via ``sqlite3``) so backups are consistent even
while the app is running, then prunes to the most recent ``BACKUP_RETENTION``
files. Backups are written to ``backups/`` as ``leadflow-YYYYmmdd-HHMMSS.db``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from config import config
from logging_manager import get_logger

log = get_logger("database")


class BackupManager:
    def __init__(self, db_path: Path = None, backups_dir: Path = None, retention: int = None) -> None:
        self._db_path = db_path or config.database_path
        self._backups_dir = backups_dir or config.backups_dir
        self._retention = retention if retention is not None else config.backup_retention
        self._backups_dir.mkdir(parents=True, exist_ok=True)

    def create_backup(self) -> Optional[Path]:
        """Create a timestamped consistent backup. Returns its path."""
        if not self._db_path.exists():
            log.warning("Backup skipped: database file does not exist yet")
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self._backups_dir / f"leadflow-{stamp}.db"
        try:
            source = sqlite3.connect(str(self._db_path))
            try:
                dest = sqlite3.connect(str(target))
                try:
                    source.backup(dest)
                finally:
                    dest.close()
            finally:
                source.close()
            log.info("Database backup created: %s", target.name)
            self.prune()
            return target
        except Exception as exc:
            log.error("Backup failed: %s", exc)
            return None

    def list_backups(self) -> List[Path]:
        return sorted(
            self._backups_dir.glob("leadflow-*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def prune(self) -> int:
        """Delete backups beyond the retention limit. Returns count removed."""
        backups = self.list_backups()
        removed = 0
        for old in backups[self._retention:]:
            try:
                old.unlink()
                removed += 1
            except Exception as exc:
                log.warning("Could not delete old backup %s: %s", old, exc)
        if removed:
            log.info("Pruned %d old backup(s)", removed)
        return removed


# Module-level singleton.
backup_manager = BackupManager()
