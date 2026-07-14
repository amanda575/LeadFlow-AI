# LeadFlow AI

An intelligent email **follow-up automation platform**. You drop email
conversations into a Gmail label; LeadFlow AI imports them as leads, sends
multi-step follow-ups over SMTP (preserving Gmail threading), watches for
replies, and **the instant a prospect replies it cancels every remaining
follow-up** — exactly like Instantly / Smartlead / Reply.io, running entirely on
your own machine.

> Built for cold-outreach SEO lead generation, but the engine is fully generic.

---

## Highlights

- **Gmail label import** — every thread in the `Follow Up` label becomes a lead.
- **Data-driven sequences** — unlimited steps (delay + template) editable in the UI.
- **SMTP sending only** — Gmail API is used strictly for *reading* replies.
- **In-thread follow-ups** — `In-Reply-To` / `References` keep replies threaded.
- **Instant reply detection** — stops campaigns, notifies (desktop + sound + log).
- **Dark-mode Flask dashboard** — leads, templates, sequences, settings, logs,
  statistics, exports, health.
- **Business-hours sending**, retries, pause/resume, CSV export, search,
  duplicate detection, nightly backups, rotating logs.
- **Pluggable AI** — abstract provider interface (personalisation, summarisation,
  sentiment, company/website extraction, send-time suggestion). Disabled by
  default; no vendor hardcoded.

---

## Architecture

```
app.py            Entry point: boots logging, DB, scheduler, dashboard.
config.py         Immutable env-driven configuration (dataclasses).
database.py       Engine, scoped sessions, seeding, settings helpers.
models.py         SQLAlchemy ORM: Lead, Template, FollowUpSequence, Log,
                  Setting, Notification, ActivityHistory.
gmail_client.py   Gmail API wrapper — READ ONLY (labels/threads/messages).
smtp_client.py    SMTP sender — the ONLY outbound path; threading + retries.
template_engine.py  Sandboxed Jinja2 rendering (HTML + text + live preview).
services.py       LeadService: import / send / retry / pause / resume orchestration.
reply_detector.py Polls threads; marks REPLIED and cancels follow-ups.
notifications.py  Desktop / sound / console notifications + history.
scheduler.py      APScheduler jobs (import, replies, send, retry, backup, …).
backup_manager.py Consistent nightly SQLite backups with retention.
health_monitor.py Aggregated subsystem health for the dashboard.
ai_provider.py    Abstract AIProvider + NullAIProvider + registry.
dashboard.py      Flask app factory + all routes (auth, CSRF, exports, API).
logging_manager.py Rotating file + console + DB-sink logging.
utils.py          Pure helpers (parsing, business-hours math, HTML→text).
```

State flow: `PENDING → WAITING → … → COMPLETED`, with `REPLIED`, `PAUSED` and
`FAILED` as off-ramps. A detected reply always wins and halts everything.

---

## Quick start

### 1. Install

```bash
cd leadflow_ai
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> Python 3.12+ is recommended; the code also runs on 3.9+.

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:

- **Dashboard** — set `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` and a long
  random `SECRET_KEY`.
- **SMTP** — your sending host. For Gmail, create an
  [App Password](https://support.google.com/accounts/answer/185833) and use it
  as `SMTP_PASSWORD` (host `smtp.gmail.com`, port `587`, TLS on).
- **Business hours / timezone** — when sends are allowed.

### 3. Gmail API (reading replies)

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a
   project and enable the **Gmail API**.
2. Create an **OAuth client ID** of type *Desktop app* and download the JSON.
3. Save it as `credentials/credentials.json` (path configurable via
   `GMAIL_CREDENTIALS_FILE`).
4. On first run a browser opens to authorise read access; the token is cached to
   `credentials/token.json`.

> No Gmail credentials? The app still runs — dashboard, templates, SMTP sending
> and scheduling all work; only import/reply-detection are paused.

### 4. Create the Gmail label

Create a Gmail label named exactly **`Follow Up`** (or change `GMAIL_LABEL`).
Apply it to any sent outreach you want followed up.

### 5. Run

```bash
python app.py
```

Open <http://127.0.0.1:5000> and sign in. Click **Scan now** to import
immediately, or wait for the 5-minute import job.

### Production

```bash
gunicorn -w 1 -b 0.0.0.0:5000 app:application
```

Use **one worker** so a single scheduler instance runs. (Set
`LEADFLOW_DISABLE_SCHEDULER=true` on extra web workers if you scale out.)

---

## How a lead flows

1. **Import** (every 5 min): each new labelled thread → a `Lead`. The prospect is
   the recipient of your outreach; company/website are inferred (or AI-extracted).
2. **Schedule**: step 1's delay (default 2 days) sets `next_followup_at`, clamped
   into the next business-hours window.
3. **Send** (every 5 min, in business hours): renders the step's template,
   threads under your last message, sends via SMTP, advances the stage and
   schedules the next step. Failures retry up to `max_retry_count`.
4. **Reply detection** (every 2 min): any message in the thread from someone
   other than you ⇒ lead marked `REPLIED`, all follow-ups cancelled, a
   notification fires, and (optionally) the Gmail label is removed.
5. **Complete**: sequence exhausted with no reply ⇒ `COMPLETED`.

---

## Security

- Secrets live in `.env` (git-ignored), never in the web UI.
- Password-protected dashboard (hashed with pbkdf2), CSRF protection on every
  form, server-side session timeout, secure/HTTP-only cookies, login rate
  limiting, request size limits, and ORM-parameterised queries throughout.

## Backups & logs

- Nightly consistent SQLite backups to `backups/` (keeps the latest 30).
- Daily-rotating logs in `logs/` plus a searchable DB log on the **Logs** page.

## Extending AI

Implement `ai_provider.AIProvider`, register it, and set `AI_PROVIDER` /
`AI_API_KEY` / `AI_MODEL` in `.env`:

```python
from ai_provider import AIProvider, register_provider

class MyProvider(AIProvider):
    name = "myllm"
    # implement personalize_email, summarize_reply, analyze_sentiment,
    # extract_company_website, suggest_send_time ...

register_provider("myllm", lambda cfg: MyProvider())
```

With no provider configured, the `NullAIProvider` supplies safe heuristics so
behaviour is identical whether or not AI is enabled.

---

## License

Provided as-is for SeoLeads.Me internal use.
