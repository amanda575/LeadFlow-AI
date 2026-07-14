"""LeadFlow AI — application entry point.

Boots the platform end to end:

1. Configure logging and initialise the database (create tables + seed defaults).
2. Start the APScheduler background jobs (import / replies / send / retry / …).
3. Build the Flask dashboard app (wired to the running scheduler).
4. Serve the dashboard.

Run directly for development::

    python app.py

For production, expose the WSGI callable ``application`` to gunicorn::

    gunicorn -w 1 -b 0.0.0.0:5000 app:application

Note: use a SINGLE worker so only one scheduler instance runs. The background
jobs do the real work; the web workers only render the UI.
"""

from __future__ import annotations

import atexit
import os
import signal
import sys

from config import config
from database import init_db
from logging_manager import configure_logging, get_logger

log = get_logger("dashboard")


def _bootstrap(start_scheduler: bool = True):
    """Initialise subsystems and return (flask_app, scheduler_service)."""
    configure_logging(config.logs_dir)
    init_db(config)
    log.info("Database initialised at %s", config.database_path)

    # Import after init_db so module singletons see a ready database.
    from scheduler import scheduler_service
    from dashboard import create_app

    if start_scheduler:
        scheduler_service.start()
        atexit.register(scheduler_service.shutdown)

    flask_app = create_app(scheduler_ref=scheduler_service)
    return flask_app, scheduler_service


def _install_signal_handlers(scheduler_service) -> None:
    def _handler(signum, _frame):
        log.info("Received signal %s — shutting down", signum)
        try:
            scheduler_service.shutdown()
        finally:
            sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Signals can only be set in the main thread; ignore otherwise.
            pass


def main() -> None:
    # Anchor the working directory to the app folder. The launcher's inherited
    # cwd may be unreadable (e.g. a sandboxed/restricted parent), which makes any
    # os.getcwd() call raise PermissionError; chdir'ing somewhere we own avoids it.
    try:
        os.chdir(config.base_dir)
    except OSError:
        pass

    flask_app, scheduler_service = _bootstrap(start_scheduler=True)
    _install_signal_handlers(scheduler_service)

    banner = (
        "\n"
        "==============================================\n"
        "   LeadFlow AI is running\n"
        f"   Dashboard: http://{config.flask_host}:{config.flask_port}\n"
        f"   Login:     {config.dashboard_username}\n"
        f"   Gmail label: {config.gmail.label}\n"
        f"   SMTP: {'configured' if config.smtp.configured else 'NOT configured'}\n"
        "==============================================\n"
    )
    print(banner)
    log.info("Starting dashboard on %s:%s", config.flask_host, config.flask_port)

    # use_reloader=False so the scheduler isn't started twice in debug mode.
    # load_dotenv=False because config.py already loaded .env; Flask's own loader
    # calls os.getcwd(), which can fail under restricted/sandboxed launchers.
    flask_app.run(
        host=config.flask_host,
        port=config.flask_port,
        debug=config.flask_debug,
        use_reloader=False,
        threaded=True,
        load_dotenv=False,
    )


# WSGI entry point for gunicorn. Avoid double-starting the scheduler when the
# reloader / multiple workers are in play by gating on an env flag.
def _create_wsgi():
    start = os.getenv("LEADFLOW_DISABLE_SCHEDULER", "false").lower() not in {
        "1", "true", "yes"
    }
    flask_app, _ = _bootstrap(start_scheduler=start)
    return flask_app


application = None  # populated lazily so importing this module is cheap


def __getattr__(name):  # PEP 562 lazy attribute for `app:application`
    global application
    if name == "application":
        if application is None:
            application = _create_wsgi()
        return application
    raise AttributeError(name)


if __name__ == "__main__":
    main()
