#!/usr/bin/env python3
"""
JBA (Japanese Yen) TIBOR -> Microsoft Teams notifier.

Flow:
  1. Load the JBA rate page and find the "this month" Japanese Yen TIBOR PDF link.
  2. Download the PDF.
  3. Parse the monthly table -> tenor labels + the latest published row.
  4. Post an Adaptive Card to a Teams (Power Automate) webhook.

The published rate updates on Tokyo business days around 17:00 JST. On days
with no new publication (weekends / Japanese holidays) the PDF still shows the
last business day, so by default we only post when the latest row's date is
"today" in JST. Set ALWAYS_POST=1 to post the latest available row regardless.
"""

import os
import re
import sys
import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

import requests
import pdfplumber

# Ensure Japanese/em-dash output is safe on a Windows console (cp932) too.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

RATE_PAGE = "https://www.jbatibor.or.jp/rate/"
BASE = "https://www.jbatibor.or.jp"
JST = ZoneInfo("Asia/Tokyo")

# Match the daily Japanese Yen TIBOR PDF, e.g. /rate/pdf/JAPANESEYENTIBOR260723.pdf
PDF_LINK_RE = re.compile(r'(/rate/pdf/JAPANESEYENTIBOR\d{6}\.pdf)', re.IGNORECASE)

UA = {"User-Agent": "tibor-teams-bot/1.0 (+https://github.com/)"}


def log(msg: str) -> None:
    print(f"[tibor-bot] {msg}", flush=True)


def find_pdf_url() -> str:
    """Scrape the rate page for the current Japanese Yen TIBOR PDF link."""
    r = requests.get(RATE_PAGE, headers=UA, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    m = PDF_LINK_RE.search(r.text)
    if not m:
        raise RuntimeError("Could not find a JAPANESEYENTIBOR PDF link on the rate page.")
    return BASE + m.group(1)


def download_pdf(url: str, dest: str) -> str:
    log(f"downloading {url}")
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    with open(dest, "wb") as f:
        f.write(r.content)
    log(f"saved {len(r.content)} bytes -> {dest}")
    return dest


TENOR_RE = re.compile(r"^\d+(WEEK|MONTH|YEAR)$")
DATE_RE = re.compile(r"^\d{4}/\d{2}/\d{2}$")
RATE_RE = re.compile(r"^-?\d+\.\d+$")


def _pretty_tenor(label: str) -> str:
    """1WEEK -> 1週間, 3MONTH -> 3ヶ月, 1YEAR -> 1年 (falls back to raw label)."""
    m = re.match(r"^(\d+)(WEEK|MONTH|YEAR)$", label)
    if not m:
        return label
    n, unit = m.group(1), m.group(2)
    return {"WEEK": f"{n}週間", "MONTH": f"{n}ヶ月", "YEAR": f"{n}年"}.get(unit, label)


def parse_pdf(path: str):
    """
    Return (reference_date_str, [(tenor_label, rate_str), ...]) for the latest
    published row. The table is borderless, so we align data numbers to header
    labels by x-coordinate rather than trusting pdfplumber's cell detection.
    """
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        words = page.extract_words(use_text_flow=False)

    # Header: tenor labels and their horizontal position (left edge).
    headers = [(w["text"], w["x0"]) for w in words if TENOR_RE.match(w["text"])]
    if not headers:
        raise RuntimeError("Could not find any tenor header labels (e.g. 1MONTH).")
    headers.sort(key=lambda h: h[1])

    # Dates in the first column; the latest published date sits at the top.
    dates = sorted((w for w in words if DATE_RE.match(w["text"])),
                   key=lambda w: w["top"])
    if not dates:
        raise RuntimeError("No dates found in PDF (nothing published yet?).")
    top_word = dates[0]
    ref_date = top_word["text"]

    # Rate numbers on the same visual line as the latest date.
    row_rates = sorted(
        (w for w in words
         if RATE_RE.match(w["text"]) and abs(w["top"] - top_word["top"]) < 3),
        key=lambda w: w["x0"],
    )
    if not row_rates:
        raise RuntimeError(f"No rate values found for {ref_date}.")

    # Assign each rate to the nearest header column by left-edge distance.
    rates = []
    for label, hx in headers:
        best = min(row_rates, key=lambda w: abs(w["x0"] - hx))
        if abs(best["x0"] - hx) < 12:  # within a column width -> populated tenor
            rates.append((_pretty_tenor(label), best["text"]))

    if not rates:
        raise RuntimeError("Could not align any rates to tenor columns.")

    return ref_date, rates


def normalize_ref_date(ref: str):
    """Parse '2026/7/23' or '2026年7月23日' -> date, else None."""
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", ref)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def pct_to_bps(pct: str) -> str:
    """'0.90627' (percent) -> '90.627' (basis points), trailing zeros trimmed."""
    bps = (Decimal(pct) * 100).normalize()
    # avoid exponent notation for whole numbers (e.g. 90.00000 -> 90, not 9E+1)
    return format(bps, "f")


def build_card(ref_date: str, rates, pdf_url: str, is_today: bool):
    facts = [{"title": label, "value": f"{pct_to_bps(val)} bps"} for label, val in rates]
    subtitle = ("本日公表のレート" if is_today
                else "※本日は新規公表なし。直近公表分を表示しています。")
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "size": "Large", "weight": "Bolder",
                     "text": "全銀協 日本円TIBOR（D-TIBOR）"},
                    {"type": "TextBlock", "spacing": "None", "isSubtle": True,
                     "wrap": True, "text": f"基準日: {ref_date}"},
                    {"type": "TextBlock", "spacing": "Small", "isSubtle": True,
                     "wrap": True, "text": subtitle},
                    {"type": "FactSet", "facts": facts},
                ],
                "actions": [{
                    "type": "Action.OpenUrl",
                    "title": "元のPDFを開く",
                    "url": pdf_url,
                }],
            },
        }],
    }


