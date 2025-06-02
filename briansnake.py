from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from flask import Flask, render_template_string
import threading
import time
from selenium.webdriver.common.by import By


def conv(odds):
    odds = int(odds)
    if odds > 0:
        prob = 100 / (odds + 100)
    else:
        prob = -odds / (-odds + 100)
    return prob * 100


def get_soup(url):
    chrome_options = Options()
    # chrome_options.add_argument('--headless')  # Commented out for debugging
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--no-sandbox')
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    driver.get(url)
    time.sleep(2)
    html = driver.page_source
    soup = BeautifulSoup(html, 'lxml')
    driver.quit()
    return soup


def scrape_draftkings(soup):
    teams = soup.find_all('div', class_='event-cell__name-text')
    odds = []
    # Remove city prefix from team names, except for 'Athletics'
    def strip_city(name):
        name = name.strip()
        if name == 'Athletics':
            return name
        parts = name.split(' ', 1)
        if len(parts) == 2:
            return parts[1]
        return name
    # Find all odds and empty cells in order
    odds_and_empty = soup.find_all(['span', 'div'], class_=[
        'sportsbook-odds', 'sportsbook-odds american', 'sportsbook-odds american default-color',
        'sportsbook-odds american no-margin default-color', 'sportsbook-empty-cell body'])
    for el in odds_and_empty:
        if 'sportsbook-empty-cell' in el.get('class', []):
            odds.append('')  # Represent empty cell as empty string
        elif 'sportsbook-odds' in el.get('class', []):
            text = el.text.strip()
            if text and (text[0] == '+' or text[0] == '-' or text[0] == '−'):
                # Replace Unicode minus sign with ASCII hyphen-minus
                text = text.replace('−', '-')
                odds.append(text)
    # Return stripped team names as objects with .text attribute for compatibility
    class FakeTag:
        def __init__(self, text):
            self.text = text
    teams = [FakeTag(strip_city(t.text)) for t in teams]
    return teams, odds


def scrape_betmgm(soup):
    teams = []
    odds = []
    event_blocks = soup.find_all(
        'ms-six-pack-event',
        class_='grid-event grid-six-pack-event ms-active-highlight two-lined-name ng-star-inserted'
    )
    for block in event_blocks:
        teams += block.find_all('div', class_='participant')
        odds_and_empty = block.find_all(['span', 'div', 'ms-option-group'], class_=[
            'custom-odds-value-style ng-star-inserted',
            'offline option-indicator',
            'grid-option-group grid-group offline suspended-lock-box two-column ng-star-inserted'
        ])
        local_odds = []
        for el in odds_and_empty:
            classes = el.get('class', [])
            if 'option-indicator' in classes and 'offline' in classes:
                local_odds.append('')  # Skip one spot
            elif 'grid-option-group' in classes and 'offline' in classes:
                local_odds.append('')
                local_odds.append('')  # Skip two spots
            elif 'custom-odds-value-style' in classes:
                text = el.text.strip()
                if text and (text[0] == '+' or text[0] == '-'):
                    local_odds.append(text)
        # Reorder every 6 odds from 123456 to 135246 (column to row order)
        for i in range(0, len(local_odds), 6):
            group = local_odds[i:i+6]
            if len(group) == 6:
                # 1 2 3 4 5 6 -> 1 3 5 2 4 6
                reordered = [group[0], group[2], group[4], group[1], group[3], group[5]]
                odds.extend(reordered)
            else:
                odds.extend([''] * 6)
    return teams, odds


