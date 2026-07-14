# LeadFlow AI — Complete User Guide (for beginners)

This guide explains, in plain language, how to set up and use LeadFlow AI.
Keep it open and follow along. Nothing here requires coding.

---

## 0. The big picture (read this first)

LeadFlow AI is a personal assistant that watches **one Gmail label** called
`Follow up`. When you label an outreach email, it becomes a "lead." The
assistant then:

1. **Imports** that conversation automatically.
2. **Sends follow-up emails** for you, on a schedule, during business hours.
3. **Watches for a reply** — and the instant someone replies, it **stops all
   follow-ups** to that person so you never pester a warm lead.

For it to work, you give it **two keys** once:

- **Sending key** = a *Gmail App Password* → lets it send emails as you.
- **Reading key** = a *Gmail API credentials file* → lets it read your inbox to
  import leads and notice replies.

You already have the app installed and running. Now we connect those two keys.

---

## 1. Start, stop, and open the app

**Open the dashboard:** in your browser go to **http://localhost:5050**
Login → Username: `admin`  Password: `changeme` (change this — see §7).

**The app runs from a Terminal window.** To start it yourself:

```bash
cd "/Users/apple/Downloads/Follow up automation/leadflow_ai"
source .venv/bin/activate
python app.py
```

- While that Terminal stays open, the website works and the assistant runs.
- Press **Ctrl + C** in that Terminal to stop it. The website then goes dark.
- To restart, run `python app.py` again.

> If `localhost:5050` ever says "can't be reached," the app simply isn't
> running — start it with the commands above.

---

## 2. Get your SENDING key (Gmail App Password) — ~3 minutes

This lets the app send emails through your Gmail.

1. Go to **https://myaccount.google.com** and sign in as `amanda@seoleads.me`.
2. Click **Security** in the left menu.
3. Make sure **2-Step Verification** is **ON**. If it's off, turn it on first
   (Google will text you a code). App Passwords require this.
4. Now go to **https://myaccount.google.com/apppasswords**
   (or search "App passwords" in the search bar at the top).
5. Type a name like **LeadFlow** and click **Create**.
6. Google shows a **16-character password** like `abcd efgh ijkl mnop`.
   Copy it. (You can keep or remove the spaces — both work.)

**Where it goes:**
1. Open the file `.env` inside the project folder
   (`/Users/apple/Downloads/Follow up automation/leadflow_ai/.env`).
2. Find the line `SMTP_PASSWORD=`
3. Paste your code right after the `=`, e.g. `SMTP_PASSWORD=abcdefghijklmnop`
4. Save the file. Restart the app (Ctrl+C, then `python app.py`).

✅ Now the app can SEND. (It still can't import leads yet — that's the next key.)

---

## 3. Get your READING key (Gmail API file) — ~10 minutes

This lets the app read your inbox to import labeled threads and detect replies.
It's the fiddliest step, but you only do it once.

1. Go to **https://console.cloud.google.com** and sign in.
2. **Create a project:** top bar → project dropdown → **New Project** →
   name it `LeadFlow` → **Create**. Wait a few seconds, then select it.
3. **Enable Gmail API:** in the top search bar type **Gmail API** → click it →
   **Enable**.
4. **Set up the consent screen:** left menu → **APIs & Services** →
   **OAuth consent screen**.
   - User type: **External** → Create.
   - App name: `LeadFlow`, user support email: your email, developer email:
     your email → Save and continue.
   - On "Scopes" just click **Save and continue**.
   - On "Test users" click **Add users**, add `amanda@seoleads.me`, save.
   - Back to dashboard.
5. **Create the credentials:** left menu → **Credentials** →
   **+ Create Credentials** → **OAuth client ID**.
   - Application type: **Desktop app** → name `LeadFlow Desktop` → **Create**.
   - A popup appears → click **Download JSON**.
6. **Place the file:** rename the downloaded file to exactly
   **`credentials.json`** and move it into the project's **`credentials/`**
   folder:
   `/Users/apple/Downloads/Follow up automation/leadflow_ai/credentials/credentials.json`
