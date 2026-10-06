"""
EV Game Line Auto-Grader.

Changes from v1:
  * Grades from The Odds API /scores endpoint by Event ID - no fuzzy name
    matching, no cross-sport collisions (Flyers vs Eagles), no repeat-matchup
    mixups. Costs 2 credits per sport per run (daysFrom=3), and only sports
    with pending plays are queried.
  * Legacy rows (no Event ID) are matched on EXACT Odds API team names plus a
    time window, then back-filled with Sport + Event ID.
  * NHL shootouts: if a final comes back tied, the total is graded as +1 goal
    and +/-1.5 puck lines are still graded; the moneyline is left PENDING and
    printed for manual review.
  * Rows with Flag = EXCLUDE are graded but left out of all stats.
  * Digest shows ROI and average CLV per edge bucket.
"""
import os
from datetime import datetime, timedelta, timezone

import requests

from ev_common import (
    BUCKET_LABELS, CLOSE_WINDOW_MIN, SPORTS_CONFIG, UNIT_SIZE, american_to_decimal,
    closing_clv, edge_bucket,
    is_separator, parse_iso, parse_odds, read_log, write_log,
)

API_KEY = os.environ.get("ODDS_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")


def fetch_scores(sport_key):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores"
    try:
        res = requests.get(url, params={"apiKey": API_KEY, "daysFrom": 3}, timeout=15)
    except requests.RequestException as e:
        print(f"{sport_key}: network error {e}")
        return []
    if res.status_code != 200:
        print(f"{sport_key}: API error {res.status_code} {res.text[:200]}")
        return []
    print(f"{sport_key}: scores loaded (credits left: {res.headers.get('x-requests-remaining')})")
    return res.json()


def final_scores(ev):
    if not ev.get("completed") or not ev.get("scores"):
        return None
    try:
        s = {x["name"]: float(x["score"]) for x in ev["scores"]}
    except (KeyError, TypeError, ValueError):
        return None
    away, home = s.get(ev["away_team"]), s.get(ev["home_team"])
    if away is None or home is None:
        return None
    return away, home


def _result(x):
    return "WIN" if x > 0 else ("LOSS" if x < 0 else "PUSH")


def grade_row(row, ev, sport_key):
    """Return (result, score_text) or None if it can't be graded automatically."""
    fs = final_scores(ev)
    if not fs:
        return None
    away, home = fs
    score_txt = f"{int(away)}-{int(home)}"
    shootout = sport_key.startswith("icehockey") and away == home

    if row["Market"] == "Total":
        line = float(row["Line"])
        total = away + home + (1 if shootout else 0)
        diff = total - line if row["Side"].lower() == "over" else line - total
        return _result(diff), f"{score_txt} (total {total:g})"

    team = row["Player"]
    if team == ev["home_team"]:
        margin = home - away
    elif team == ev["away_team"]:
        margin = away - home
    else:
        return None

    line = 0.0 if row["Market"] == "Moneyline" else float(row["Line"])
    margins = [1, -1] if shootout else [margin]  # shootout winner unknown
    results = {_result(m + line) for m in margins}
    if len(results) != 1:
        return None
    return results.pop(), score_txt