def scrape_fanduel(soup):
    # FanDuel MLB moneyline odds scraping (robust to dynamic classes)
    teams = []
    odds = []
    # Find all event/game blocks (look for data-test attribute or role)
    event_blocks = soup.find_all(lambda tag: tag.name == 'div' and tag.has_attr('data-test') and 'event' in tag['data-test'])
    if not event_blocks:
        # Fallback: try to find blocks with at least two team names and two odds
        event_blocks = []
        for div in soup.find_all('div'):
            team_spans = div.find_all('span', string=True)
            odds_spans = div.find_all('span', string=True)
            if len(team_spans) >= 2 and any('+' in s or '-' in s for s in [el.text for el in odds_spans]):
                event_blocks.append(div)
    for block in event_blocks:
        # Team names: look for <span> with data-test or aria-label or just text
        team_spans = block.find_all('span', attrs={'data-test': 'participant-name'})
        if not team_spans:
            # Fallback: get all <span> with text and filter out odds
            team_spans = [el for el in block.find_all('span', string=True) if not any(c in el.text for c in '+-')]  # crude filter
        teams.extend(team_spans[:2])  # Only take first two per block
        # Odds: look for <span> with data-test or text containing + or -
        odds_spans = [el for el in block.find_all('span', string=True) if any(c in el.text for c in '+-')]
        odds.extend([el.text.strip() for el in odds_spans[:2]])
        # If odds are missing, pad with empty strings
        if len(odds_spans) < 2:
            odds.extend([''] * (2 - len(odds_spans)))
    # Return stripped team names as objects with .text attribute for compatibility
    class FakeTag:
        def __init__(self, text):
            self.text = text
    teams = [FakeTag(t.text if hasattr(t, 'text') else t) for t in teams]
    # Pad odds to match teams
    if len(odds) < len(teams):
        odds.extend([''] * (len(teams) - len(odds)))
    return teams, odds


SCRAPERS = {
    'draftkings': scrape_draftkings,
    'betmgm': scrape_betmgm,
    'fanduel': scrape_fanduel,
}

URLS = {
    'draftkings': 'https://sportsbook.draftkings.com/leagues/baseball/mlb',
    'betmgm': 'https://www.az.betmgm.com/en/sports/baseball-23/betting/usa-9/mlb-75',
    'fanduel': 'https://sportsbook.fanduel.com/navigation/mlb',  # MLB moneyline page
}


# Shared data for latest odds
def get_moneyline_table(teams, odds):
    num_games = len(teams) // 2
    lines = []
    for i in range(num_games):
        t1 = teams[i*2].text.strip()
        t2 = teams[i*2+1].text.strip()
        o1 = odds[i*6+2] if i*6+2 < len(odds) else ''
        o2 = odds[i*6+5] if i*6+5 < len(odds) else ''
        lines.append(f"{t1:20} {o1:>8}")
        lines.append(f"{t2:20} {o2:>8}")
        lines.append("")
    return '\n'.join(lines)

latest_tables = {'dk': '', 'bm': ''}
selenium_drivers = {}

def start_persistent_drivers():
    chrome_options = Options()
    # chrome_options.add_argument('--headless')  # For debugging, keep visible
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--no-sandbox')
    selenium_drivers['draftkings'] = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    selenium_drivers['betmgm'] = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    selenium_drivers['fanduel'] = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    selenium_drivers['draftkings'].get(URLS['draftkings'])
    selenium_drivers['betmgm'].get(URLS['betmgm'])
    selenium_drivers['fanduel'].get(URLS['fanduel'])
    time.sleep(5)  # Let JS load

def get_soup_persistent(site):
    driver = selenium_drivers[site]
    html = driver.page_source
    return BeautifulSoup(html, 'lxml')

def close_persistent_drivers():
    for drv in selenium_drivers.values():
        try:
            drv.quit()
        except Exception:
            pass

