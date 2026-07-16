"""Flask dashboard (application factory + routes) for LeadFlow AI.

Provides the password-protected, dark-mode Bootstrap 5 web UI: dashboard cards,
a searchable/sortable/paginated leads table with per-lead actions, template and
sequence editors with live preview, settings, logs, statistics, CSV/Excel
exports and a health page.

Security: CSRF protection (Flask-WTF), hashed password auth, server-side session
timeout, secure cookies, a lightweight in-memory rate limiter on the login form,
and input validation on every mutating endpoint. All DB access uses the ORM
(parameterised queries).
"""

from __future__ import annotations

import csv
import io
import time

import pytz
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_wtf import CSRFProtect
from sqlalchemy import func, or_
from werkzeug.security import check_password_hash, generate_password_hash

from config import config
from database import (
    get_setting,
    get_setting_bool,
    session_scope,
    set_setting,
)
from health_monitor import health_monitor
from logging_manager import get_logger
from models import (
    ActivityHistory,
    FollowUpSequence,
    Lead,
    LeadStatus,
    Log,
    Notification,
    Template,
)
from services import lead_service
from template_engine import RenderContext, engine
from utils import is_valid_email

log = get_logger("dashboard")
auth_log = get_logger("auth")

csrf = CSRFProtect()

# Very small in-memory rate limiter: {ip: [timestamps]} for login attempts.
_login_attempts: dict[str, list[float]] = defaultdict(list)
_LOGIN_WINDOW = 300  # seconds
_LOGIN_MAX = 10      # attempts per window


# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #

_USER_HASHES: Optional[dict] = None


def _user_hashes() -> dict:
    """Lazily hash every configured dashboard password (pbkdf2:sha256).

    pbkdf2 is used explicitly: it is available on every Python build, unlike
    scrypt (Werkzeug's default), which needs an OpenSSL with scrypt support.
    """
    global _USER_HASHES
    if _USER_HASHES is None:
        _USER_HASHES = {
            username: generate_password_hash(password, method="pbkdf2:sha256")
            for username, password in config.dashboard_users.items()
        }
    return _USER_HASHES


def _verify_credentials(username: str, password: str) -> bool:
    """Return True if username/password match an allowed full-access account."""
    stored = _user_hashes().get(username)
    return bool(stored and check_password_hash(stored, password))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        # Session timeout enforcement.
        last = session.get("last_active", 0)
        timeout = config.session_timeout_minutes * 60
        if timeout and time.time() - last > timeout:
            session.clear()
            flash("Session expired. Please sign in again.", "warning")
            return redirect(url_for("login"))
        session["last_active"] = time.time()
        return view(*args, **kwargs)

    return wrapped


def _rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < _LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) >= _LOGIN_MAX


# --------------------------------------------------------------------------- #
# Stats aggregation
# --------------------------------------------------------------------------- #

def _status_counts(session_obj) -> dict:
    rows = (
        session_obj.query(Lead.status, func.count(Lead.id))
        .group_by(Lead.status)
        .all()
    )
    counts = {s: 0 for s in LeadStatus}
    for status, count in rows:
        counts[status] = count
    total = sum(counts.values())

    # "Sending today" = due today and not yet sent / replied.
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    sending_today = (
        session_obj.query(func.count(Lead.id))
        .filter(Lead.replied.is_(False))
        .filter(Lead.next_followup_at.isnot(None))
        .filter(Lead.next_followup_at >= start)
        .filter(Lead.next_followup_at < end)
        .scalar()
    )
    return {
        "total": total,
        "pending": counts[LeadStatus.PENDING],
        "waiting": counts[LeadStatus.WAITING],
        "sending_today": sending_today or 0,
        "replied": counts[LeadStatus.REPLIED],
        "completed": counts[LeadStatus.COMPLETED],
        "paused": counts[LeadStatus.PAUSED],
        "failed": counts[LeadStatus.FAILED],
    }


# --------------------------------------------------------------------------- #
# Application factory
# --------------------------------------------------------------------------- #

