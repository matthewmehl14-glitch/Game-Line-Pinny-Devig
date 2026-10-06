"""
Shared helpers for gameline_scanner.py and gameline_grader.py.

- One CSV schema (FIELDNAMES) used by both scripts.
- read_log() migrates any older layout (12- or 14-column rows, mixed widths)
  into the current schema by column NAME, so legacy rows are preserved.
- write_log() rewrites atomically (temp file + os.replace).
"""
import csv
import os
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
CSV_FILENAME = os.environ.get("EV_CSV", "ev_plays_log.csv")
UNIT_SIZE = float(os.environ.get("UNIT_SIZE", "25"))

# Odds API sport key -> markets
SPORTS_CONFIG = {
    "basketball_wnba": "h2h,spreads,totals",
    "basketball_nba": "h2h,spreads,totals",
    "basketball_nba_preseason": "h2h,spreads,totals",
    "icehockey_nhl": "h2h,spreads,totals",
    "icehockey_nhl_preseason": "h2h,spreads,totals",
    "americanfootball_nfl": "h2h,spreads,totals",
    "americanfootball_ncaaf": "h2h,spreads,totals",
}

# Odds API sport key -> ESPN (sport, league) for the pregame status check
ESPN_LEAGUES = {
    "basketball_wnba": ("basketball", "wnba"),
    "basketball_nba": ("basketball", "nba"),
    "basketball_nba_preseason": ("basketball", "nba"),
    "icehockey_nhl": ("hockey", "nhl"),
    "icehockey_nhl_preseason": ("hockey", "nhl"),
    "americanfootball_nfl": ("football", "nfl"),
    "americanfootball_ncaaf": ("football", "college-football"),
}

FIELDNAMES = [
    "Timestamp", "Sport", "Event ID", "Commence CT", "Game", "Market", "Player",
    "Side", "Line", "Bookmaker", "Odds", "True Prob %", "Edge %", "Kelly Units",
    "Bet Amount", "Close Prob %", "CLV %", "Close Seen CT", "Result", "Net Units", "Flag",
]

# Moneyline and spread are the same "side" of a game: only one gets logged.
MARKET_GROUP = {"Moneyline": "side", "Spread": "side", "Total": "total"}

# A CLV value only counts as "closing" if Pinnacle was last read within this
# many minutes of the start time. Older reads are excluded from CLV stats.
CLOSE_WINDOW_MIN = float(os.environ.get("CLOSE_WINDOW_MIN", "60"))

SEP = "---"
BUCKET_LABELS = ["1️⃣ **Under 2% Edge**", "2️⃣ **2.0% to 4.99% Edge**", "3️⃣ **5.0%+ Edge**"]


# ---------- odds math ----------
def american_to_prob(odds):
    odds = float(odds)
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)


def american_to_decimal(odds):
    odds = float(odds)
    if odds > 0:
        return odds / 100 + 1
    return 100 / abs(odds) + 1


def parse_odds(s):
    return float(str(s).replace("+", "").strip())


def closing_clv(row, window_min=None):
    """Return the row's CLV % if it was captured near the close, else None."""
    window = CLOSE_WINDOW_MIN if window_min is None else window_min
    if not row.get("CLV %") or not row.get("Close Seen CT") or not row.get("Commence CT"):
        return None
    try:
        seen = datetime.strptime(row["Close Seen CT"], "%Y-%m-%d %H:%M")
        start = datetime.strptime(row["Commence CT"], "%Y-%m-%d %H:%M")
        clv = float(row["CLV %"])
    except ValueError:
        return None
    minutes_before = (start - seen).total_seconds() / 60
    return clv if 0 <= minutes_before <= window else None


def edge_bucket(edge_pct):
    return 0 if edge_pct < 2.0 else (1 if edge_pct < 5.0 else 2)


# ---------- time ----------
def parse_iso(s):
    """Parse '2026-10-05T23:00:00Z' / '2026-10-05T23:00Z' into an aware UTC datetime."""
    dt = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_ct():
    return datetime.now(CT)


# ---------- CSV ----------
def is_separator(row):
    return row.get("Timestamp", "") == SEP or str(row.get("Game", "")).startswith("===")


def make_separator(label):
    row = {f: SEP for f in FIELDNAMES}
    row["Game"] = label
    return row


def read_log():
    """Read the log and normalise every row to FIELDNAMES (handles legacy layouts)."""
    if not os.path.isfile(CSV_FILENAME) or os.path.getsize(CSV_FILENAME) == 0:
        return []
    with open(CSV_FILENAME, "r", newline="", encoding="utf-8") as f:
        raw = list(csv.reader(f))
    if not raw:
        return []

    header = [h.strip() for h in raw[0]]
    rows = []
    for vals in raw[1:]:
        if not any(v.strip() for v in vals):
            continue
        d = {h: (vals[i] if i < len(vals) else "") for i, h in enumerate(header)}
        if d.get("Timestamp", "") == SEP or str(d.get("Game", "")).startswith("==="):
            rows.append(make_separator(d.get("Game", "")))
            continue
        row = {f: (d.get(f) or "") for f in FIELDNAMES}
        if not row["Result"]:
            row["Result"] = "PENDING"
        if not row["Net Units"]:
            row["Net Units"] = "0.00"
        rows.append(row)
    return rows


def write_log(rows):
    target_dir = os.path.dirname(os.path.abspath(CSV_FILENAME))
    fd, tmp = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, CSV_FILENAME)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
