"""Central configuration for LeadFlow AI.

All runtime configuration is loaded from environment variables (via a ``.env``
file when present) into immutable, typed dataclasses. Nothing in this module
talks to a database — these are the *bootstrap* defaults. Mutable, user-editable
settings (business hours, notification toggles, …) are mirrored into the
``Settings`` database table at first run so they can be changed from the
dashboard without editing files. See :mod:`models` and :mod:`dashboard`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Resolve project paths relative to this file so the app works regardless of the
# current working directory it is launched from.
BASE_DIR: Path = Path(__file__).resolve().parent

# Load .env early. Real environment variables always win over the file.
load_dotenv(BASE_DIR / ".env")


def _get_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean-ish environment variable."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _get_int(name: str, default: int) -> int:
    """Parse an integer environment variable, falling back on errors."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value is not None else default


@dataclass(frozen=True)
class SMTPConfig:
    """Outbound mail configuration. SENDING happens here and ONLY here."""

    host: str
    port: int
    username: str
    password: str
    use_tls: bool
    use_ssl: bool
    from_name: str
    from_email: str
    max_retries: int
    retry_delay_seconds: int

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username and self.password)


@dataclass(frozen=True)
class GmailConfig:
    """Inbound Gmail API configuration. READING ONLY — never sends mail."""

    credentials_file: Path
    token_file: Path
    label: str
    auto_remove_label: bool
    # Read-only scope keeps the principle of least privilege; sending uses SMTP.
    scopes: List[str] = field(
        default_factory=lambda: ["https://www.googleapis.com/auth/gmail.modify"]
    )

    @property
    def configured(self) -> bool:
        return self.credentials_file.exists() or self.token_file.exists()


@dataclass(frozen=True)
class BusinessHoursConfig:
    start_hour: int
    end_hour: int
    timezone: str
    weekdays_only: bool


@dataclass(frozen=True)
class SchedulerConfig:
    import_interval_minutes: int
    reply_interval_minutes: int
    send_interval_minutes: int
    retry_interval_minutes: int
    health_interval_minutes: int


@dataclass(frozen=True)
class NotificationConfig:
    desktop: bool
    sound: bool
    console: bool


@dataclass(frozen=True)
class AIConfig:
    """Optional AI provider. Empty ``provider`` disables all AI features."""

    provider: str
    api_key: str
    model: str

    @property
    def enabled(self) -> bool:
        return bool(self.provider)


@dataclass(frozen=True)
class Config:
    """Top-level immutable application configuration."""

    base_dir: Path
    secret_key: str
    flask_host: str
    flask_port: int
    flask_debug: bool

    dashboard_username: str
    dashboard_password: str
    dashboard_password_hash: str
    session_timeout_minutes: int
    # Only mark the session cookie "Secure" (HTTPS-only) when actually serving
    # over HTTPS. Default False so local http://localhost logins work.
    session_cookie_secure: bool

    smtp: SMTPConfig
    gmail: GmailConfig
    business_hours: BusinessHoursConfig
    scheduler: SchedulerConfig
    notifications: NotificationConfig
    ai: AIConfig

    backup_retention: int
    log_retention_days: int

    # Derived directory paths.
    database_dir: Path
    logs_dir: Path
    backups_dir: Path
    templates_dir: Path
    email_templates_dir: Path

    @property
    def database_path(self) -> Path:
        return self.database_dir / "leadflow.db"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path}"


