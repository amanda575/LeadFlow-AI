"""Jinja2-based email template rendering for LeadFlow AI.

Templates live in two places that the engine transparently unifies:

* The database ``templates`` table (authoritative; editable in the dashboard).
* Files under ``templates/email/`` (used to bootstrap / fall back).

Rendering is sandboxed (:class:`SandboxedEnvironment`) so operator-authored
templates cannot execute arbitrary Python. Supported variables:
``name``, ``company``, ``website``, ``service``, ``industry``, ``city`` and
``custom`` (a free-form dict).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from jinja2 import TemplateSyntaxError
from jinja2.sandbox import SandboxedEnvironment

from config import config
from database import session_scope
from logging_manager import get_logger
from models import Template
from utils import html_to_text

log = get_logger("dashboard")


@dataclass
class RenderContext:
    """Variables exposed to a template render."""

    name: str = "there"
    company: Optional[str] = None
    website: Optional[str] = None
    service: str = "SEO lead generation"
    industry: Optional[str] = None
    city: Optional[str] = None
    custom: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name or "there",
            "company": self.company,
            "website": self.website,
            "service": self.service,
            "industry": self.industry,
            "city": self.city,
            "custom": self.custom,
        }


@dataclass
class RenderedEmail:
    subject: str
    html: str
    text: str


class TemplateEngine:
    """Renders subject/html/text from a named template + context."""

    def __init__(self) -> None:
        self._env = SandboxedEnvironment(
            autoescape=True, trim_blocks=True, lstrip_blocks=True
        )

    # -- rendering --------------------------------------------------------- #

    def _render_string(self, source: str, ctx: RenderContext) -> str:
        try:
            template = self._env.from_string(source or "")
            return template.render(**ctx.as_dict())
        except TemplateSyntaxError as exc:
            log.error("Template syntax error: %s", exc)
            # Surface the raw source rather than crashing a send/preview.
            return source or ""
        except Exception as exc:  # pragma: no cover - defensive
            log.error("Template render error: %s", exc)
            return source or ""

    def render(self, template_name: str, ctx: RenderContext) -> RenderedEmail:
        """Render a stored template into a :class:`RenderedEmail`."""
        record = self._load_template(template_name)
        if record is None:
            log.error("Template '%s' not found; using empty body", template_name)
            return RenderedEmail(subject="", html="", text="")

        subject = self._render_string(record["subject"], ctx)
        html = self._render_string(record["html_body"], ctx)
        text_source = record["text_body"] or ""
        if text_source.strip():
            text = self._render_string(text_source, ctx)
        else:
            # Derive a plain-text part from the rendered HTML when none stored.
            text = html_to_text(html)
        return RenderedEmail(subject=subject, html=html, text=text)

    def preview(self, template_name: str, ctx: Optional[RenderContext] = None) -> RenderedEmail:
        """Render with sample data for the dashboard live preview."""
        ctx = ctx or RenderContext(
            name="Jordan",
            company="Acme Co",
            website="https://acme.example",
            industry="Retail",
            city="Austin",
        )
        return self.render(template_name, ctx)

    # -- storage ----------------------------------------------------------- #

    @staticmethod
    def _load_template(name: str) -> Optional[dict]:
        with session_scope() as session:
            record = session.query(Template).filter_by(name=name).first()
            if record is None:
                return None
            return {
                "subject": record.subject,
                "html_body": record.html_body,
                "text_body": record.text_body,
            }

    @staticmethod
    def export_files() -> None:
        """Write current DB templates out to ``templates/email/`` as backups."""
        config.email_templates_dir.mkdir(parents=True, exist_ok=True)
        with session_scope() as session:
            for tpl in session.query(Template).all():
                base = config.email_templates_dir / tpl.name
                base.write_text(tpl.html_body, encoding="utf-8")
                txt_name = base.with_suffix(".txt")
                txt_name.write_text(tpl.text_body, encoding="utf-8")


# Module-level singleton for convenience.
engine = TemplateEngine()