def _human_dt(value) -> str:
    """Format a stored UTC datetime/ISO string as a readable time in the
    configured business timezone, e.g. "Jul 17, 2026 · 12:11 AM IST"."""
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    tz_name = get_setting("business_timezone", "Asia/Kolkata")
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.utc
    aware = pytz.utc.localize(value) if value.tzinfo is None else value
    local = aware.astimezone(tz)
    abbr = local.strftime("%Z") or ""
    return local.strftime("%b %d, %Y · %I:%M %p").replace(" 0", " ") + (f" {abbr}" if abbr else "")


def create_app(scheduler_ref=None) -> Flask:
    app = Flask(
        __name__,
        template_folder="templates/dashboard",
        static_folder="static",
    )
    app.config.update(
        SECRET_KEY=config.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.session_cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=config.session_timeout_minutes),
        WTF_CSRF_TIME_LIMIT=None,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    csrf.init_app(app)
    app.jinja_env.filters["human"] = _human_dt

    if scheduler_ref is not None:
        health_monitor.bind_scheduler(scheduler_ref)

    _register_context(app)
    _register_auth(app)
    _register_pages(app, scheduler_ref)
    _register_actions(app)
    _register_api(app)
    _register_errors(app)
    return app


def _register_context(app: Flask) -> None:
    @app.context_processor
    def inject_globals():
        unread = 0
        try:
            session_obj = None
            with session_scope() as s:
                unread = s.query(func.count(Notification.id)).filter(
                    Notification.read.is_(False)
                ).scalar() or 0
        except Exception:
            unread = 0
        return {
            "app_name": "LeadFlow AI",
            "theme": get_setting("theme", "dark"),
            "unread_notifications": unread,
            "current_year": datetime.utcnow().year,
        }


def _register_errors(app: Flask) -> None:
    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404, message="Page not found"), 404

    @app.errorhandler(500)
    def server_error(_e):
        return render_template("error.html", code=500, message="Internal error"), 500


# --------------------------------------------------------------------------- #
# Auth routes
# --------------------------------------------------------------------------- #

def _register_auth(app: Flask) -> None:
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            ip = request.remote_addr or "unknown"
            if _rate_limited(ip):
                auth_log.warning("Rate-limited login from %s", ip)
                flash("Too many attempts. Try again later.", "danger")
                return render_template("login.html"), 429
            _login_attempts[ip].append(time.time())

            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            if _verify_credentials(username, password):
                session.clear()
                session["authenticated"] = True
                session["username"] = username
                session["last_active"] = time.time()
                session.permanent = True
                auth_log.info("Successful login: %s from %s", username, ip)
                nxt = request.args.get("next") or url_for("dashboard")
                return redirect(nxt)
            auth_log.warning("Failed login for '%s' from %s", username, ip)
            flash("Invalid username or password.", "danger")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        flash("Signed out.", "success")
        return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Page routes
# --------------------------------------------------------------------------- #

