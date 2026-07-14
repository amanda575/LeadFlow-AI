"""Pluggable AI provider abstraction (bonus feature scaffolding).

LeadFlow AI never hardcodes an AI vendor. Instead it defines an abstract
:class:`AIProvider` interface plus a registry/factory so a concrete provider
(OpenAI, Anthropic, Gemini, local model, …) can be dropped in later by
implementing the interface and registering it — without touching call sites.

When ``AI_PROVIDER`` is empty the :class:`NullAIProvider` is used: every method
degrades gracefully to a safe heuristic or no-op so the rest of the app behaves
identically whether or not AI is configured.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Optional

from config import AIConfig, config


@dataclass
class PersonalizationContext:
    """Inputs available when generating or personalising an email."""

    name: str
    company: Optional[str] = None
    website: Optional[str] = None
    industry: Optional[str] = None
    city: Optional[str] = None
    previous_subject: Optional[str] = None
    step_number: int = 1


class AIProvider(abc.ABC):
    """Abstract interface every concrete AI integration must implement."""

    name: str = "abstract"

    @abc.abstractmethod
    def personalize_email(
        self, base_html: str, base_text: str, ctx: PersonalizationContext
    ) -> tuple[str, str]:
        """Return possibly-rewritten ``(html, text)`` tailored to *ctx*."""

    @abc.abstractmethod
    def summarize_reply(self, reply_body: str) -> str:
        """Return a one-line summary of a prospect's reply."""

    @abc.abstractmethod
    def analyze_sentiment(self, reply_body: str) -> str:
        """Return one of ``positive`` | ``neutral`` | ``negative``."""

    @abc.abstractmethod
    def extract_company_website(self, text: str) -> Dict[str, Optional[str]]:
        """Return ``{"company": ..., "website": ...}`` extracted from *text*."""

    @abc.abstractmethod
    def suggest_send_time(self, ctx: PersonalizationContext) -> Optional[datetime]:
        """Return a suggested optimal send time, or ``None`` to use defaults."""

    def should_stop_campaign(self, reply_body: str) -> bool:
        """Heuristic: stop on clearly negative intent. Overridable."""
        return self.analyze_sentiment(reply_body) == "negative"


class NullAIProvider(AIProvider):
    """No-op provider used when AI is disabled. Pure, deterministic heuristics."""

    name = "null"

    _NEGATIVE = (
        "unsubscribe", "not interested", "no thanks", "stop emailing",
        "remove me", "do not contact", "leave me alone", "fuck off",
    )
    _POSITIVE = (
        "yes", "interested", "sounds good", "let's talk", "sure",
        "happy to", "great", "call me", "schedule",
    )

    def personalize_email(self, base_html, base_text, ctx):  # noqa: D401
        return base_html, base_text

    def summarize_reply(self, reply_body: str) -> str:
        snippet = (reply_body or "").strip().replace("\n", " ")
        return (snippet[:117] + "…") if len(snippet) > 118 else snippet

    def analyze_sentiment(self, reply_body: str) -> str:
        text = (reply_body or "").lower()
        if any(kw in text for kw in self._NEGATIVE):
            return "negative"
        if any(kw in text for kw in self._POSITIVE):
            return "positive"
        return "neutral"

    def extract_company_website(self, text: str) -> Dict[str, Optional[str]]:
        # Defer to the heuristic utilities for the null provider.
        from utils import extract_website, guess_company

        return {
            "company": guess_company(""),  # no email context here
            "website": extract_website(text),
        }

    def suggest_send_time(self, ctx):  # noqa: D401
        return None


# --------------------------------------------------------------------------- #
# Registry / factory
# --------------------------------------------------------------------------- #

_REGISTRY: Dict[str, Callable[[AIConfig], AIProvider]] = {}


def register_provider(
    name: str, factory: Callable[[AIConfig], AIProvider]
) -> None:
    """Register a provider factory under a lowercase *name*."""
    _REGISTRY[name.lower()] = factory


def get_ai_provider(cfg: AIConfig = config.ai) -> AIProvider:
    """Return the configured provider, or :class:`NullAIProvider` if disabled."""
    if not cfg.enabled:
        return NullAIProvider()
    factory = _REGISTRY.get(cfg.provider.lower())
    if factory is None:
        # Unknown provider name configured -> fail safe to null behaviour.
        return NullAIProvider()
    try:
        return factory(cfg)
    except Exception:
        return NullAIProvider()


# The null provider is always available under its own name too.
register_provider("null", lambda _cfg: NullAIProvider())
