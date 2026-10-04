import os
import re
import csv
import requests
import unicodedata
from datetime import datetime, timedelta
from difflib import SequenceMatcher

DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')
CSV_FILENAME = 'ev_plays_log.csv'
UNIT_SIZE = 25.00

def american_to_decimal(odds):
    if odds > 0: return (odds / 100) + 1
    return (100 / abs(odds)) + 1

def normalize_name(name):
    if not name: return ""
    name = unicodedata.normalize('NFKD', str(name)).encode('ASCII', 'ignore').decode('utf-8')
    name = name.lower()
    name = re.sub(r'\b(jr|sr|ii|iii|iv)\b\.?', '', name)
    name = re.sub(r'[^a-z\s]', '', name)
    return ' '.join(name.split())

def is_team_match(team1, team2):
    n1 = normalize_name(team1)
    n2 = normalize_name(team2)
    if not n1 or not n2: return False
    
    if n1 in n2 or n2 in n1: return True
    
    tokens1 = set(n1.split())
    tokens2 = set(n2.split())
    
    ignore = {'new', 'york', 'los', 'angeles', 'las', 'vegas', 'san', 'bay', 'city', 'state', 'university'}
    t1_core = tokens1 - ignore
    t2_core = tokens2 - ignore
    
    if t1_core and t2_core and not t1_core.isdisjoint(t2_core):
        return True
        
    score = SequenceMatcher(None, n1, n2).ratio()
    return score > 0.75