def _register_pages(app: Flask, scheduler_ref) -> None:
    @app.route("/")
    @login_required
    def dashboard():
        with session_scope() as s:
            stats = _status_counts(s)
            recent = (
                s.query(Lead).order_by(Lead.updated_at.desc()).limit(8).all()
            )
            recent_data = [l.to_dict() for l in recent]
            recent_replies = (
                s.query(Lead)
                .filter(Lead.replied.is_(True))
                .order_by(Lead.reply_at.desc())
                .limit(5)
                .all()
            )
            replies_data = [l.to_dict() for l in recent_replies]
        return render_template(
            "dashboard.html",
            stats=stats,
            recent=recent_data,
            recent_replies=replies_data,
            active="dashboard",
        )

    @app.route("/leads")
    @login_required
    def leads():
        page = max(1, request.args.get("page", 1, type=int))
        per_page = min(100, max(5, request.args.get("per_page", 25, type=int)))
        query_text = (request.args.get("q") or "").strip()
        status_filter = request.args.get("status") or ""
        sort = request.args.get("sort", "updated_at")
        direction = request.args.get("dir", "desc")

        with session_scope() as s:
            query = s.query(Lead)
            if query_text:
                like = f"%{query_text}%"
                query = query.filter(
                    or_(
                        Lead.email.ilike(like),
                        Lead.name.ilike(like),
                        Lead.company.ilike(like),
                        Lead.subject.ilike(like),
                        Lead.website.ilike(like),
                        Lead.reply_body.ilike(like),
                    )
                )
            if status_filter:
                try:
                    query = query.filter(Lead.status == LeadStatus(status_filter))
                except ValueError:
                    pass

            sort_col = {
                "email": Lead.email,
                "name": Lead.name,
                "company": Lead.company,
                "status": Lead.status,
                "date_added": Lead.date_added,
                "next_followup_at": Lead.next_followup_at,
                "updated_at": Lead.updated_at,
            }.get(sort, Lead.updated_at)
            sort_col = sort_col.desc() if direction == "desc" else sort_col.asc()
            query = query.order_by(sort_col)

            total = query.count()
            rows = query.offset((page - 1) * per_page).limit(per_page).all()
            data = [l.to_dict() for l in rows]

        pages = max(1, (total + per_page - 1) // per_page)
        return render_template(
            "leads.html",
            leads=data,
            page=page,
            pages=pages,
            per_page=per_page,
            total=total,
            q=query_text,
            status=status_filter,
            sort=sort,
            dir=direction,
            statuses=[s.value for s in LeadStatus],
            active="leads",
        )

    @app.route("/leads/<int:lead_id>")
    @login_required
    def lead_detail(lead_id: int):
        with session_scope() as s:
            lead = s.get(Lead, lead_id)
            if lead is None:
                abort(404)
            data = lead.to_dict()
            data["reply_body"] = lead.reply_body
            data["last_error"] = lead.last_error
            activities = (
                s.query(ActivityHistory)
                .filter_by(lead_id=lead_id)
                .order_by(ActivityHistory.created_at.desc())
                .all()
            )
            acts = [
                {
                    "created_at": a.created_at.isoformat(),
                    "action": a.action,
                    "detail": a.detail,
                }
                for a in activities
            ]
        return render_template("lead_detail.html", lead=data, activities=acts, active="leads")

    @app.route("/templates")
    @login_required
    def templates_page():
        with session_scope() as s:
            tpls = s.query(Template).order_by(Template.name).all()
            data = [
                {
                    "id": t.id,
                    "name": t.name,
                    "subject": t.subject,
                    "html_body": t.html_body,
                    "text_body": t.text_body,
                    "description": t.description,
                }
                for t in tpls
            ]
        return render_template("templates.html", templates=data, active="templates")

    @app.route("/sequence")
    @login_required
    def sequence_page():
        with session_scope() as s:
            steps = (
                s.query(FollowUpSequence)
                .order_by(FollowUpSequence.step_number)
                .all()
            )
            data = [st.to_dict() for st in steps]
            names = [t.name for t in s.query(Template).order_by(Template.name).all()]
        return render_template("sequence.html", steps=data, template_names=names, active="sequence")

    @app.route("/settings")
    @login_required
    def settings_page():
        keys = [
            "business_start_hour", "business_end_hour", "business_timezone",
            "business_weekdays_only", "notify_desktop", "notify_sound",
            "notify_console", "theme", "scheduler_import_minutes",
            "scheduler_reply_minutes", "scheduler_send_minutes",
            "max_retry_count", "auto_remove_label",
        ]
        values = {k: get_setting(k, "") for k in keys}
        smtp_info = {
            "host": config.smtp.host,
            "port": config.smtp.port,
            "username": config.smtp.username,
            "use_tls": config.smtp.use_tls,
            "use_ssl": config.smtp.use_ssl,
            "from_email": config.smtp.from_email,
            "configured": config.smtp.configured,
        }
        gmail_info = {
            "label": config.gmail.label,
            "configured": config.gmail.configured,
        }
        return render_template(
            "settings.html",
            values=values,
            smtp=smtp_info,
            gmail=gmail_info,
            active="settings",
        )

    @app.route("/logs")
    @login_required
    def logs_page():
        page = max(1, request.args.get("page", 1, type=int))
        per_page = 50
        category = request.args.get("category") or ""
        with session_scope() as s:
            query = s.query(Log)
            if category:
                query = query.filter(Log.category == category)
            query = query.order_by(Log.created_at.desc())
            total = query.count()
            rows = query.offset((page - 1) * per_page).limit(per_page).all()
            data = [
                {
                    "created_at": r.created_at.isoformat(sep=" ", timespec="seconds"),
                    "category": r.category,
                    "level": r.level,
                    "message": r.message,
                }
                for r in rows
            ]
            categories = [
                c[0] for c in s.query(Log.category).distinct().all()
            ]
        pages = max(1, (total + per_page - 1) // per_page)
        return render_template(
            "logs.html",
            logs=data,
            page=page,
            pages=pages,
            category=category,
            categories=categories,
            active="logs",
        )

    @app.route("/statistics")
    @login_required
    def statistics_page():
        with session_scope() as s:
            stats = _status_counts(s)
            # Replies vs sends over last 14 days.
            since = datetime.utcnow() - timedelta(days=14)
            daily = defaultdict(lambda: {"sent": 0, "replied": 0})
            for lead in s.query(Lead).filter(Lead.last_sent_at >= since).all():
                if lead.last_sent_at:
                    daily[lead.last_sent_at.date().isoformat()]["sent"] += 1
            for lead in s.query(Lead).filter(Lead.reply_at >= since).all():
                if lead.reply_at:
                    daily[lead.reply_at.date().isoformat()]["replied"] += 1
            series = sorted(daily.items())
            reply_rate = (
                round(stats["replied"] / stats["total"] * 100, 1)
                if stats["total"]
                else 0.0
            )
        return render_template(
            "statistics.html",
            stats=stats,
            series=series,
            reply_rate=reply_rate,
            active="statistics",
        )

    @app.route("/exports")
    @login_required
    def exports_page():
        return render_template("exports.html", active="exports")

    @app.route("/health")
    @login_required
    def health_page():
        report = health_monitor.report().to_dict()
        jobs = scheduler_ref.jobs_info() if scheduler_ref else []
        return render_template("health.html", health=report, jobs=jobs, active="health")


# --------------------------------------------------------------------------- #
# Mutating action routes (forms)
# --------------------------------------------------------------------------- #

def _register_actions(app: Flask) -> None:
    @app.route("/leads/<int:lead_id>/action", methods=["POST"])
    @login_required
    def lead_action(lead_id: int):
        action = request.form.get("action", "")
        handlers = {
            "send": lead_service.send_lead_now,
            "pause": lead_service.pause_lead,
            "resume": lead_service.resume_lead,
            "retry": lead_service.retry_lead,
            "delete": lead_service.delete_lead,
        }
        handler = handlers.get(action)
        if handler is None:
            flash("Unknown action.", "danger")
        else:
            ok = handler(lead_id)
            flash(
                f"Action '{action}' {'succeeded' if ok else 'had no effect'}.",
                "success" if ok else "warning",
            )
        if action == "delete":
            return redirect(url_for("leads"))
        return redirect(request.referrer or url_for("leads"))

    @app.route("/templates/save", methods=["POST"])
    @login_required
    def template_save():
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Template name is required.", "danger")
            return redirect(url_for("templates_page"))
        with session_scope() as s:
            tpl = s.query(Template).filter_by(name=name).first()
            if tpl is None:
                tpl = Template(name=name)
                s.add(tpl)
            tpl.subject = request.form.get("subject", "")
            tpl.html_body = request.form.get("html_body", "")
            tpl.text_body = request.form.get("text_body", "")
            tpl.description = request.form.get("description", "")
        flash(f"Template '{name}' saved.", "success")
        return redirect(url_for("templates_page"))

    @app.route("/templates/<int:template_id>/delete", methods=["POST"])
    @login_required
    def template_delete(template_id: int):
        with session_scope() as s:
            tpl = s.get(Template, template_id)
            if tpl:
                s.delete(tpl)
                flash(f"Template '{tpl.name}' deleted.", "success")
        return redirect(url_for("templates_page"))

    @app.route("/sequence/save", methods=["POST"])
    @login_required
    def sequence_save():
        try:
            step_number = int(request.form.get("step_number", "0"))
            delay_days = int(request.form.get("delay_days", "2"))
        except ValueError:
            flash("Step number and delay must be integers.", "danger")
            return redirect(url_for("sequence_page"))
        template_name = (request.form.get("template_name") or "").strip()
        enabled = request.form.get("enabled") == "on"
        if step_number < 1 or not template_name:
            flash("A valid step number and template are required.", "danger")
            return redirect(url_for("sequence_page"))
        with session_scope() as s:
            step = s.query(FollowUpSequence).filter_by(step_number=step_number).first()
            if step is None:
                step = FollowUpSequence(step_number=step_number)
                s.add(step)
            step.delay_days = delay_days
            step.template_name = template_name
            step.enabled = enabled
        flash(f"Sequence step {step_number} saved.", "success")
        return redirect(url_for("sequence_page"))

    @app.route("/sequence/<int:step_id>/delete", methods=["POST"])
    @login_required
    def sequence_delete(step_id: int):
        with session_scope() as s:
            step = s.get(FollowUpSequence, step_id)
            if step:
                s.delete(step)
                flash(f"Step {step.step_number} deleted.", "success")
        return redirect(url_for("sequence_page"))

    @app.route("/settings/save", methods=["POST"])
    @login_required
    def settings_save():
        bool_keys = {
            "business_weekdays_only", "notify_desktop", "notify_sound",
            "notify_console", "auto_remove_label",
        }
        text_keys = {
            "business_start_hour", "business_end_hour", "business_timezone",
            "theme", "scheduler_import_minutes", "scheduler_reply_minutes",
            "scheduler_send_minutes", "max_retry_count",
        }
        for key in text_keys:
            if key in request.form:
                set_setting(key, request.form.get(key, "").strip())
        for key in bool_keys:
            set_setting(key, "true" if request.form.get(key) == "on" else "false")
        flash("Settings saved.", "success")
        log.info("Settings updated via dashboard")
        return redirect(url_for("settings_page"))

    @app.route("/scan", methods=["POST"])
    @login_required
    def manual_scan():
        imported = lead_service.import_leads()
        from reply_detector import reply_detector

        replies = reply_detector.detect()
        flash(f"Scan complete: {imported} imported, {replies} new replies.", "success")
        return redirect(request.referrer or url_for("dashboard"))


# --------------------------------------------------------------------------- #
# JSON API + exports
# --------------------------------------------------------------------------- #

def _register_api(app: Flask) -> None:
    @app.route("/api/stats")
    @login_required
    def api_stats():
        with session_scope() as s:
            return jsonify(_status_counts(s))

    @app.route("/api/health")
    @login_required
    def api_health():
        return jsonify(health_monitor.report().to_dict())

    @app.route("/api/template/preview", methods=["POST"])
    @login_required
    def api_template_preview():
        # Preview an unsaved template body directly.
        subject = request.form.get("subject", "")
        html_body = request.form.get("html_body", "")
        text_body = request.form.get("text_body", "")
        ctx = RenderContext(
            name="Jordan", company="Acme Co", website="https://acme.example",
            industry="Retail", city="Austin",
        )
        # Render ad-hoc strings via the engine's sandbox.
        rendered_subject = engine._render_string(subject, ctx)  # noqa: SLF001
        rendered_html = engine._render_string(html_body, ctx)   # noqa: SLF001
        rendered_text = engine._render_string(text_body, ctx)   # noqa: SLF001
        return jsonify(
            {"subject": rendered_subject, "html": rendered_html, "text": rendered_text}
        )

    @app.route("/exports/leads.csv")
    @login_required
    def export_csv():
        scope = request.args.get("scope", "all")
        status_map = {
            "replied": LeadStatus.REPLIED,
            "pending": LeadStatus.PENDING,
            "waiting": LeadStatus.WAITING,
            "completed": LeadStatus.COMPLETED,
            "failed": LeadStatus.FAILED,
            "paused": LeadStatus.PAUSED,
        }
        with session_scope() as s:
            query = s.query(Lead)
            if scope in status_map:
                query = query.filter(Lead.status == status_map[scope])
            rows = query.order_by(Lead.date_added.desc()).all()
            records = [l.to_dict() for l in rows]

        buffer = io.StringIO()
        fieldnames = [
            "id", "name", "email", "company", "website", "subject", "status",
            "current_stage", "date_added", "next_followup_at", "last_sent_at",
            "replied", "reply_at", "reply_from", "reply_sentiment", "gmail_url",
        ]
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)
        filename = f"leadflow-{scope}-{datetime.utcnow():%Y%m%d}.csv"
        return Response(
            buffer.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/notifications/read", methods=["POST"])
    @login_required
    def mark_notifications_read():
        with session_scope() as s:
            s.query(Notification).filter(Notification.read.is_(False)).update(
                {Notification.read: True}
            )
        return redirect(request.referrer or url_for("dashboard"))
