"""Gmail API client for LeadFlow AI — READING ONLY.

This wrapper authenticates via OAuth (installed-app flow) and exposes the
read/inspect operations the platform needs: listing labels, finding threads
inside the "Follow Up" label, fetching thread/message detail and removing the
label. It deliberately exposes **no send capability** — outbound mail is SMTP
only (see :mod:`smtp_client`).

If credentials are not present the client reports ``available == False`` and all
methods degrade to empty results, so the app still runs (dashboard, SMTP, etc.)
without Gmail configured.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import GmailConfig, config
from logging_manager import get_logger
from utils import html_to_text

log = get_logger("import")


@dataclass
class GmailMessage:
    """Normalised view of a single Gmail message."""

    id: str
    thread_id: str
    rfc_message_id: Optional[str]
    references: Optional[str]
    in_reply_to: Optional[str]
    from_header: str
    to_header: str
    subject: str
    date: str
    snippet: str
    body_text: str
    label_ids: List[str] = field(default_factory=list)
    internal_date_ms: int = 0


@dataclass
class GmailThread:
    id: str
    messages: List[GmailMessage]

    @property
    def first(self) -> Optional[GmailMessage]:
        return self.messages[0] if self.messages else None

    @property
    def last(self) -> Optional[GmailMessage]:
        return self.messages[-1] if self.messages else None


class GmailClient:
    """Thin, fault-tolerant wrapper over the Gmail REST API (read scope)."""

    def __init__(self, gmail_config: GmailConfig = config.gmail) -> None:
        self._cfg = gmail_config
        self._service = None
        self._label_cache: Dict[str, str] = {}

    # -- auth -------------------------------------------------------------- #

    def _build_service(self):
        """Lazily build the Google API service, refreshing/creating tokens."""
        if self._service is not None:
            return self._service
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except Exception as exc:
            log.warning("Google API libraries unavailable: %s", exc)
            return None

        creds: Optional[Credentials] = None
        token_file = self._cfg.token_file
        try:
            if token_file.exists():
                creds = Credentials.from_authorized_user_file(
                    str(token_file), self._cfg.scopes
                )
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                elif self._cfg.credentials_file.exists() and self._cfg.allow_interactive_auth:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(self._cfg.credentials_file), self._cfg.scopes
                    )
                    # run_local_server opens a browser the first time only.
                    creds = flow.run_local_server(port=0)
                elif not self._cfg.allow_interactive_auth:
                    log.warning(
                        "Gmail not authorized and interactive auth is disabled "
                        "(headless). Supply a valid token.json (via GMAIL_TOKEN_JSON "
                        "or the mounted volume)."
                    )
                    return None
                else:
                    log.warning(
                        "No Gmail credentials file at %s", self._cfg.credentials_file
                    )
                    return None
                token_file.parent.mkdir(parents=True, exist_ok=True)
                token_file.write_text(creds.to_json(), encoding="utf-8")

            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            return self._service
        except Exception as exc:
            log.error("Gmail authentication failed: %s", exc)
            return None

    @property
    def available(self) -> bool:
        return self._build_service() is not None

    # -- labels ------------------------------------------------------------ #

    def _resolve_label_id(self, label_name: str) -> Optional[str]:
        if label_name in self._label_cache:
            return self._label_cache[label_name]
        service = self._build_service()
        if service is None:
            return None
        try:
            resp = service.users().labels().list(userId="me").execute()
            for label in resp.get("labels", []):
                self._label_cache[label["name"]] = label["id"]
            return self._label_cache.get(label_name)
        except Exception as exc:
            log.error("Could not list labels: %s", exc)
            return None

    def list_labels(self) -> List[str]:
        service = self._build_service()
        if service is None:
            return []
        try:
            resp = service.users().labels().list(userId="me").execute()
            return [lbl["name"] for lbl in resp.get("labels", [])]
        except Exception as exc:
            log.error("Could not list labels: %s", exc)
            return []

    # -- threads / messages ------------------------------------------------ #

    def list_label_threads(self, label_name: Optional[str] = None) -> List[str]:
        """Return thread IDs currently carrying the follow-up label."""
        label_name = label_name or self._cfg.label
        service = self._build_service()
        if service is None:
            return []
        label_id = self._resolve_label_id(label_name)
        if label_id is None:
            log.warning("Label '%s' not found in Gmail", label_name)
            return []
        thread_ids: List[str] = []
        try:
            page_token: Optional[str] = None
            while True:
                resp = (
                    service.users()
                    .threads()
                    .list(userId="me", labelIds=[label_id], pageToken=page_token)
                    .execute()
                )
                thread_ids.extend(t["id"] for t in resp.get("threads", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except Exception as exc:
            log.error("Could not list threads for label '%s': %s", label_name, exc)
        return thread_ids

    def get_thread(self, thread_id: str) -> Optional[GmailThread]:
        service = self._build_service()
        if service is None:
            return None
        try:
            resp = (
                service.users()
                .threads()
                .get(userId="me", id=thread_id, format="full")
                .execute()
            )
            messages = [self._parse_message(m) for m in resp.get("messages", [])]
            return GmailThread(id=thread_id, messages=messages)
        except Exception as exc:
            log.error("Could not fetch thread %s: %s", thread_id, exc)
            return None

    def remove_label(self, thread_id: str, label_name: Optional[str] = None) -> bool:
        """Remove the follow-up label from a thread (used on completion/reply)."""
        label_name = label_name or self._cfg.label
        service = self._build_service()
        if service is None:
            return False
        label_id = self._resolve_label_id(label_name)
        if label_id is None:
            return False
        try:
            service.users().threads().modify(
                userId="me",
                id=thread_id,
                body={"removeLabelIds": [label_id]},
            ).execute()
            return True
        except Exception as exc:
            log.error("Could not remove label from thread %s: %s", thread_id, exc)
            return False

    # -- parsing ----------------------------------------------------------- #

    @staticmethod
    def _header(headers: List[dict], name: str) -> str:
        name_l = name.lower()
        for h in headers:
            if h.get("name", "").lower() == name_l:
                return h.get("value", "")
        return ""

    @classmethod
    def _extract_body(cls, payload: dict) -> str:
        """Recursively pull the best text representation from a payload."""
        mime = payload.get("mimeType", "")
        body = payload.get("body", {})
        data = body.get("data")

        if mime == "text/plain" and data:
            return cls._decode(data)
        if mime == "text/html" and data:
            return html_to_text(cls._decode(data))

        # Multipart: prefer text/plain, fall back to text/html.
        parts = payload.get("parts", []) or []
        plain = ""
        html = ""
        for part in parts:
            text = cls._extract_body(part)
            if part.get("mimeType") == "text/plain" and text:
                plain = plain or text
            elif part.get("mimeType") == "text/html" and text:
                html = html or text
            elif text and not plain:
                plain = text
        return plain or html

    @staticmethod
    def _decode(data: str) -> str:
        try:
            return base64.urlsafe_b64decode(data.encode("utf-8")).decode(
                "utf-8", errors="replace"
            )
        except Exception:
            return ""

    @classmethod
    def _parse_message(cls, message: dict) -> GmailMessage:
        payload = message.get("payload", {})
        headers = payload.get("headers", [])
        return GmailMessage(
            id=message.get("id", ""),
            thread_id=message.get("threadId", ""),
            rfc_message_id=cls._header(headers, "Message-ID") or None,
            references=cls._header(headers, "References") or None,
            in_reply_to=cls._header(headers, "In-Reply-To") or None,
            from_header=cls._header(headers, "From"),
            to_header=cls._header(headers, "To"),
            subject=cls._header(headers, "Subject"),
            date=cls._header(headers, "Date"),
            snippet=message.get("snippet", ""),
            body_text=cls._extract_body(payload),
            label_ids=message.get("labelIds", []),
            internal_date_ms=int(message.get("internalDate", 0) or 0),
        )


# Module-level singleton.
gmail_client = GmailClient()