def find_legacy_event(row, all_events):
    """Exact-name match for rows logged before Event IDs existed."""
    try:
        away, home = row["Game"].split(" @ ")
        ts = datetime.strptime(row["Timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    cands = []
    for sk, ev in all_events:
        if ev.get("away_team") != away or ev.get("home_team") != home:
            continue
        try:
            start = parse_iso(ev["commence_time"])
        except (KeyError, ValueError):
            continue
        if ts - timedelta(hours=12) <= start <= ts + timedelta(hours=48):
            cands.append((start, sk, ev))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    return cands[0][1], cands[0][2]


def new_stats():
    return [{"W": 0, "L": 0, "P": 0, "units": 0.0, "risked": 0.0, "clv": 0.0, "clv_n": 0}
            for _ in range(3)]


def add_to_stats(stats, row):
    try:
        b = stats[edge_bucket(float(row["Edge %"] or 0))]
    except ValueError:
        return
    if row["Result"] in ("WIN", "LOSS", "PUSH"):
        b[row["Result"][0]] += 1
        b["units"] += float(row["Net Units"] or 0)
        b["risked"] += float(row["Kelly Units"] or 0)
    clv = closing_clv(row)
    if clv is not None:
        b["clv"] += clv
        b["clv_n"] += 1


def build_digest(batch, lifetime, graded_count, excluded):
    lines = []
    batch_total = life_total = 0.0
    for i, label in enumerate(BUCKET_LABELS):
        bt, lt = batch[i], lifetime[i]
        batch_total += bt["units"]
        life_total += lt["units"]
        decided = lt["W"] + lt["L"]
        pct = lt["W"] / decided * 100 if decided else 0.0
        roi = lt["units"] / lt["risked"] * 100 if lt["risked"] else 0.0
        clv = f"{lt['clv'] / lt['clv_n']:+.2f}% (n={lt['clv_n']})" if lt["clv_n"] else "n/a"
        lines.append(label)
        lines.append(f"**Batch:** {bt['W']}-{bt['L']}-{bt['P']} | {bt['units']:+.2f}u")
        lines.append(f"**Lifetime:** {lt['W']}-{lt['L']}-{lt['P']} ({pct:.1f}%) | "
                     f"{lt['units']:+.2f}u | ROI {roi:+.1f}%")
        lines.append(f"**Avg CLV (≤{CLOSE_WINDOW_MIN:g}m pre-start):** {clv}\n")
    lines.append(f"💰 **Batch Profit:** {batch_total:+.2f}u (${batch_total * UNIT_SIZE:+.2f})")
    lines.append(f"🏦 **Lifetime Profit:** {life_total:+.2f}u (${life_total * UNIT_SIZE:+.2f})")
    if excluded:
        lines.append(f"🚫 {excluded} flagged plays excluded from stats")
    title = f"📊 EV Gameline Auto-Grader ({graded_count} New Settlements)"
    return title, "\n".join(lines)


def send_digest(title, body):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL,
                      json={"embeds": [{"title": title, "description": body, "color": 16753920}]},
                      timeout=10)
    except requests.RequestException as e:
        print(f"Error sending Discord digest: {e}")


def run_grader():
    if not API_KEY:
        print("CRITICAL ERROR: ODDS_API_KEY missing.")
        return
    rows = read_log()
    if not rows:
        print("No CSV found to grade.")
        return

    pending = [r for r in rows if not is_separator(r) and r["Result"] == "PENDING"]
    if not pending:
        print("No pending plays.")
        return

    sports = {r["Sport"] for r in pending if r["Sport"]}
    if any(not r["Event ID"] for r in pending):
        sports |= set(SPORTS_CONFIG)

    by_id, all_events = {}, []
    for sk in sorted(sports):
        for ev in fetch_scores(sk):
            by_id[ev["id"]] = (sk, ev)
            all_events.append((sk, ev))

    batch = new_stats()
    graded = 0
    for r in pending:
        if r["Event ID"]:
            match = by_id.get(r["Event ID"])
        else:
            match = find_legacy_event(r, all_events)
        if not match:
            continue
        sk, ev = match
        if not ev.get("completed"):
            continue

        g = grade_row(r, ev, sk)
        if g is None:
            print(f"MANUAL REVIEW: {r['Game']} | {r['Player']} {r['Side']} {r['Line']} "
                  f"(scores: {ev.get('scores')})")
            continue

        res, score_txt = g
        units = float(r["Kelly Units"] or 0)
        if res == "WIN":
            net = units * (american_to_decimal(parse_odds(r["Odds"])) - 1)
        elif res == "LOSS":
            net = -units
        else:
            net = 0.0
        r["Result"], r["Net Units"] = res, f"{net:.2f}"
        if not r["Event ID"]:
            r["Sport"], r["Event ID"] = sk, ev["id"]
        graded += 1
        print(f"Graded: {r['Game']} | {r['Player']} {r['Side']} {r['Line']} -> {score_txt} ({res})")
        if r["Flag"].strip().upper() != "EXCLUDE":
            add_to_stats(batch, r)

    if graded == 0:
        print("No pending plays were ready to grade.")
        return

    write_log(rows)

    lifetime = new_stats()
    excluded = 0
    for r in rows:
        if is_separator(r):
            continue
        if r["Flag"].strip().upper() == "EXCLUDE":
            excluded += 1
            continue
        add_to_stats(lifetime, r)

    title, body = build_digest(batch, lifetime, graded, excluded)
    print(f"\n{title}\n{body}")
    send_digest(title, body)


if __name__ == "__main__":
    run_grader()