def read_last_posted(path: str):
    """Return the last successfully-posted rate date, or None."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = f.read().strip()
        return dt.date.fromisoformat(s) if s else None
    except (FileNotFoundError, ValueError):
        return None


def write_last_posted(path: str, d: dt.date) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(d.isoformat())


def post_to_teams(webhook: str, card: dict) -> None:
    r = requests.post(webhook, json=card, timeout=30)
    # Power Automate returns 202; legacy connectors return 200.
    if r.status_code not in (200, 202):
        raise RuntimeError(f"Teams webhook returned {r.status_code}: {r.text[:500]}")
    log(f"posted to Teams (HTTP {r.status_code})")


def main() -> int:
    webhook = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    always_post = os.environ.get("ALWAYS_POST", "").strip() in ("1", "true", "True")
    dry_run = "--dry-run" in sys.argv or not webhook

    pdf_url = find_pdf_url()
    log(f"pdf url: {pdf_url}")
    tmp = os.path.join(os.environ.get("RUNNER_TEMP", "."), "tibor.pdf")
    download_pdf(pdf_url, tmp)

    ref_date, rates = parse_pdf(tmp)
    log(f"reference date: {ref_date}")
    for label, val in rates:
        log(f"  {label}: {pct_to_bps(val)} bps  ({val}%)")

    ref = normalize_ref_date(ref_date)
    today = dt.datetime.now(JST).date()
    is_today = (ref == today)

    # De-duplicate by the rate's own date, NOT by "is it today?". GitHub's cron
    # is best-effort and often runs hours late (sometimes past JST midnight), so
    # a strict "ref == today" check would wrongly skip a fresh rate. Instead we
    # remember the last date we posted and post any strictly newer one exactly
    # once — which also naturally skips weekends/holidays (no new date).
    state_file = os.environ.get("STATE_FILE", "state/last_posted.txt")
    last_posted = read_last_posted(state_file)
    log(f"last posted date: {last_posted}")

    if ref is None and not always_post:
        log("WARNING: could not parse reference date; skipping to avoid duplicate posts.")
        return 0

    already_posted = (last_posted is not None and ref is not None and ref <= last_posted)
    if already_posted and not always_post:
        log(f"latest rate {ref} already posted (last was {last_posted}); "
            f"nothing new — exiting without posting.")
        return 0

    card = build_card(ref_date, rates, pdf_url, is_today)

    if dry_run:
        import json
        log(f"DRY RUN — would post (last posted: {last_posted}). Card payload:")
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return 0

    post_to_teams(webhook, card)

    if ref is not None:
        write_last_posted(state_file, ref)
        log(f"recorded last-posted date {ref} -> {state_file}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