def load_config() -> Config:
    """Build the :class:`Config` object from the current environment."""

    database_dir = BASE_DIR / "database"
    logs_dir = BASE_DIR / "logs"
    backups_dir = BASE_DIR / "backups"
    templates_dir = BASE_DIR / "templates"
    email_templates_dir = templates_dir / "email"

    for directory in (database_dir, logs_dir, backups_dir, email_templates_dir):
        directory.mkdir(parents=True, exist_ok=True)

    smtp = SMTPConfig(
        host=_get_str("SMTP_HOST", "smtp.gmail.com"),
        port=_get_int("SMTP_PORT", 587),
        username=_get_str("SMTP_USERNAME"),
        password=_get_str("SMTP_PASSWORD"),
        use_tls=_get_bool("SMTP_USE_TLS", True),
        use_ssl=_get_bool("SMTP_USE_SSL", False),
        from_name=_get_str("SMTP_FROM_NAME", "LeadFlow AI"),
        from_email=_get_str("SMTP_FROM_EMAIL") or _get_str("SMTP_USERNAME"),
        max_retries=_get_int("SMTP_MAX_RETRIES", 3),
        retry_delay_seconds=_get_int("SMTP_RETRY_DELAY_SECONDS", 30),
    )

    gmail = GmailConfig(
        credentials_file=BASE_DIR
        / _get_str("GMAIL_CREDENTIALS_FILE", "credentials/credentials.json"),
        token_file=BASE_DIR / _get_str("GMAIL_TOKEN_FILE", "credentials/token.json"),
        label=_get_str("GMAIL_LABEL", "Follow Up"),
        auto_remove_label=_get_bool("GMAIL_AUTO_REMOVE_LABEL", False),
    )

    business_hours = BusinessHoursConfig(
        start_hour=_get_int("BUSINESS_START_HOUR", 9),
        end_hour=_get_int("BUSINESS_END_HOUR", 17),
        timezone=_get_str("BUSINESS_TIMEZONE", "America/New_York"),
        weekdays_only=_get_bool("BUSINESS_WEEKDAYS_ONLY", True),
    )

    scheduler = SchedulerConfig(
        import_interval_minutes=_get_int("IMPORT_INTERVAL_MINUTES", 5),
        reply_interval_minutes=_get_int("REPLY_INTERVAL_MINUTES", 2),
        send_interval_minutes=_get_int("SEND_INTERVAL_MINUTES", 5),
        retry_interval_minutes=_get_int("RETRY_INTERVAL_MINUTES", 15),
        health_interval_minutes=_get_int("HEALTH_INTERVAL_MINUTES", 10),
    )

    notifications = NotificationConfig(
        desktop=_get_bool("NOTIFY_DESKTOP", True),
        sound=_get_bool("NOTIFY_SOUND", True),
        console=_get_bool("NOTIFY_CONSOLE", True),
    )

    ai = AIConfig(
        provider=_get_str("AI_PROVIDER"),
        api_key=_get_str("AI_API_KEY"),
        model=_get_str("AI_MODEL"),
    )

    return Config(
        base_dir=BASE_DIR,
        secret_key=_get_str("SECRET_KEY", "dev-insecure-secret-change-me"),
        flask_host=_get_str("FLASK_HOST", "127.0.0.1"),
        flask_port=_get_int("FLASK_PORT", 5000),
        flask_debug=_get_bool("FLASK_DEBUG", False),
        dashboard_username=_get_str("DASHBOARD_USERNAME", "admin"),
        dashboard_password=_get_str("DASHBOARD_PASSWORD", "changeme"),
        dashboard_password_hash=_get_str("DASHBOARD_PASSWORD_HASH"),
        session_timeout_minutes=_get_int("SESSION_TIMEOUT_MINUTES", 60),
        session_cookie_secure=_get_bool("SESSION_COOKIE_SECURE", False),
        smtp=smtp,
        gmail=gmail,
        business_hours=business_hours,
        scheduler=scheduler,
        notifications=notifications,
        ai=ai,
        backup_retention=_get_int("BACKUP_RETENTION", 30),
        log_retention_days=_get_int("LOG_RETENTION_DAYS", 30),
        database_dir=database_dir,
        logs_dir=logs_dir,
        backups_dir=backups_dir,
        templates_dir=templates_dir,
        email_templates_dir=email_templates_dir,
    )


# A module-level singleton is convenient for the rest of the app to import.
config: Config = load_config()
