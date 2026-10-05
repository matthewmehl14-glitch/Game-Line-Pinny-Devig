import os
import csv
import requests
from datetime import datetime, timedelta, timezone

API_KEY = os.environ.get('ODDS_API_KEY')
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')
UNIT_SIZE = 25.00

# Approved Kansas books to bet on
KS_BOOKS = 'fanduel,draftkings,betmgm,caesars,espnbet,novig'
ALLOWED_BOOKS = set(KS_BOOKS.split(','))
CSV_FILENAME = 'ev_plays_log.csv'

# Game Line Markets
SPORTS_CONFIG = {
    'basketball_wnba': 'h2h,spreads,totals',
    'basketball_nba': 'h2h,spreads,totals',
    'basketball_nba_preseason': 'h2h,spreads,totals',
    'icehockey_nhl': 'h2h,spreads,totals',
    'icehockey_nhl_preseason': 'h2h,spreads,totals',
    'americanfootball_nfl': 'h2h,spreads,totals',
    'americanfootball_ncaaf': 'h2h,spreads,totals'
}

def american_to_prob(odds):
    if odds < 0: return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)

def american_to_decimal(odds):
    if odds > 0: return (odds / 100) + 1
    return (100 / abs(odds)) + 1

def load_seen_plays():
    seen = set()
    if not os.path.isfile(CSV_FILENAME): return seen
    with open(CSV_FILENAME, mode='r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            player = row.get('Player', '')
            game = row.get('Game', '')
            if not player or player.startswith('---') or game.startswith('==='):
                continue
                
            key = (
                game.strip().lower(),
                row.get('Market', '').strip().lower(),
                player.strip().lower(),
                row.get('Side', '').strip().lower(),
                str(row.get('Line', '')).strip()
            )
            seen.add(key)
    return seen

def log_batch_to_csv(new_plays, run_timestamp):
    file_exists = os.path.isfile(CSV_FILENAME)
    is_empty = not file_exists or os.path.getsize(CSV_FILENAME) == 0

    with open(CSV_FILENAME, mode='a', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        
        if is_empty:
            writer.writerow(['Timestamp', 'Game', 'Market', 'Player', 'Side', 'Line', 'Bookmaker', 'Odds', 'True Prob %', 'Edge %', 'Kelly Units', 'Bet Amount'])
        else:
            writer.writerow([
                '---',
                f'=== GAMELINE RUN: {run_timestamp} ({len(new_plays)} PLAYS FOUND) ===',
                '---', '---', '---', '---', '---', '---', '---', '---', '---', '---'
            ])

        for play in new_plays:
            writer.writerow([
                play['timestamp'], play['game'], play['market'], 
                play['player'], play['side'], play['line'], 
                play['book'], play['odds'], play['true_prob'], 
                play['edge'], play['units'], play['wager']
            ])

def send_discord_digest(new_plays, run_timestamp):
    if not DISCORD_WEBHOOK_URL or not new_plays:
        return

    sorted_plays = sorted(new_plays, key=lambda x: float(x['edge']), reverse=True)

    chunk_size = 15
    for chunk_idx in range(0, len(sorted_plays), chunk_size):
        chunk = sorted_plays[chunk_idx:chunk_idx + chunk_size]
        
        lines = []
        for play in chunk:
            edge_val = float(play['edge'])
            icon = "🔥" if edge_val >= 5.0 else ("💎" if edge_val >= 2.0 else "▫️")
            
            line_1 = f"{icon} **+{play['edge']}%** | **{play['player']}** {play['side']} {play['line']}"
            line_2 = f"↳ **{play['odds']}** @ {play['book']} • **{play['units']}u** (${play['wager']}) • *{play['game']}*"
            lines.append(f"{line_1}\n{line_2}")

        total_chunks = (len(sorted_plays) + chunk_size - 1) // chunk_size
        part_tag = f" (Part {chunk_idx // chunk_size + 1}/{total_chunks})" if total_chunks > 1 else ""

        embed = {
            "title": f"🚨 +EV Game Line Digest ({len(sorted_plays)} Plays Found){part_tag}",
            "description": "\n\n".join(lines),
            "color": 16753920,
            "footer": {"text": f"Scanned at {run_timestamp} CT • Pinnacle Baseline"}
        }

        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        except Exception as e:
            print(f"Error sending Discord digest: {e}")

def fetch_and_scan():
    if not API_KEY:
        print("CRITICAL ERROR: API Key missing.")
        return
        
    seen_plays = load_seen_plays()
    new_plays_to_log = []
    edges_found = 0
    
    run_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"--- Starting EV Game Line Scanner (Run at {run_timestamp}) ---")
    
    utc_now = datetime.now(timezone.utc)
    central_time = utc_now - timedelta(hours=5)
    today = central_time.date()
    
    start_local = datetime(today.year, today.month, today.day, 0, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    end_local = datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=timezone(timedelta(hours=-5)))
    
    for sport, markets in SPORTS_CONFIG.items():
        print(f"\nFetching Odds for {sport}...")
        
        target_books = f"pinnacle,{KS_BOOKS}"
        odds_url = f'https://api.the-odds-api.com/v4/sports/{sport}/odds'
        odds_params = {'apiKey': API_KEY, 'bookmakers': target_books, 'markets': markets, 'oddsFormat': 'american'}
        
        try:
            odds_res = requests.get(odds_url, params=odds_params, timeout=15)
        except Exception as e:
            print(f"Network error fetching odds for {sport}: {e}")
            continue
            
        if odds_res.status_code != 200:
            print(f"API Error fetching odds for {sport}: {odds_res.text}")
            continue
            
        events_data = odds_res.json()
        
        for event in events_data:
            game_name = f"{event['away_team']} @ {event['home_team']}"
            
            try:
                commence_time = datetime.strptime(event['commence_time'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
                
                # Check 1: Ensure the game is scheduled for today (Central Time)
                if not (start_local.astimezone(timezone.utc) <= commence_time <= end_local.astimezone(timezone.utc)):
                    continue
                    
                # Check 2: STRICT PREGAME FILTER - Skip if the game has already started
                if commence_time <= datetime.now(timezone.utc):
                    continue
                    
            except Exception:
                continue
                
            print(f"  -> Scanning {game_name}...")
            
            pinny_true = {}
            for book in event.get('bookmakers', []):
                if book['key'] == 'pinnacle':
                    for market in book.get('markets', []):
                        m_key = market['key']
                        if len(market['outcomes']) == 2:
                            o1, o2 = market['outcomes'][0], market['outcomes'][1]
                            p1 = american_to_prob(o1['price'])
                            p2 = american_to_prob(o2['price'])
                            
                            t1 = p1 / (p1 + p2)
                            t2 = p2 / (p1 + p2)
                            
                            if m_key not in pinny_true: pinny_true[m_key] = {}
                            
                            pinny_true[m_key][(o1['name'], o1.get('point'))] = t1
                            pinny_true[m_key][(o2['name'], o2.get('point'))] = t2

            for book in event.get('bookmakers', []):
                if book['key'] not in ALLOWED_BOOKS: continue
                book_name = book['title']
                
                for market in book.get('markets', []):
                    m_key = market['key']
                    if m_key not in pinny_true: continue
                    
                    for outcome in market['outcomes']:
                        name = outcome['name']
                        pt = outcome.get('point')
                        avail_odds = outcome['price']
                        
                        if avail_odds < 0 and avail_odds < -150:
                            continue
                        if avail_odds > 0 and avail_odds > 200:
                            continue
                        
                        true_prob = pinny_true[m_key].get((name, pt))
                        if true_prob is None: continue

                        dec_odds = american_to_decimal(avail_odds)
                        edge = (true_prob * dec_odds) - 1
                        
                        if edge > 0:
                            edges_found += 1
                            formatted_odds = f"+{avail_odds}" if avail_odds > 0 else str(avail_odds)
                            
                            if m_key == 'h2h':
                                m_display = "Moneyline"
                                p_display = name
                                side_display = "ML"
                                pt_display = "---"
                            elif m_key == 'spreads':
                                m_display = "Spread"
                                p_display = name
                                side_display = "Spread"
                                pt_display = str(pt) if pt is not None else "---"
                            else: 
                                m_display = "Total"
                                p_display = "Game Total"
                                side_display = name 
                                pt_display = str(pt) if pt is not None else "---"
                            
                            dedup_key = (
                                game_name.strip().lower(), 
                                m_display.lower(), 
                                p_display.lower(), 
                                side_display.lower(), 
                                pt_display
                            )
                            if dedup_key in seen_plays: continue

                            b = dec_odds - 1
                            kelly_decimal = (true_prob * b - (1 - true_prob)) / b
                            
                            kelly_units = kelly_decimal * 100
                            quarter_kelly_units = kelly_units / 4
                            dollar_wager = quarter_kelly_units * UNIT_SIZE
                            
                            play_data = {
                                'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                'game': game_name, 'market': m_display, 'player': p_display,
                                'side': side_display, 'line': pt_display, 'book': book_name, 'odds': formatted_odds,
                                'true_prob': f"{true_prob * 100:.1f}", 'edge': f"{edge * 100:.2f}",
                                'units': f"{quarter_kelly_units:.2f}", 'wager': f"{dollar_wager:.2f}"
                            }
                            
                            seen_plays.add(dedup_key)
                            new_plays_to_log.append(play_data)

    if new_plays_to_log:
        log_batch_to_csv(new_plays_to_log, run_timestamp)
        send_discord_digest(new_plays_to_log, run_timestamp)

    print(f"Scan complete. Found {edges_found} active pregame game line edges ({len(new_plays_to_log)} new plays logged & alerted).")

if __name__ == "__main__":
    fetch_and_scan()
