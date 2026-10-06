"""
EV Game Line Scanner - Pinnacle no-vig baseline vs Kansas books.

Changes from v1:
  * America/Chicago timezone (DST-safe) for the slate window and all timestamps
  * ESPN status check: any game ESPN reports as 'in' or 'post' is skipped
  * Stale-Pinnacle guard: skip a market when the soft book updated more than
    MAX_PINNY_LAG_MIN minutes after Pinnacle did
  * NHL puck lines other than +/-1.5 are rejected (those are live lines)
  * MIN_EDGE_PCT floor and MAX_EDGE_PCT "suspect" ceiling
  * One play per game "side" (moneyline OR spread, never both) plus one total:
    best edge across books, and never the opposite side of anything already logged
  * Stores Sport + Event ID so the grader matches by ID, not by name
  * SPORT_MIN_EDGE: per-sport edge floors or 'off'
  * CLV: every run refreshes 'Close Prob %' / 'CLV %' on pending plays from the
    latest pregame Pinnacle no-vig price, so the last pregame run = closing line
"""
import os
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

from ev_common import (
    CT, ESPN_LEAGUES, FIELDNAMES, MARKET_GROUP, SPORTS_CONFIG, UNIT_SIZE,
    american_to_decimal, american_to_prob, is_separator, make_separator,
    now_ct, parse_iso, parse_odds, read_log, write_log,
)

API_KEY = os.environ.get("ODDS_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

KS_BOOKS = "fanduel,draftkings,betmgm,caesars,espnbet,novig"
ALLOWED_BOOKS = set(KS_BOOKS.split(","))

MIN_EDGE_PCT = float(os.environ.get("MIN_EDGE_PCT", "1.0"))
MAX_EDGE_PCT = float(os.environ.get("MAX_EDGE_PCT", "15.0"))
MAX_PINNY_LAG_MIN = float(os.environ.get("MAX_PINNY_LAG_MIN", "10"))
MIN_ODDS = int(os.environ.get("MIN_ODDS", "-150"))
MAX_ODDS = int(os.environ.get("MAX_ODDS", "200"))
KELLY_FRACTION = float(os.environ.get("KELLY_FRACTION", "0.25"))
# 1 = skip any game ESPN can't find. 0 = allow it (commence_time still enforced).
REQUIRE_ESPN_PREGAME = os.environ.get("REQUIRE_ESPN_PREGAME", "0") == "1"


def parse_sport_min_edge(raw):
    """'americanfootball_ncaaf=2.5,icehockey_nhl_preseason=off' -> {sport: float|None}"""
    out = {}
    for part in (raw or "").split(","):
        if "=" not in part:
            continue
        key, val = (x.strip() for x in part.split("=", 1))
        if key not in SPORTS_CONFIG:
            print(f"WARNING: SPORT_MIN_EDGE has unknown sport '{key}', ignored")
            continue
        out[key] = None if val.lower() == "off" else float(val)
    return out


# Per-sport overrides of MIN_EDGE_PCT; 'off' stops scanning that sport (saves credits).
SPORT_MIN_EDGE = parse_sport_min_edge(os.environ.get("SPORT_MIN_EDGE", ""))

MARKET_DISPLAY = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}
DISPLAY_TO_KEY = {v: k for k, v in MARKET_DISPLAY.items()}


# ---------- ESPN pregame check ----------
IGNORE_TOKENS = {"new", "york", "los", "angeles", "las", "vegas", "san", "bay",
                 "city", "state", "university", "st", "the"}


def normalize_name(name):
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", str(name)).encode("ASCII", "ignore").decode("utf-8")
    name = name.lower()
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", name)
    name = re.sub(r"[^a-z\s]", "", name)
    return " ".join(name.split())


def teams_match(a, b):
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return bool((set(na.split()) - IGNORE_TOKENS) & (set(nb.split()) - IGNORE_TOKENS))


_espn_cache = {}