7. **Authorize once:** restart the app (`python app.py`). The first time, a
   browser window opens asking you to allow access — sign in, click
   **Continue / Allow**. (If it warns the app is "unverified," click
   **Advanced → Go to LeadFlow**; it's your own app, it's safe.) A
   `token.json` file is saved so you won't be asked again.

✅ Now the app can READ replies and IMPORT leads. It's fully connected.

---

## 4. Create the Gmail label and add your first lead

1. In Gmail, create a label named **exactly** `Follow up`
   (Gmail left sidebar → scroll down → **+ Create new label**).
2. Find a sent outreach email (or send one), and apply the `Follow up` label
   to it (open the email → label icon → check `Follow up`).
3. In the dashboard, click **Scan now** (bottom-left), or just wait 5 minutes.
4. Go to the **Leads** page — your contact appears as a new lead. 🎉

---

## 5. Set up your follow-up sequence and templates

**Templates** (the emails that get sent) — left menu → **Templates**:
- Two are included: `followup1.html` and `followup2.html`.
- Click the pencil to edit one. Use `{{ name }}`, `{{ company }}`,
  `{{ website }}` etc. — these get filled in per lead.
- Click **Live preview** to see how it looks. **Save** when happy.

**Sequence** (the timing) — left menu → **Follow-up Sequence**:
- Step 1: wait 2 days → send `followup1.html`
- Step 2: wait 4 more days → send `followup2.html`
- Add more steps with the form (e.g. Step 3, 7 days, another template).
- Toggle steps on/off without deleting them.

**Business hours** — left menu → **Settings**:
- Follow-ups only send between your start/end hours, weekdays only by default.
- Change the timezone to match yours.

---

## 6. Your day-to-day workflow

Once set up, this is all you do:

1. Send cold outreach as normal.
2. Apply the **`Follow up`** label to anything you want chased.
3. That's it. LeadFlow handles the rest:
   - imports the lead,
   - sends follow-ups on schedule,
   - **stops instantly** when they reply (you get a notification),
   - marks the lead Replied / Completed accordingly.

Check the **Dashboard** anytime for a snapshot, and **Leads** to manage
individuals (Send now, Pause, Resume, Retry, Delete, Open Gmail thread).

---

## 7. Important housekeeping

- **Change your dashboard password:** edit `.env`, set
  `DASHBOARD_PASSWORD=YourOwnPassword`, save, restart.
- **Backups:** the app auto-saves a database backup nightly into `backups/`.
- **Logs:** see the **Logs** page (or `logs/` folder) for everything it did.
- **Health page:** green dots = healthy. SMTP green means sending works; Gmail
  green means reading works.

---

## 8. Tour of every page

| Page | What it's for |
|------|----------------|
| **Dashboard** | At-a-glance counts + recent leads + latest replies |
| **Leads** | The full list; search, filter, and act on each lead |
| **Templates** | Write/edit the follow-up emails with live preview |
| **Follow-up Sequence** | Define how many follow-ups and how far apart |
| **Statistics** | Reply rate and a sends-vs-replies chart |
| **Exports** | Download your leads as CSV (all, replied, pending, etc.) |
| **Logs** | A searchable record of everything the app did |
| **Health** | Live status of database, scheduler, SMTP, Gmail |
| **Settings** | Business hours, timezone, notifications, theme |

---

## 9. Quick troubleshooting

| Problem | Fix |
|---------|-----|
| `localhost:5050` won't load | The app isn't running — start it (see §1) |
| "CSRF session token missing" | Already fixed; reload the page fresh |
| Health shows **SMTP red** | App Password missing/wrong in `.env` (see §2) |
| Health shows **Gmail red** | `credentials.json` missing, or not authorized yet (§3) |
| No leads importing | Label must be exactly `Follow up`; click **Scan now** |
| Port 5000 didn't work | macOS uses 5000 for AirPlay — we use 5050 instead |

---

Need help with any step? The Gmail setup (§2 and §3) is the only tricky part —
do it once and you're done forever.
