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

import csv
import hashlib
import json
import os
import re
import sys
import datetime as dt
from decimal import Decimal
from urllib.parse import urljoin
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

# Match the daily Japanese Yen TIBOR PDF link, wherever it lives on the site.
# JBA has moved this file before (was /rate/pdf/..., now /...), so we match the
# href by filename and resolve it against the page URL — path-change proof.
PDF_LINK_RE = re.compile(
    r'href=["\']([^"\']*JAPANESEYENTIBOR\d{6}\.pdf)["\']', re.IGNORECASE)
# Fallback: a bare URL/path anywhere in the HTML if it isn't in an href.
PDF_URL_RE = re.compile(
    r'((?:https?://[^\s"\'<>]+?)?/?JAPANESEYENTIBOR\d{6}\.pdf)', re.IGNORECASE)

UA = {"User-Agent": "tibor-teams-bot/1.0 (+https://github.com/)"}

# Chart palette: the validated categorical slots (blue, orange, aqua, yellow,
# magenta) in FIXED order — assigned shortest tenor first so a tenor keeps its
# colour between days. Text stays ink-coloured; only the marks carry identity.
SERIES_COLOURS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
SURFACE = "#ffffff"
INK = "#0b0b0b"
INK_SOFT = "#52514e"
GRID = "#d9d8d4"

# Committed to the repo and served publicly to Teams; see chart_url().
CHART_PATH = "charts/tibor_5d.png"
HISTORY_PATH = "state/history.csv"
CHART_DAYS = int(os.environ.get("CHART_DAYS", "5"))


def log(msg: str) -> None:
    print(f"[tibor-bot] {msg}", flush=True)