def espn_slate(sport_key, date_ct):
    league = ESPN_LEAGUES.get(sport_key)
    if not league:
        return None
    cache_key = (league, date_ct)
    if cache_key in _espn_cache:
        return _espn_cache[cache_key]

    sport, lg = league
    params = {"dates": date_ct.strftime("%Y%m%d"), "limit": 500}
    if lg == "college-football":
        params["groups"] = "80"  # all FBS, not just featured games
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/scoreboard"

    slate = None
    try:
        res = requests.get(url, params=params, timeout=10)
        if res.status_code == 200:
            slate = []
            for ev in res.json().get("events", []):
                try:
                    comps = ev["competitions"][0]["competitors"]
                    home = next(c for c in comps if c["homeAway"] == "home")["team"]["displayName"]
                    away = next(c for c in comps if c["homeAway"] == "away")["team"]["displayName"]
                    slate.append({
                        "home": home, "away": away,
                        "start": parse_iso(ev["date"]),
                        "state": ev["status"]["type"]["state"],
                    })
                except (KeyError, StopIteration, ValueError, TypeError):
                    continue
        else:
            print(f"  ESPN {lg} returned {res.status_code}; pregame check unavailable")
    except requests.RequestException as e:
        print(f"  ESPN {lg} error: {e}; pregame check unavailable")

    _espn_cache[cache_key] = slate
    return slate


def espn_state(slate, away, home, commence):
    """'pre' / 'in' / 'post', or None if ESPN has no matching game."""
    if not slate:
        return None
    for g in slate:
        if abs((g["start"] - commence).total_seconds()) > 6 * 3600:
            continue
        if teams_match(away, g["away"]) and teams_match(home, g["home"]):
            return g["state"]
    return None


# ---------- Pinnacle ----------
def pinnacle_fair(event):
    """m_key -> {'probs': {(name, point): no-vig prob}, 'last_update': datetime|None}"""
    fair = {}
    for book in event.get("bookmakers", []):
        if book.get("key") != "pinnacle":
            continue
        for market in book.get("markets", []):
            outs = market.get("outcomes", [])
            if len(outs) != 2:
                continue
            p1 = american_to_prob(outs[0]["price"])
            p2 = american_to_prob(outs[1]["price"])
            total = p1 + p2
            lu = market.get("last_update") or book.get("last_update")
            fair[market["key"]] = {
                "probs": {
                    (outs[0]["name"], outs[0].get("point")): p1 / total,
                    (outs[1]["name"], outs[1].get("point")): p2 / total,
                },
                "last_update": parse_iso(lu) if lu else None,
            }
    return fair


def outcome_key_for_row(row):
    """Map a logged row back to (market key, (outcome name, point))."""
    m_key = DISPLAY_TO_KEY.get(row["Market"])
    if m_key == "h2h":
        return m_key, (row["Player"], None)
    try:
        line = float(row["Line"])
    except ValueError:
        return None, None
    if m_key == "spreads":
        return m_key, (row["Player"], line)
    if m_key == "totals":
        return m_key, (row["Side"], line)
    return None, None


def update_clv(rows, fair, run_ct):
    updated = 0
    seen_stamp = run_ct.strftime("%Y-%m-%d %H:%M")
    for row in rows:
        m_key, key = outcome_key_for_row(row)
        if not m_key:
            continue
        prob = fair.get(m_key, {}).get("probs", {}).get(key)
        if prob is None:
            continue  # Pinnacle moved off this number; keep last known value
        clv = (prob * american_to_decimal(parse_odds(row["Odds"])) - 1) * 100
        new_close, new_clv = f"{prob * 100:.1f}", f"{clv:.2f}"
        row["Close Prob %"], row["CLV %"], row["Close Seen CT"] = new_close, new_clv, seen_stamp
        updated += 1
    return updated


# ---------- play building ----------
def build_play(sport_key, event, m_key, cand, run_ct, commence):
    if m_key == "h2h":
        player, side, line = cand["name"], "ML", "---"
    elif m_key == "spreads":
        player, side, line = cand["name"], "Spread", str(cand["point"])
    else:
        player, side, line = "Game Total", cand["name"], str(cand["point"])

    p = cand["true_prob"]
    b = american_to_decimal(cand["odds"]) - 1
    kelly = (p * b - (1 - p)) / b
    units = kelly * 100 * KELLY_FRACTION
    odds = cand["odds"]

    row = {f: "" for f in FIELDNAMES}
    row.update({
        "Timestamp": run_ct.strftime("%Y-%m-%d %H:%M:%S"),
        "Sport": sport_key,
        "Event ID": event["id"],
        "Commence CT": commence.astimezone(CT).strftime("%Y-%m-%d %H:%M"),
        "Game": f"{event['away_team']} @ {event['home_team']}",
        "Market": MARKET_DISPLAY[m_key],
        "Player": player,
        "Side": side,
        "Line": line,
        "Bookmaker": cand["book"],
        "Odds": f"+{odds}" if odds > 0 else str(odds),
        "True Prob %": f"{p * 100:.1f}",
        "Edge %": f"{cand['edge'] * 100:.2f}",
        "Kelly Units": f"{units:.2f}",
        "Bet Amount": f"{units * UNIT_SIZE:.2f}",
        "Close Prob %": f"{p * 100:.1f}",
        "CLV %": f"{cand['edge'] * 100:.2f}",
        "Close Seen CT": run_ct.strftime("%Y-%m-%d %H:%M"),
        "Result": "PENDING",
        "Net Units": "0.00",
    })
    return row