def migrate_csv():
    if not os.path.isfile(CSV_FILENAME): return False
    with open(CSV_FILENAME, 'r', encoding='utf-8') as f:
        reader = list(csv.reader(f))
    
    if not reader: return False
    headers = reader[0]
    
    if 'Result' not in headers:
        headers.extend(['Result', 'Net Units'])
        with open(CSV_FILENAME, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in reader[1:]:
                if row[0] == '---':
                    row.extend(['---', '---'])
                else:
                    row.extend(['PENDING', '0.00'])
                writer.writerow(row)
    return True

def fetch_completed_events():
    print("Fetching ESPN scoreboards from the last 4 days...")
    events_found = []
    seen_events = set()
    
    sports = [
        ('basketball', 'wnba'), 
        ('basketball', 'nba'), 
        ('hockey', 'nhl'), 
        ('football', 'nfl'), 
        ('football', 'college-football')
    ]
    
    dates_to_check = [(datetime.now() - timedelta(days=i)).strftime('%Y%m%d') for i in range(4)]
    
    for sport, league in sports:
        for d in set(dates_to_check):
            url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates={d}"
            try:
                res = requests.get(url, timeout=10)
                if res.status_code != 200: continue
                events = res.json().get('events', [])
                for event in events:
                    game_id = event['id']
                    if game_id in seen_events: continue
                    seen_events.add(game_id)
                    
                    if event['status']['type']['completed']:
                        events_found.append(event)
            except Exception:
                pass
    return events_found

def evaluate_bet(market, side, line_val, player_str, home_name, home_score, away_name, away_score):
    actual_score_str = f"{int(away_score)}-{int(home_score)}"
    
    if market == 'Moneyline':
        if is_team_match(player_str, home_name):
            margin = home_score - away_score
        elif is_team_match(player_str, away_name):
            margin = away_score - home_score
        else:
            return None
        
        if margin > 0: return ('WIN', actual_score_str)
        elif margin < 0: return ('LOSS', actual_score_str)
        else: return ('PUSH', actual_score_str)

    elif market == 'Spread':
        if is_team_match(player_str, home_name):
            margin = home_score - away_score
        elif is_team_match(player_str, away_name):
            margin = away_score - home_score
        else:
            return None
            
        covered_by = margin + line_val
        if covered_by > 0: return ('WIN', actual_score_str)
        elif covered_by < 0: return ('LOSS', actual_score_str)
        else: return ('PUSH', actual_score_str)

    elif market == 'Total':
        total = home_score + away_score
        if side == 'over':
            if total > line_val: return ('WIN', total)
            elif total < line_val: return ('LOSS', total)
            else: return ('PUSH', total)
        elif side == 'under':
            if total < line_val: return ('WIN', total)
            elif total > line_val: return ('LOSS', total)
            else: return ('PUSH', total)

    return None

def get_game_result(completed_events, game_str, market, side, line_val, player_str):
    try:
        odds_away, odds_home = game_str.split(' @ ')
    except ValueError:
        return None
        
    for event in completed_events:
        try:
            competitors = event['competitions'][0]['competitors']
            home_team = next(c for c in competitors if c['homeAway'] == 'home')
            away_team = next(c for c in competitors if c['homeAway'] == 'away')
            
            home_name = home_team['team']['displayName']
            away_name = away_team['team']['displayName']
            
            if is_team_match(odds_away, away_name) and is_team_match(odds_home, home_name):
                away_score = float(away_team['score'])
                home_score = float(home_team['score'])
                return evaluate_bet(market, side, line_val, player_str, home_name, home_score, away_name, away_score)
        except Exception:
            continue
    return None

def send_digest(daily_buckets, all_time_buckets, graded_count):
    if not DISCORD_WEBHOOK_URL: return
    
    labels = ["1️⃣ **0.0% to 1.99% Edge**", "2️⃣ **2.0% to 4.99% Edge**", "3️⃣ **5.0%+ Edge**"]
    
    daily_total_units = 0.0
    all_time_total_units = 0.0
    
    lines = []
    
    for i in range(3):
        dw, dl, dp, d_units = daily_buckets[i]['W'], daily_buckets[i]['L'], daily_buckets[i]['P'], daily_buckets[i]['Units']
        daily_total_units += d_units
        d_bets = dw + dl
        d_pct = (dw / d_bets * 100) if d_bets > 0 else 0.0
        
        aw, al, ap, a_units = all_time_buckets[i]['W'], all_time_buckets[i]['L'], all_time_buckets[i]['P'], all_time_buckets[i]['Units']
        all_time_total_units += a_units
        a_bets = aw + al
        a_pct = (aw / a_bets * 100) if a_bets > 0 else 0.0
        
        lines.append(f"{labels[i]}")
        lines.append(f"**Today:** {dw}-{dl}-{dp} ({d_pct:.1f}%) | {d_units:+.2f}u")
        lines.append(f"**Lifetime:** {aw}-{al}-{ap} ({a_pct:.1f}%) | {a_units:+.2f}u\n")

    lines.append(f"💰 **Batch Profit:** {daily_total_units:+.2f} Units (${daily_total_units * UNIT_SIZE:+.2f})")
    lines.append(f"🏦 **Lifetime Profit:** {all_time_total_units:+.2f} Units (${all_time_total_units * UNIT_SIZE:+.2f})")

    embed = {
        "title": f"📊 EV Gameline Auto-Grader Report ({graded_count} New Settlements)",
        "description": "\n".join(lines),
        "color": 16753920
    }
    try: requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
    except Exception: pass

def run_grader():
    if not migrate_csv():
        print("No CSV found to grade.")
        return
        
    completed_events = fetch_completed_events()
    print(f"Loaded {len(completed_events)} completed game scores.")
    
    with open(CSV_FILENAME, 'r', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
        
    newly_graded = 0
    daily_buckets = [{'W': 0, 'L': 0, 'P': 0, 'Units': 0.0} for _ in range(3)]
    all_time_buckets = [{'W': 0, 'L': 0, 'P': 0, 'Units': 0.0} for _ in range(3)]
    
    for row in rows:
        player_cell = row.get('Player', '')
        game_cell = row.get('Game', '')
        if not player_cell or player_cell.startswith('---') or game_cell.startswith('==='):
            continue
            
        edge = float(row.get('Edge %', 0))
        b_idx = 0 if edge < 2.0 else (1 if edge < 5.0 else 2)
        just_graded_now = False
        
        current_result = row.get('Result')
        if current_result in ['PENDING', None, '']:
            game_str = row['Game']
            market = row['Market']
            side = row['Side'].lower()
            line_str = row['Line']
            
            try: line_val = float(line_str)
            except ValueError: line_val = 0.0
            
            odds = float(str(row.get('Odds', '0')).replace('+', ''))
            units = float(row.get('Kelly Units', 0))
            
            evaluation = get_game_result(completed_events, game_str, market, side, line_val, player_cell)
            
            if evaluation is not None:
                res, actual = evaluation
                newly_graded += 1
                just_graded_now = True
                
                if res == 'WIN':
                    net = units * (american_to_decimal(odds) - 1)
                elif res == 'LOSS':
                    net = -units
                else:
                    net = 0.0
                    
                row['Result'] = res
                row['Net Units'] = f"{net:.2f}"
                print(f"Graded: {game_str} | {player_cell} {side} {line_str} -> Actual: {actual} ({res})")
            else:
                row['Result'] = 'PENDING'
                row['Net Units'] = '0.00'

        if row.get('Result') in ['WIN', 'LOSS', 'PUSH']:
            res = row['Result']
            try:
                net = float(row.get('Net Units', 0))
            except (ValueError, TypeError):
                net = 0.0
            
            all_time_buckets[b_idx][res[0]] += 1
            all_time_buckets[b_idx]['Units'] += net
            
            if just_graded_now:
                daily_buckets[b_idx][res[0]] += 1
                daily_buckets[b_idx]['Units'] += net

    if newly_graded > 0:
        fieldnames = list(rows[0].keys())
        if 'Result' not in fieldnames:
            fieldnames.extend(['Result', 'Net Units'])
            
        with open(CSV_FILENAME, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            
        send_digest(daily_buckets, all_time_buckets, newly_graded)
    else:
        print("No new pending game lines were ready to be graded.")

if __name__ == "__main__":
    run_grader()
