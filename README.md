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

## Schedule & de-duplication

GitHub's scheduled cron is **best-effort**: runs are frequently delayed by hours
and are sometimes dropped entirely under load. So the bot does NOT depend on one
punctual run:

- The PDF is finalised **~14:00–15:40 JST** (measured from its `Last-Modified`
  header over many days), so **16:00 JST is the earliest safe target**. It fires
  at **16:00 JST (primary)** plus catch-ups at 16:30 / 17:30 / 19:00 JST —
  `0 7`, `30 7`, `30 8`, `0 10` UTC on weekdays.
- It **de-duplicates by rate date**: the last posted date is stored in
  [`state/last_posted.txt`](state/last_posted.txt) (committed back to the repo by
  the workflow). Each new rate is posted **exactly once**, whenever the first
  successful run after publication sees it — even if that run is hours late.
  Weekends/holidays produce no new date, so nothing is posted.
- This replaced an earlier strict "post only if the rate is dated *today* (JST)"
  check, which silently skipped fresh rates whenever GitHub delayed a run past
  JST midnight.
- To change timing, edit the `cron:` lines in
  [`.github/workflows/tibor.yml`](.github/workflows/tibor.yml). A manual run
  (Actions → Run workflow) with **always_post** ticked re-posts the latest rate
  regardless of the de-dup state.

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