# ---------- Discord ----------
def send_discord_digest(new_plays, run_label):
    if not DISCORD_WEBHOOK_URL or not new_plays:
        return
    plays = sorted(new_plays, key=lambda r: float(r["Edge %"]), reverse=True)
    chunk_size = 15
    total_chunks = (len(plays) + chunk_size - 1) // chunk_size

    for idx in range(0, len(plays), chunk_size):
        lines = []
        for r in plays[idx:idx + chunk_size]:
            edge = float(r["Edge %"])
            icon = "🔥" if edge >= 5.0 else ("💎" if edge >= 2.0 else "▫️")
            line_txt = "" if r["Line"] == "---" else f" {r['Line']}"
            lines.append(
                f"{icon} **+{r['Edge %']}%** | **{r['Player']}** {r['Side']}{line_txt}\n"
                f"↳ **{r['Odds']}** @ {r['Bookmaker']} • **{r['Kelly Units']}u** "
                f"(${r['Bet Amount']}) • *{r['Game']}* • {r['Commence CT']} CT"
            )
        part = f" (Part {idx // chunk_size + 1}/{total_chunks})" if total_chunks > 1 else ""
        embed = {
            "title": f"🚨 +EV Game Line Digest ({len(plays)} Plays){part}",
            "description": "\n\n".join(lines),
            "color": 16753920,
            "footer": {"text": f"Scanned {run_label} CT • Pinnacle no-vig • min edge {MIN_EDGE_PCT}%"},
        }
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        except requests.RequestException as e:
            print(f"Error sending Discord digest: {e}")