def scrape_and_update_tables():
    while True:
        try:
            soup_dk = get_soup_persistent('draftkings')
            teams_dk, odds_dk = SCRAPERS['draftkings'](soup_dk)
            soup_bm = get_soup_persistent('betmgm')
            teams_bm, odds_bm = SCRAPERS['betmgm'](soup_bm)
            latest_tables['dk'] = get_moneyline_table(teams_dk, odds_dk)
            latest_tables['bm'] = get_moneyline_table(teams_bm, odds_bm)
        except Exception as e:
            latest_tables['dk'] = f"Error: {e}"
            latest_tables['bm'] = f"Error: {e}"
        time.sleep(3)  # Scrape every 5 seconds

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Moneyline Odds Comparison</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
    <style>
        .odds-block { margin-bottom: 2rem; padding: 1.2rem 1.5rem; border: 1px solid #dee2e6; border-radius: 0.5rem; background: #f8f9fa; }
        .teams { font-weight: bold; font-size: 1.15rem; margin-bottom: 0.5rem; }
        .odds-table { width: 100%; margin-bottom: 0; }
        .odds-table th, .odds-table td { text-align: center; padding: 0.3rem 0.6rem; border: none; }
        .odds-table th { background: #e9ecef; font-weight: bold; font-size: 1rem; color: #888; }
        .odds-table td.team { text-align: left; font-weight: 500; }
        .odds-up { color: #198754; font-weight: bold; transition: color 0.3s; }
        .odds-down { color: #dc3545; font-weight: bold; transition: color 0.3s; }
    </style>
</head>
<body>
<div class="container mt-4">
    <h2>Moneyline Odds Comparison</h2>
    <div id="odds-blocks">
        {% for game in games %}
        <div class="odds-block">
            <table class="odds-table">
                <tr>
                    <th style="text-align:left">Teams</th>
                    <th>DraftKings</th>
                    <th>BetMGM</th>
                </tr>
                <tr>
                    <td class="team">{{ game.team1 }}</td>
                    <td>{{ game.dk1 }}</td>
                    <td>{{ game.bm1 }}</td>
                </tr>
                <tr>
                    <td class="team">{{ game.team2 }}</td>
                    <td>{{ game.dk2 }}</td>
                    <td>{{ game.bm2 }}</td>
                </tr>
            </table>
        </div>
        {% endfor %}
    </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
let previousOdds = {};

function oddsToInt(odds) {
    if (typeof odds !== 'string') return null;
    if (!odds.trim()) return null;
    // Remove +, convert to int
    let n = parseInt(odds.replace('+', ''));
    return isNaN(n) ? null : n;
}

function getOddsKey(game, team, site) {
    return `${game.team1}|${game.team2}|${team}|${site}`;
}

function reloadOdds() {
    fetch('/odds_json').then(r => r.json()).then(data => {
        let container = document.getElementById('odds-blocks');
        container.innerHTML = '';
        for (const game of data.games) {
            let block = document.createElement('div');
            block.className = 'odds-block';
            let table = document.createElement('table');
            table.className = 'odds-table';
            table.innerHTML = `
                <tr><th style="text-align:left">Teams</th><th>DraftKings</th><th>BetMGM</th></tr>
                <tr><td class="team">${game.team1}</td><td id="${getOddsKey(game, game.team1, 'dk')}">${game.dk1}</td><td id="${getOddsKey(game, game.team1, 'bm')}">${game.bm1}</td></tr>
                <tr><td class="team">${game.team2}</td><td id="${getOddsKey(game, game.team2, 'dk')}">${game.dk2}</td><td id="${getOddsKey(game, game.team2, 'bm')}">${game.bm2}</td></tr>
            `;
            block.appendChild(table);
            container.appendChild(block);
        }
        // Highlight odds changes
        for (const game of data.games) {
            for (const [team, dk, bm, site] of [
                [game.team1, game.dk1, game.bm1, 'dk'],
                [game.team1, game.dk1, game.bm1, 'bm'],
                [game.team2, game.dk2, game.bm2, 'dk'],
                [game.team2, game.dk2, game.bm2, 'bm']
            ]) {
                let key = getOddsKey(game, team, site);
                let el = document.getElementById(key);
                if (!el) continue;
                let newVal = (site === 'dk') ? (team === game.team1 ? game.dk1 : game.dk2) : (team === game.team1 ? game.bm1 : game.bm2);
                let prevVal = previousOdds[key];
                let newInt = oddsToInt(newVal);
                let prevInt = oddsToInt(prevVal);
                if (prevVal !== undefined && newInt !== null && prevInt !== null && newInt !== prevInt) {
                    // For American odds: higher is better for underdog (+), lower is better for favorite (-)
                    let isImprovement = false;
                    if (prevInt < 0 && newInt > prevInt) isImprovement = true; // -120 to -110 is better
                    if (prevInt > 0 && newInt > prevInt) isImprovement = true; // +120 to +130 is better
                    if (prevInt < 0 && newInt < prevInt) isImprovement = false; // -110 to -120 is worse
                    if (prevInt > 0 && newInt < prevInt) isImprovement = false; // +130 to +120 is worse
                    el.classList.remove('odds-up', 'odds-down');
                    el.classList.add(isImprovement ? 'odds-up' : 'odds-down');
                    setTimeout(() => { el.classList.remove('odds-up', 'odds-down'); }, 2500);
                }
                previousOdds[key] = newVal;
            }
        }
    });
}
setInterval(reloadOdds, 5000); // 5 seconds
</script>
</body>
</html>
'''

def get_moneyline_game_blocks(teams_dk, odds_dk, odds_bm):
    # Returns a list of dicts: [{team1, team2, dk1, dk2, bm1, bm2}, ...] for block rendering
    num_games = len(teams_dk) // 2
    games = []
    for i in range(num_games):
        t1 = teams_dk[i*2].text.strip()
        t2 = teams_dk[i*2+1].text.strip()
        dk1 = odds_dk[i*6+2] if i*6+2 < len(odds_dk) else ''
        dk2 = odds_dk[i*6+5] if i*6+5 < len(odds_dk) else ''
        bm1 = odds_bm[i*6+2] if i*6+2 < len(odds_bm) else ''
        bm2 = odds_bm[i*6+5] if i*6+5 < len(odds_bm) else ''
        games.append({'team1': t1, 'team2': t2, 'dk1': dk1, 'dk2': dk2, 'bm1': bm1, 'bm2': bm2})
    return games

def align_betmgm_to_draftkings(teams_dk, odds_dk, teams_bm, odds_bm):
    # Build a mapping from team name to (index, odds) for BetMGM
    bm_games = []
    for i in range(0, len(teams_bm), 2):
        t1 = teams_bm[i].text.strip()
        t2 = teams_bm[i+1].text.strip() if i+1 < len(teams_bm) else ''
        o = odds_bm[i//2*6:(i//2+1)*6]
        bm_games.append(((t1, t2), o))
    # For each DraftKings game, find the matching BetMGM game (by team set)
    aligned_odds_bm = []
    for i in range(0, len(teams_dk), 2):
        dk_t1 = teams_dk[i].text.strip()
        dk_t2 = teams_dk[i+1].text.strip() if i+1 < len(teams_dk) else ''
        found = False
        for (bm_t1, bm_t2), bm_odds in bm_games:
            if set([dk_t1, dk_t2]) == set([bm_t1, bm_t2]):
                aligned_odds_bm.extend(bm_odds)
                found = True
                break
        if not found:
            aligned_odds_bm.extend(['']*6)
    return aligned_odds_bm

def run_flask_moneyline():
    app = Flask(__name__)

    @app.route('/')
    def index():
        teams_dk, odds_dk = get_current_teams_odds('draftkings')
        teams_bm, odds_bm = get_current_teams_odds('betmgm')
        teams_fd, odds_fd = get_current_teams_odds('fanduel')
        aligned_odds_bm = align_betmgm_to_draftkings(teams_dk, odds_dk, teams_bm, odds_bm)
        aligned_odds_fd = align_betmgm_to_draftkings(teams_dk, odds_dk, teams_fd, odds_fd)
        games = get_moneyline_game_blocks_3way(teams_dk, odds_dk, aligned_odds_bm, aligned_odds_fd)
        return render_template_string(HTML_TEMPLATE_3WAY, games=games)

    @app.route('/odds_json')
    def odds_json():
        teams_dk, odds_dk = get_current_teams_odds('draftkings')
        teams_bm, odds_bm = get_current_teams_odds('betmgm')
        teams_fd, odds_fd = get_current_teams_odds('fanduel')
        aligned_odds_bm = align_betmgm_to_draftkings(teams_dk, odds_dk, teams_bm, odds_bm)
        aligned_odds_fd = align_betmgm_to_draftkings(teams_dk, odds_dk, teams_fd, odds_fd)
        games = get_moneyline_game_blocks_3way(teams_dk, odds_dk, aligned_odds_bm, aligned_odds_fd)
        return {'games': games}

    def get_current_teams_odds(site):
        if site == 'draftkings':
            soup = get_soup_persistent('draftkings')
            return SCRAPERS['draftkings'](soup)
        elif site == 'betmgm':
            soup = get_soup_persistent('betmgm')
            return SCRAPERS['betmgm'](soup)
        else:
            soup = get_soup_persistent('fanduel')
            return SCRAPERS['fanduel'](soup)

    start_persistent_drivers()
    t = threading.Thread(target=scrape_and_update_tables, daemon=True)
    t.start()
    try:
        app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
    finally:
        close_persistent_drivers()

# Helper for 3-way table
HTML_TEMPLATE_3WAY = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Moneyline Odds Comparison</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
    <style>
        .odds-block { margin-bottom: 2rem; padding: 1.2rem 1.5rem; border: 1px solid #dee2e6; border-radius: 0.5rem; background: #f8f9fa; }
        .teams { font-weight: bold; font-size: 1.15rem; margin-bottom: 0.5rem; }
        .odds-table { width: 100%; margin-bottom: 0; }
        .odds-table th, .odds-table td { text-align: center; padding: 0.3rem 0.6rem; border: none; }
        .odds-table th { background: #e9ecef; font-weight: bold; font-size: 1rem; color: #888; }
        .odds-table td.team { text-align: left; font-weight: 500; }
        .odds-up { color: #198754; font-weight: bold; transition: color 0.3s; }
        .odds-down { color: #dc3545; font-weight: bold; transition: color 0.3s; }
        .odds-green { background-color: #d1e7dd; }
        .odds-blue { background-color: #cfe2ff; }
    </style>
</head>
<body>
<div class="container mt-4">
    <h2>Moneyline Odds Comparison</h2>
    <div id="odds-blocks">
        {% for game in games %}
        <div class="odds-block">
            <table class="odds-table">
                <tr>
                    <th style="text-align:left">Teams</th>
                    <th>DraftKings</th>
                    <th>BetMGM</th>
                    <th>FanDuel</th>
                </tr>
                <tr>
                    <td class="team">{{ game.team1 }}</td>
                    <td class="{{ game.dk1_class }}">{{ game.dk1 }}</td>
                    <td class="{{ game.bm1_class }}">{{ game.bm1 }}</td>
                    <td class="{{ game.b365_1_class }}">{{ game.b365_1 }}</td>
                </tr>
                <tr>
                    <td class="team">{{ game.team2 }}</td>
                    <td class="{{ game.dk2_class }}">{{ game.dk2 }}</td>
                    <td class="{{ game.bm2_class }}">{{ game.bm2 }}</td>
                    <td class="{{ game.b365_2_class }}">{{ game.b365_2 }}</td>
                </tr>
            </table>
        </div>
        {% endfor %}
    </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<!-- Odds highlighting JS can be extended for 3-way if needed -->
</body>
</html>
'''

def highlight_odds_row(odds_row):
    # odds_row: list of odds strings (e.g. ['-150', '-170', '+140'])
    odds_ints = []
    for o in odds_row:
        try:
            if o and (o[0] == '+' or o[0] == '-'):  # American odds
                odds_ints.append(int(o.replace('+', '')))
            else:
                odds_ints.append(None)
        except Exception:
            odds_ints.append(None)
    # Find highest positive and least-magnitude negative
    max_pos = None
    max_pos_idx = None
    min_neg = None
    min_neg_idx = None
    for idx, val in enumerate(odds_ints):
        if val is not None:
            if val > 0:
                if max_pos is None or val > max_pos:
                    max_pos = val
                    max_pos_idx = idx
            elif val < 0:
                if min_neg is None or val > min_neg:  # closer to zero
                    min_neg = val
                    min_neg_idx = idx
    # Assign classes
    classes = [''] * len(odds_row)
    if max_pos_idx is not None:
        classes[max_pos_idx] = 'odds-green'
    if min_neg_idx is not None:
        classes[min_neg_idx] = 'odds-blue'
    return classes

def get_moneyline_game_blocks_3way(teams_dk, odds_dk, odds_bm, odds_fd):
    num_games = len(teams_dk) // 2
    games = []
    for i in range(num_games):
        t1 = teams_dk[i*2].text.strip()
        t2 = teams_dk[i*2+1].text.strip()
        # Collect odds for each team from all books
        dk1 = odds_dk[i*6+2] if i*6+2 < len(odds_dk) else ''
        dk2 = odds_dk[i*6+5] if i*6+5 < len(odds_dk) else ''
        bm1 = odds_bm[i*6+2] if i*6+2 < len(odds_bm) else ''
        bm2 = odds_bm[i*6+5] if i*6+5 < len(odds_bm) else ''
        fd1 = odds_fd[i*6+2] if i*6+2 < len(odds_fd) else ''
        fd2 = odds_fd[i*6+5] if i*6+5 < len(odds_fd) else ''
        # Highlight classes for each team row
        row1 = [dk1, bm1, fd1]
        row2 = [dk2, bm2, fd2]
        classes1 = highlight_odds_row(row1)
        classes2 = highlight_odds_row(row2)
        games.append({
            'team1': t1, 'team2': t2,
            'dk1': dk1, 'dk2': dk2, 'bm1': bm1, 'bm2': bm2, 'b365_1': fd1, 'b365_2': fd2,
            'dk1_class': classes1[0], 'bm1_class': classes1[1], 'b365_1_class': classes1[2],
            'dk2_class': classes2[0], 'bm2_class': classes2[1], 'b365_2_class': classes2[2],
        })
    return games
if __name__ == '__main__':
    # Set this to True to run Flask live odds GUI, False to just print odds for one site for testing
    RUN_FLASK = True
    TEST_SITE = 'betmgm'  # Change to 'draftkings' or 'betmgm' for testing
    if RUN_FLASK:
        run_flask_moneyline()
    else:
        # For testing: print all scraped stats for the selected site once
        print(f"\n--- {TEST_SITE.upper()} ---")
        soup = get_soup(URLS[TEST_SITE])
        teams, odds = SCRAPERS[TEST_SITE](soup)
        num_games = len(teams) // 2
        headers = ["Spread", "Total", "Moneyline"]
        for i in range(num_games):
            print(f"Game {i + 1}:")
            print(f"{'':20}{headers[0]:>10}{headers[1]:>10}{headers[2]:>12}")
            def get_odds_text(idx):
                if idx < len(odds):
                    val = odds[idx]
                    return val.text if hasattr(val, 'text') else str(val)
                return ''
            print(f"{teams[i*2].text:20}{get_odds_text(i*6):>10}{get_odds_text(i*6+1):>10}{get_odds_text(i*6+2):>12}")
            print(f"{teams[i*2+1].text:20}{get_odds_text(i*6+3):>10}{get_odds_text(i*6+4):>10}{get_odds_text(i*6+5):>12}")
            print()