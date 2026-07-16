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
    # When False (e.g. on a headless server), never open a browser for OAuth;
    # rely on a pre-supplied token.json that auto-refreshes instead.
    allow_interactive_auth: bool
    # gmail.modify → read threads/labels & detect replies.
    # gmail.send   → send follow-ups over HTTPS via the Gmail API (needed where
    #                outbound SMTP is blocked, e.g. Railway and most PaaS hosts).
    scopes: List[str] = field(
        default_factory=lambda: [
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        ]
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
    # How outbound follow-ups are sent: "smtp" (default, local) or "gmail_api"
    # (HTTPS — required on hosts that block SMTP, e.g. Railway).
    send_method: str

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

    # Stateful data lives under DATA_DIR when set (e.g. a mounted cloud volume),
    # so leads/logs/backups/token survive redeploys. Code assets stay in BASE_DIR.
    data_root = Path(_get_str("DATA_DIR")).expanduser() if _get_str("DATA_DIR") else BASE_DIR
    database_dir = data_root / "database"
    logs_dir = data_root / "logs"
    backups_dir = data_root / "backups"
    credentials_dir = data_root / "credentials"
    templates_dir = BASE_DIR / "templates"
    email_templates_dir = templates_dir / "email"

    for directory in (database_dir, logs_dir, backups_dir, credentials_dir,
                      email_templates_dir):
        directory.mkdir(parents=True, exist_ok=True)

    def _resolve_data_path(env_name: str, default: str) -> Path:
        raw = Path(_get_str(env_name, default)).expanduser()
        return raw if raw.is_absolute() else (data_root / raw)

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
        credentials_file=_resolve_data_path(
            "GMAIL_CREDENTIALS_FILE", "credentials/credentials.json"
        ),
        token_file=_resolve_data_path("GMAIL_TOKEN_FILE", "credentials/token.json"),
        label=_get_str("GMAIL_LABEL", "Follow Up"),
        auto_remove_label=_get_bool("GMAIL_AUTO_REMOVE_LABEL", False),
        allow_interactive_auth=_get_bool("GMAIL_ALLOW_INTERACTIVE_AUTH", True),
    )

    business_hours = BusinessHoursConfig(
        start_hour=_get_int("BUSINESS_START_HOUR", 9),
        end_hour=_get_int("BUSINESS_END_HOUR", 17),
        timezone=_get_str("BUSINESS_TIMEZONE", "Asia/Kolkata"),
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
        # Cloud platforms (Railway/Render/Heroku) inject PORT and expect the app
        # to bind 0.0.0.0. Locally we default to loopback for safety.
        flask_host=_get_str("FLASK_HOST", "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"),
        flask_port=_get_int("PORT", _get_int("FLASK_PORT", 5000)),
        flask_debug=_get_bool("FLASK_DEBUG", False),
        dashboard_username=_get_str("DASHBOARD_USERNAME", "admin"),
        dashboard_password=_get_str("DASHBOARD_PASSWORD", "changeme"),
        dashboard_password_hash=_get_str("DASHBOARD_PASSWORD_HASH"),
        session_timeout_minutes=_get_int("SESSION_TIMEOUT_MINUTES", 60),
        session_cookie_secure=_get_bool("SESSION_COOKIE_SECURE", False),
        send_method=_get_str("SEND_METHOD", "smtp").strip().lower() or "smtp",
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