# ---------- main ----------
def fetch_and_scan():
    if not API_KEY:
        print("CRITICAL ERROR: ODDS_API_KEY missing.")
        return

    run_ct = now_ct()
    run_label = run_ct.strftime("%Y-%m-%d %H:%M:%S")
    now_utc = datetime.now(timezone.utc)
    day_start = run_ct.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    print(f"--- EV Game Line Scanner ({run_label} CT) | min edge {MIN_EDGE_PCT}% ---")

    rows = read_log()

    # Dedup: one play per (event, group), where ML + spread share the "side"
    # group. Legacy rows (no Event ID) fall back to (game, group) for 2 days.
    seen = set()
    legacy_seen = set()
    pending_by_event = defaultdict(list)
    legacy_cutoff = (run_ct - timedelta(days=2)).replace(tzinfo=None)
    for r in rows:
        if is_separator(r):
            continue
        if r["Event ID"]:
            seen.add((r["Event ID"], MARKET_GROUP.get(r["Market"], r["Market"].lower())))
            if r["Result"] == "PENDING":
                pending_by_event[r["Event ID"]].append(r)
        else:
            try:
                ts = datetime.strptime(r["Timestamp"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                ts = legacy_cutoff
            if ts >= legacy_cutoff:
                legacy_seen.add((r["Game"].strip().lower(),
                                 MARKET_GROUP.get(r["Market"].strip(), r["Market"].strip().lower())))

    new_plays = []
    clv_updates = 0
    counts = defaultdict(int)

    for sport_key, markets in SPORTS_CONFIG.items():
        min_edge = SPORT_MIN_EDGE.get(sport_key, MIN_EDGE_PCT)
        if min_edge is None:
            print(f"\n{sport_key}: disabled via SPORT_MIN_EDGE")
            continue
        params = {
            "apiKey": API_KEY,
            "bookmakers": f"pinnacle,{KS_BOOKS}",
            "markets": markets,
            "oddsFormat": "american",
        }
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
        try:
            res = requests.get(url, params=params, timeout=15)
        except requests.RequestException as e:
            print(f"{sport_key}: network error {e}")
            continue
        if res.status_code != 200:
            print(f"{sport_key}: API error {res.status_code} {res.text[:200]}")
            continue
        remaining = res.headers.get("x-requests-remaining")
        events = res.json()
        print(f"\n{sport_key}: {len(events)} events, min edge {min_edge}% (credits left: {remaining})")

        for event in events:
            try:
                commence = parse_iso(event["commence_time"])
            except (KeyError, ValueError):
                continue
            if not (day_start <= commence.astimezone(CT) < day_end):
                continue
            if commence <= now_utc:
                continue

            game = f"{event['away_team']} @ {event['home_team']}"
            state = espn_state(espn_slate(sport_key, day_start.date()),
                               event["away_team"], event["home_team"], commence)
            if state in ("in", "post"):
                print(f"  SKIP {game}: ESPN says '{state}' (API commence {commence:%H:%M} UTC)")
                counts["espn_live"] += 1
                continue
            if state is None and REQUIRE_ESPN_PREGAME:
                print(f"  SKIP {game}: not found on ESPN")
                counts["espn_missing"] += 1
                continue

            fair = pinnacle_fair(event)
            if not fair:
                continue

            # Refresh closing-line value for plays already logged on this game
            clv_updates += update_clv(pending_by_event.get(event["id"], []), fair, run_ct)

            best = {}
            for book in event.get("bookmakers", []):
                if book.get("key") not in ALLOWED_BOOKS:
                    continue
                for market in book.get("markets", []):
                    m_key = market["key"]
                    if m_key not in fair:
                        continue

                    pin_lu = fair[m_key]["last_update"]
                    book_lu = market.get("last_update") or book.get("last_update")
                    if pin_lu and book_lu and parse_iso(book_lu) - pin_lu > timedelta(minutes=MAX_PINNY_LAG_MIN):
                        counts["stale_pinny"] += 1
                        continue

                    for o in market.get("outcomes", []):
                        name, pt, price = o["name"], o.get("point"), o["price"]
                        if price < MIN_ODDS or price > MAX_ODDS:
                            continue
                        if (sport_key.startswith("icehockey") and m_key == "spreads"
                                and pt is not None and abs(float(pt)) != 1.5):
                            counts["bad_puckline"] += 1
                            continue
                        true_prob = fair[m_key]["probs"].get((name, pt))
                        if true_prob is None:
                            continue
                        edge = true_prob * american_to_decimal(price) - 1
                        edge_pct = edge * 100
                        if edge_pct < min_edge:
                            continue
                        if edge_pct > MAX_EDGE_PCT:
                            print(f"  SUSPECT {game} {name} {pt} {price} @ {book['title']}: "
                                  f"{edge_pct:.1f}% edge, not logged")
                            counts["suspect"] += 1
                            continue
                        group = MARKET_GROUP[MARKET_DISPLAY[m_key]]
                        if group not in best or edge > best[group]["edge"]:
                            best[group] = {"m_key": m_key, "name": name, "point": pt,
                                           "odds": price, "book": book["title"],
                                           "true_prob": true_prob, "edge": edge}

            for group, cand in best.items():
                if (event["id"], group) in seen or (game.lower(), group) in legacy_seen:
                    continue
                play = build_play(sport_key, event, cand["m_key"], cand, run_ct, commence)
                seen.add((event["id"], group))
                new_plays.append(play)
                print(f"  + {play['Edge %']}% {play['Player']} {play['Side']} {play['Line']} "
                      f"{play['Odds']} @ {play['Bookmaker']}")

    if new_plays:
        if any(not is_separator(r) for r in rows):
            rows.append(make_separator(f"=== GAMELINE RUN: {run_label} CT ({len(new_plays)} PLAYS FOUND) ==="))
        rows.extend(new_plays)
    if new_plays or clv_updates:
        write_log(rows)
    send_discord_digest(new_plays, run_label)

    print(f"\nDone. {len(new_plays)} new plays, {clv_updates} CLV updates. "
          f"Skipped: {dict(counts) if counts else 'none'}")


if __name__ == "__main__":
    fetch_and_scan()
