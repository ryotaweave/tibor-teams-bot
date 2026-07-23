# TIBOR → Teams bot

Posts the latest **JBA Japanese Yen TIBOR (D-TIBOR)** rates to Microsoft Teams
every weekday evening.

Each run:
1. Reads <https://www.jbatibor.or.jp/rate/> and finds the current month's PDF.
2. Downloads and parses it (`JAPANESEYENTIBOR<YYMMDD>.pdf`).
3. Extracts the latest published row — all tenors (1週間, 1・3・6・12ヶ月).
4. Posts an Adaptive Card to a Teams webhook.

It runs on **GitHub Actions** (free, always-on cloud cron) — your PC does not
need to be on. The message is delivered through a **Power Automate "Workflows"
webhook**, the current Microsoft-supported replacement for the retired Office
365 incoming-webhook connectors.

> **Only D-TIBOR** is published — Euroyen TIBOR ended on 2024-12-30.

---

## Setup

Two parts. Do **Part A** first (you need the webhook URL for Part B).

### Part A — Create the Teams webhook (in your Weave tenant)

1. Open **Microsoft Teams** and go to the **channel** (or chat) where you want
   the rate posted.
2. Click the channel's **••• → Workflows** (or open the **Workflows** app from
   the left rail → **+ New flow**).
3. Choose the template **“Post to a channel when a webhook request is
   received.”** (For a 1:1 chat use **“Post to a chat when a webhook request is
   received.”**)
4. Sign in / confirm the connection when prompted, pick the **team + channel**
   (or chat), then **Add workflow**.
5. Teams shows a **URL** — copy it. This is your `TEAMS_WEBHOOK_URL`.
   Treat it like a password (anyone with it can post to that channel).

The card this bot sends already uses the exact envelope these workflows expect
(`type: "message"` → `attachments[].content` with
`contentType: application/vnd.microsoft.card.adaptive`), so the default template
renders it without any extra editing.

### Part B — Put it on GitHub Actions

1. Create a **new GitHub repository** (private is fine), e.g. `tibor-teams-bot`.
2. Push these files to it (from this folder):
   ```bash
   git init
   git add .
   git commit -m "TIBOR Teams bot"
   git branch -M main
   git remote add origin https://github.com/<you>/tibor-teams-bot.git
   git push -u origin main
   ```
3. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**.
   - Name: `TEAMS_WEBHOOK_URL`
   - Value: the URL from Part A. **Save.**
4. Open the **Actions** tab. If prompted, click **“I understand my workflows,
   enable them.”**

### Test it

1. **Actions** tab → **TIBOR Teams notifier** → **Run workflow**.
2. Tick **“Post the latest available rate even if not published today”** (that
   sets `ALWAYS_POST`, so it posts regardless of whether today is a publish day)
   → **Run workflow**.
3. Watch the run turn green and check the card lands in Teams. Open the run logs
   to see the parsed rates.

Once that works, leave it alone — it runs automatically (see below).

---

## Schedule

- Fires **weekdays at 09:30 UTC = 18:30 JST**, ~1.5h after the ~17:00 JST
  publication.
- GitHub cron is UTC-only and has no Japanese-holiday awareness, so it also
  fires on holidays. The script guards against this: it only posts when the
  PDF's latest row is dated **today (JST)**. On holidays / no-publish days the
  latest row is stale, so it logs and exits **without posting** — no duplicate
  or misleading messages.
- To change the time, edit the `cron:` line in
  [`.github/workflows/tibor.yml`](.github/workflows/tibor.yml). Note GitHub can
  delay scheduled runs by a few (occasionally 15–30) minutes at peak times.

---

## Files

| File | Purpose |
|------|---------|
| `tibor_bot.py` | Scrape → download → parse → post |
| `requirements.txt` | `requests`, `pdfplumber`, `tzdata` (Windows) |
| `.github/workflows/tibor.yml` | Cron + manual-run workflow |

## Running locally (optional)

Python 3.12 is installed on this machine at
`C:\Users\ryota\AppData\Local\Programs\Python\Python312\python.exe`.

```bash
# Preview without posting (prints the card JSON):
python tibor_bot.py --dry-run

# Actually post (needs the env var):
TEAMS_WEBHOOK_URL="https://..." python tibor_bot.py

# Post the latest row even if it isn't today's:
ALWAYS_POST=1 python tibor_bot.py --dry-run
```

## Troubleshooting

- **`400 Bad Request` from the webhook** — the flow was built from a different
  template that doesn't expect the card envelope. Rebuild it with the
  **“Post to a channel when a webhook request is received”** template.
- **Run is green but no card** — check the logs: if it says *"latest row date …
  != today"*, there was no publication that day (holiday), which is correct
  behaviour. Use the manual run with `ALWAYS_POST` to force a test.
- **Parsing error after a JBA site change** — the parser aligns rate numbers to
  header columns by x-position; if JBA restructures the PDF, adjust
  `parse_pdf()` in `tibor_bot.py`.