def find_pdf_url() -> str:
    """Scrape the rate page for the current Japanese Yen TIBOR PDF link."""
    r = requests.get(RATE_PAGE, headers=UA, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    m = PDF_LINK_RE.search(r.text) or PDF_URL_RE.search(r.text)
    if not m:
        raise RuntimeError("Could not find a JAPANESEYENTIBOR PDF link on the rate page.")
    # Resolve relative or absolute hrefs against the page URL (handles both the
    # old /rate/pdf/... layout and the new root-level one).
    return urljoin(RATE_PAGE, m.group(1))


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


def parse_pdf_rows(path: str):
    """
    Return [(date, [(raw_tenor, pct_str), ...]), ...] — newest first — for EVERY
    populated row in the PDF. The file is named for a single day but holds the
    whole month to date, which is what feeds the 5-business-day chart.

    The table is borderless, so data numbers are aligned to their header label
    by x-coordinate rather than trusting pdfplumber's cell detection.
    """
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        words = page.extract_words(use_text_flow=False)

    # Header: tenor labels and their horizontal position (left edge).
    headers = [(w["text"], w["x0"]) for w in words if TENOR_RE.match(w["text"])]
    if not headers:
        raise RuntimeError("Could not find any tenor header labels (e.g. 1MONTH).")
    headers.sort(key=lambda h: h[1])

    date_words = [w for w in words if DATE_RE.match(w["text"])]
    if not date_words:
        raise RuntimeError("No dates found in PDF (nothing published yet?).")
    rate_words = [w for w in words if RATE_RE.match(w["text"])]

    rows = []
    for dw in date_words:
        # Rate numbers sitting on the same visual line as this date.
        line = [w for w in rate_words if abs(w["top"] - dw["top"]) < 3]
        if not line:
            continue
        vals = []
        for label, hx in headers:
            best = min(line, key=lambda w: abs(w["x0"] - hx))
            if abs(best["x0"] - hx) < 12:  # within a column width -> populated
                vals.append((label, best["text"]))
        d = normalize_ref_date(dw["text"])
        if d and vals:
            rows.append((d, vals))

    if not rows:
        raise RuntimeError("Could not align any rates to tenor columns.")

    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


def parse_pdf(path: str):
    """Latest published row only: (reference_date_str, [(pretty_tenor, pct)])."""
    d, vals = parse_pdf_rows(path)[0]
    return d.strftime("%Y/%m/%d"), [(_pretty_tenor(t), v) for t, v in vals]


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


def _tenor_sort_key(t: str):
    m = re.match(r"^(\d+)(WEEK|MONTH|YEAR)$", t)
    if not m:
        return (9, 0)
    return ({"WEEK": 0, "MONTH": 1, "YEAR": 2}[m.group(2)], int(m.group(1)))


def _short_tenor(t: str) -> str:
    """1WEEK -> 1W, 3MONTH -> 3M. Chart labels must be ASCII: the GitHub runner
    has no CJK font, so Japanese would render as tofu boxes."""
    m = re.match(r"^(\d+)(WEEK|MONTH|YEAR)$", t)
    return t if not m else m.group(1) + {"WEEK": "W", "MONTH": "M", "YEAR": "Y"}[m.group(2)]


def read_history(path: str):
    """{date: {raw_tenor: pct_str}} from the CSV, or {} if it isn't there yet."""
    hist = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    d = dt.date.fromisoformat((row.get("date") or "").strip())
                except ValueError:
                    continue
                hist[d] = {k: v for k, v in row.items()
                           if k != "date" and v not in (None, "")}
    except FileNotFoundError:
        pass
    return hist


def update_history(path: str, rows):
    """Merge parsed PDF rows into the CSV and return the full history.

    The PDF only covers the current month, so this file is what makes the
    5-business-day window survive a month boundary (on Sep 1 the PDF has one
    row, but August's rows are still here).
    """
    hist = read_history(path)
    for d, vals in rows:
        hist.setdefault(d, {}).update(dict(vals))
    tenors = sorted({t for v in hist.values() for t in v}, key=_tenor_sort_key)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date"] + tenors)
        for d in sorted(hist):
            w.writerow([d.isoformat()] + [hist[d].get(t, "") for t in tenors])
    return hist


def _nice_step(span: float) -> float:
    """A round tick step giving ~4-5 labelled lines across `span`."""
    target = (span / 4.0) if span > 0 else 1.0
    for s in (0.2, 0.5, 1.0, 2.0, 2.5, 5.0, 10.0, 20.0, 25.0, 50.0):
        if target <= s:
            return s
    return 100.0


def _split_bands(values, min_gap: float = 8.0):
    """Group values into bands, splitting wherever there is an empty stretch.

    TIBOR levels cluster (1W near 90, 1M near 117, the 3/6/12M pack near 150-166)
    while daily moves are only a few bps, so a single axis spends most of its
    height on empty space and every line looks flat. Splitting on the gaps lets
    each cluster be drawn at its own zoom.
    """
    vals = sorted(values)
    bands, cur = [], [vals[0]]
    for v in vals[1:]:
        if v - cur[-1] > min_gap:
            bands.append(cur)
            cur = [v]
        else:
            cur.append(v)
    bands.append(cur)
    return [(b[0], b[-1]) for b in bands]


def build_chart(hist, out_path: str, days: int = 5):
    """Line chart of the last `days` published days, in bps. Returns (path, md5).

    Only days actually present in the history are plotted, so weekends and
    holidays are skipped automatically — the x-axis is categorical, not a
    calendar, which also avoids flat gaps across a weekend.

    The y-axis is BROKEN across the empty stretches between rate clusters, so
    each cluster is zoomed to its own range and a 2bps move is a visible step
    rather than a flat line. Breaks are marked with the usual diagonal ticks.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MultipleLocator

    dates = sorted(hist)[-days:]
    if not dates:
        raise RuntimeError("No history to chart.")
    tenors = sorted({t for d in dates for t in hist[d]}, key=_tenor_sort_key)

    series = []          # (short_label, colour, [(x, y)…], legend_text)
    all_vals = []
    for i, t in enumerate(tenors):
        pts = [(j, float(hist[d][t]) * 100)
               for j, d in enumerate(dates) if hist[d].get(t)]
        if not pts:
            continue
        colour = SERIES_COLOURS[i % len(SERIES_COLOURS)]
        diff = pts[-1][1] - pts[0][1]
        sign = "+" if diff > 0 else ""
        series.append((_short_tenor(t), colour, pts,
                       f"{_short_tenor(t)} {pts[-1][1]:.2f} ({sign}{diff:.2f})"))
        all_vals += [p[1] for p in pts]

    if not series:
        raise RuntimeError("No series to chart.")

    # Highest band first, so the panels read top-to-bottom like one y-axis.
    bands = _split_bands(all_vals)[::-1]
    # Pad each band, and give a dead-flat one (span 0) a usable window.
    limits, spans = [], []
    for lo, hi in bands:
        span = hi - lo
        pad = max(span * 0.28, 0.8)
        limits.append((lo - pad, hi + pad))
        spans.append((hi + pad) - (lo - pad))

    # Panel heights follow each band's range, so every panel shares one bps-per-
    # pixel scale as far as possible — a 2bps move looks the same size anywhere.
    # A floor keeps a flat single-value band from collapsing to a sliver.
    ratios = [max(s, max(spans) * 0.22) for s in spans]

    # Teams scales card images to ~450px wide, so what matters is font size
    # RELATIVE to figsize, not pixel count (lesson from the Nikkei VI chart).
    # A LESS WIDE figure is what makes the picture bigger on screen.
    fig, axes = plt.subplots(len(bands), 1, figsize=(6.3, 6.2), dpi=180,
                             sharex=True, height_ratios=ratios,
                             gridspec_kw={"hspace": 0.10})
    if len(bands) == 1:
        axes = [axes]
    fig.patch.set_facecolor(SURFACE)

    x = list(range(len(dates)))
    for ax, (ylo, yhi) in zip(axes, limits):
        ax.set_facecolor(SURFACE)
        for label, colour, pts, _ in series:
            ax.plot([p[0] for p in pts], [p[1] for p in pts],
                    color=colour, marker="o", markersize=6.0, linewidth=2.2,
                    # Surface ring: 3M and 12M can sit ~2bps apart, so their
                    # markers overlap; the ring keeps them readable.
                    markeredgecolor=SURFACE, markeredgewidth=1.4,
                    solid_capstyle="round", label=label)
        ax.set_ylim(ylo, yhi)
        ax.set_xlim(-0.35, len(dates) - 0.65)

        step = _nice_step(yhi - ylo)
        ax.yaxis.set_major_locator(MultipleLocator(step))
        ax.yaxis.set_minor_locator(MultipleLocator(step / 5.0))
        # Solid hairlines, one shade off the surface — never dashed, never heavy.
        ax.grid(which="major", color=GRID, linewidth=0.9)
        ax.grid(which="minor", color=GRID, linewidth=0.6, alpha=0.45)
        ax.set_axisbelow(True)
        ax.tick_params(axis="both", labelsize=12.5, colors=INK_SOFT, length=0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.spines["left"].set_color(GRID)
        ax.spines["bottom"].set_color(GRID)

    # Diagonal break marks on each seam, so the axis is never read as continuous.
    brk = dict(marker=[(-1, -0.6), (1, 0.6)], markersize=7, linestyle="none",
               color=INK_SOFT, mec=INK_SOFT, mew=1.2, clip_on=False)
    for upper, lower in zip(axes[:-1], axes[1:]):
        upper.spines["bottom"].set_visible(False)
        upper.tick_params(axis="x", length=0)
        lower.spines["top"].set_visible(False)
        upper.plot([0, 1], [0, 0], transform=upper.transAxes, **brk)
        lower.plot([0, 1], [1, 1], transform=lower.transAxes, **brk)

    axes[0].set_title("JBA Japanese Yen TIBOR\nlast %d business days" % len(dates),
                      fontsize=16, pad=12, color=INK)
    axes[-1].set_xticks(x)
    axes[-1].set_xticklabels([d.strftime("%m/%d") for d in dates], fontsize=13)
    fig.supylabel("bps", fontsize=14, color=INK_SOFT, x=0.012)

    # Legend under the bottom panel. Labels carry the latest value and the change
    # over the window, so the exact bps move is readable as text as well.
    handles, _ = axes[-1].get_legend_handles_labels()
    leg = axes[-1].legend(handles, [s[3] for s in series], loc="upper center",
                          bbox_to_anchor=(0.5, -0.30), ncol=2, fontsize=12.5,
                          frameon=False, handlelength=1.6, columnspacing=1.2,
                          labelspacing=0.45)
    for txt in leg.get_texts():          # identity is the swatch, not the text
        txt.set_color(INK_SOFT)

    # Reserve the legend's space explicitly instead of tight_layout/bbox_inches,
    # so the axes keep the full width of the figure.
    fig.subplots_adjust(left=0.155, right=0.97, top=0.885, bottom=0.235)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, facecolor=SURFACE)
    plt.close(fig)

    with open(out_path, "rb") as f:
        digest = hashlib.md5(f.read()).hexdigest()[:10]
    return out_path, digest


def chart_url(digest: str, ref_date: str) -> str:
    """Public raw URL for the committed chart.

    Teams can only render card images from a PUBLIC url, which is why this repo
    is public. The query string must be busted on the CONTENT hash, not just the
    date — otherwise Teams and the raw CDN keep serving the previous picture
    when a chart is regenerated for the same day.
    """
    repo = os.environ.get("CHART_REPO", "ryotaweave/tibor-teams-bot")
    branch = os.environ.get("CHART_BRANCH", "master")
    stamp = ref_date.replace("/", "")
    return (f"https://raw.githubusercontent.com/{repo}/{branch}/{CHART_PATH}"
            f"?v={stamp}-{digest}")


def deltas_vs_previous(hist, ref: dt.date):
    """{raw_tenor: change_in_bps} for `ref` vs the previous published day."""
    days = sorted(d for d in hist if d <= ref)
    if len(days) < 2:
        return {}
    cur, prev = hist[days[-1]], hist[days[-2]]
    out = {}
    for t, v in cur.items():
        if t in prev:
            try:
                out[_pretty_tenor(t)] = (Decimal(v) - Decimal(prev[t])) * 100
            except Exception:
                pass
    return out


def build_card(ref_date: str, rates, pdf_url: str, is_today: bool,
               img_url: str = None, deltas: dict = None):
    deltas = deltas or {}

    def fact_value(label, val):
        text = f"{pct_to_bps(val)} bps"
        d = deltas.get(label)
        if d is None:
            return text
        if d > 0:
            return f"{text}　▲ +{d.normalize():f}"
        if d < 0:
            return f"{text}　▼ {d.normalize():f}"
        return f"{text}　→ 0"

    facts = [{"title": label, "value": fact_value(label, val)}
             for label, val in rates]
    subtitle = ("本日公表のレート" if is_today
                else "※本日は新規公表なし。直近公表分を表示しています。")

    body = [
        {"type": "TextBlock", "size": "Large", "weight": "Bolder",
         "text": "全銀協 日本円TIBOR（D-TIBOR）"},
        {"type": "TextBlock", "spacing": "None", "isSubtle": True,
         "wrap": True, "text": f"基準日: {ref_date}"},
        {"type": "TextBlock", "spacing": "Small", "isSubtle": True,
         "wrap": True, "text": subtitle + "（▲▼ は前営業日比 bps）"},
        {"type": "FactSet", "facts": facts},
    ]
    actions = [{"type": "Action.OpenUrl", "title": "元のPDFを開く", "url": pdf_url}]

    if img_url:
        body.append({
            "type": "Image", "url": img_url, "size": "Stretch",
            "altText": "過去5営業日のD-TIBOR推移",
            # Click-to-zoom: some Teams clients ignore an Image selectAction, so
            # the button below is kept as a fallback.
            "selectAction": {"type": "Action.OpenUrl", "url": img_url},
        })
        actions.append({"type": "Action.OpenUrl", "title": "グラフを拡大",
                        "url": img_url})

    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": body,
                "actions": actions,
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


META_PATH = os.path.join(os.environ.get("RUNNER_TEMP", "."), "tibor_meta.json")


def phase_prepare(always_post: bool, state_file: str) -> dict:
    """Fetch + parse + update history, and draw the chart when there is a new
    rate. Split from posting because the chart has to be committed and pushed
    BEFORE the card is sent, or Teams renders a broken image."""
    pdf_url = find_pdf_url()
    log(f"pdf url: {pdf_url}")
    tmp = os.path.join(os.environ.get("RUNNER_TEMP", "."), "tibor.pdf")
    download_pdf(pdf_url, tmp)

    rows = parse_pdf_rows(tmp)
    ref, vals = rows[0]
    ref_date = ref.strftime("%Y/%m/%d")
    rates = [(_pretty_tenor(t), v) for t, v in vals]
    log(f"reference date: {ref_date}  ({len(rows)} rows in this PDF)")
    for label, val in rates:
        log(f"  {label}: {pct_to_bps(val)} bps  ({val}%)")

    hist = update_history(HISTORY_PATH, rows)
    log(f"history: {len(hist)} published days -> {HISTORY_PATH}")

    # De-duplicate by the rate's own date, NOT by "is it today?". GitHub's cron
    # is best-effort and often runs hours late (sometimes past JST midnight), so
    # a strict "ref == today" check would wrongly skip a fresh rate. Instead we
    # remember the last date we posted and post any strictly newer one exactly
    # once — which also naturally skips weekends/holidays (no new date).
    last_posted = read_last_posted(state_file)
    log(f"last posted date: {last_posted}")
    is_new = bool(always_post or last_posted is None or ref > last_posted)

    meta = {
        "ref": ref.isoformat(), "ref_date": ref_date, "pdf_url": pdf_url,
        "rates": rates, "is_today": ref == dt.datetime.now(JST).date(),
        "is_new": is_new, "img_url": None, "deltas": {},
    }

    if is_new:
        _, digest = build_chart(hist, CHART_PATH, CHART_DAYS)
        meta["img_url"] = chart_url(digest, ref_date)
        meta["deltas"] = {k: str(v) for k, v in deltas_vs_previous(hist, ref).items()}
        log(f"chart: {CHART_PATH} (md5 {digest})")
    else:
        log(f"rate {ref} already posted (last was {last_posted}) — "
            f"chart not regenerated, nothing to publish.")

    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return meta


def phase_post(meta: dict, webhook: str, dry_run: bool, state_file: str) -> int:
    if not meta.get("is_new"):
        log("nothing new — exiting without posting.")
        return 0

    deltas = {k: Decimal(v) for k, v in (meta.get("deltas") or {}).items()}
    card = build_card(meta["ref_date"], meta["rates"], meta["pdf_url"],
                      meta["is_today"], img_url=meta.get("img_url"),
                      deltas=deltas)

    if dry_run:
        log("DRY RUN — not posting. Card payload:")
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return 0

    post_to_teams(webhook, card)
    write_last_posted(state_file, dt.date.fromisoformat(meta["ref"]))
    log(f"recorded last-posted date {meta['ref']} -> {state_file}")
    return 0


def main() -> int:
    webhook = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    always_post = os.environ.get("ALWAYS_POST", "").strip() in ("1", "true", "True")
    dry_run = "--dry-run" in sys.argv or not webhook
    state_file = os.environ.get("STATE_FILE", "state/last_posted.txt")

    do_prepare = "--prepare" in sys.argv
    do_post = "--post" in sys.argv
    if not do_prepare and not do_post:      # local/manual run: both phases
        do_prepare = do_post = True

    meta = phase_prepare(always_post, state_file) if do_prepare else None
    if not do_post:
        return 0
    if meta is None:
        with open(META_PATH, encoding="utf-8") as f:
            meta = json.load(f)
    return phase_post(meta, webhook, dry_run, state_file)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
