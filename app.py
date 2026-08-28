from flask import Flask, render_template, request, jsonify, make_response
import os
import time
import requests


def _fetch_url(url, timeout=20, headers=None):
    """Fetch a URL directly."""
    ua = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
    req_headers = {'User-Agent': ua}
    if headers:
        req_headers.update(headers)
    return requests.get(url, headers=req_headers, timeout=timeout)
from bs4 import BeautifulSoup
import re
import sqlite3
import json

from datetime import datetime, timedelta

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = timedelta(days=7)

import csv
import io

_camp_cache      = None
_camp_cache_time = 0
_CAMP_CACHE_TTL  = 3600
_SHEETS_CSV_URL  = ('https://docs.google.com/spreadsheets/d/'
                    '1T3whgGSEBuhyxuWypY9wSQc_wRTQH52jHMjIaS0ubgs'
                    '/export?format=csv&gid=0')

_stat_cache      = None
_stat_cache_time = 0
_STAT_CACHE_TTL  = 300   # 5 minutes — keeps FAQ roster data fresh
_STAT_BOOK_URL   = 'https://slnstatbook.com/'

def _clean_camp_text(s):
    cleaned = re.sub(r'[^\x20-\x7E]', '', s)       # strip non-ASCII
    cleaned = re.sub(r'\(\s*\)', '', cleaned)        # remove empty parens left behind
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    return cleaned

def _parse_camp_csv(text):
    players = {}
    reader  = csv.DictReader(io.StringIO(text))
    for row in reader:
        name = (row.get('Name') or '').strip()
        if not name:
            continue
        key    = name.lower()
        rating = _clean_camp_text((row.get('Rating') or '').strip())
        year   = (row.get('Year')   or '').strip()
        total  = (row.get('Total')  or '').strip()
        if key not in players:
            players[key] = {'name': name, 'entries': [], 'total': None}
        if rating and year:
            players[key]['entries'].append({'year': year, 'rating': rating})
        if total and total != 'N/A':
            players[key]['total'] = total.replace('Total:', '').strip()
    return players

DATABASE_URL = os.environ.get('DATABASE_URL', '')
DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'depth_charts.db'))
USE_POSTGRES = bool(DATABASE_URL)

# ── Database wrapper ──────────────────────────────────────────────────────────
# Provides a unified interface over SQLite (local) and PostgreSQL (production).
# All query placeholders should be written as ? — they are translated to %s
# automatically when PostgreSQL is in use.

class _DBConn:
    """Thin wrapper that normalises SQLite and psycopg2 connections."""

    def __init__(self, raw, is_pg):
        self._raw = raw
        self._is_pg = is_pg
        self._cur = raw.cursor() if is_pg else None

    def _adapt(self, sql):
        """Replace ? placeholders with %s for PostgreSQL."""
        if self._is_pg:
            return sql.replace('?', '%s')
        return sql

    def execute(self, sql, params=()):
        sql = self._adapt(sql)
        if self._is_pg:
            # Don't pass empty params tuple — psycopg2 would try to format % chars
            # in the SQL (e.g. LIKE 'salary:%') and raise IndexError
            if params:
                self._cur.execute(sql, params)
            else:
                self._cur.execute(sql)
            return self._cur
        else:
            return self._raw.execute(sql, params)

    def commit(self):
        self._raw.commit()

    def close(self):
        if self._is_pg:
            self._cur.close()
        self._raw.close()


def get_db():
    if USE_POSTGRES:
        import psycopg2
        url = DATABASE_URL
        # Railway sometimes uses postgres:// which psycopg2 needs as postgresql://
        if url.startswith('postgres://'):
            url = url.replace('postgres://', 'postgresql://', 1)
        raw = psycopg2.connect(url)
        conn = _DBConn(raw, is_pg=True)
        conn.execute('''CREATE TABLE IF NOT EXISTS team_charts
                        (team_url TEXT PRIMARY KEY, data TEXT,
                         updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS draft_state
                        (id INTEGER PRIMARY KEY,
                         data TEXT,
                         updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS owed_picks
                        (id INTEGER PRIMARY KEY,
                         data TEXT,
                         updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS settings
                        (key TEXT PRIMARY KEY, value TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS roster_cache
                        (team_url TEXT PRIMARY KEY, data TEXT,
                         fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.commit()
        return conn
    else:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        raw = sqlite3.connect(DB_PATH)
        conn = _DBConn(raw, is_pg=False)
        conn.execute('''CREATE TABLE IF NOT EXISTS team_charts
                        (team_url TEXT PRIMARY KEY, data TEXT,
                         updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS draft_state
                        (id INTEGER PRIMARY KEY CHECK (id = 1),
                         data TEXT,
                         updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS owed_picks
                        (id INTEGER PRIMARY KEY CHECK (id = 1),
                         data TEXT,
                         updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS settings
                        (key TEXT PRIMARY KEY, value TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS roster_cache
                        (team_url TEXT PRIMARY KEY, data TEXT,
                         fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.commit()
        return conn


@app.route('/save_chart', methods=['POST'])
def save_chart():
    body = request.get_json()
    team_url = (body.get('team_url') or '').strip()
    data = body.get('data')
    if not team_url or data is None:
        return jsonify({'error': 'Missing fields'}), 400
    try:
        conn = get_db()
        conn.execute(
            '''INSERT INTO team_charts (team_url, data, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT (team_url) DO UPDATE SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at''',
            (team_url, json.dumps(data))
        )
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/load_chart', methods=['POST'])
def load_chart():
    body = request.get_json()
    team_url = (body.get('team_url') or '').strip()
    if not team_url:
        return jsonify({'error': 'Missing team_url'}), 400
    try:
        conn = get_db()
        row = conn.execute('SELECT data FROM team_charts WHERE team_url = ?', (team_url,)).fetchone()
        conn.close()
        return jsonify({'data': json.loads(row[0]) if row else None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Players allowed to start at PG regardless of their listed position
PG_STARTER_EXCEPTIONS = {
    'luka doncic',
    'boogie fland',
    'darius adams',
    'dylan harper',
    'dyland harper',
    'martray bagley',
    'caleb holt',
    'immanuel quickley',
    'cade cunningham',
    'jeremiah fears',
    'dyson daniels',
    'egor demin',
    'isaac bonga',
    'jalen haralson',
    'dooney johnson',
}

# Grade scale: index 0 = worst, 12 = best
GRADE_ORDER = ['F', 'D-', 'D', 'D+', 'C-', 'C', 'C+', 'B-', 'B', 'B+', 'A-', 'A', 'A+']


def grade_value(grade):
    g = str(grade).strip()
    try:
        return GRADE_ORDER.index(g)
    except ValueError:
        return -1


def grade_gte(grade, threshold):
    """grade is at least as good as threshold (e.g. B >= C)"""
    return grade_value(grade) >= grade_value(threshold)


def grade_lte(grade, threshold):
    """grade is no better than threshold / 'threshold or worse' (e.g. B- <= B-)"""
    return grade_value(grade) <= grade_value(threshold)


def parse_height_inches(height_str):
    match = re.match(r"(\d+)['\-](\d+)", str(height_str))
    if match:
        return int(match.group(1)) * 12 + int(match.group(2))
    return 0


def compute_eligibility(player):
    pos = player['pos']
    name = player['name'].lower().strip()
    reb = player['reb']
    out = player['out']

    starter = {}
    backup = {}

    # --- STARTER RULES ---

    # PG: only PG-position players are eligible, plus specific named exceptions
    starter['PG'] = pos == 'PG' or name in PG_STARTER_EXCEPTIONS

    # SG: PG can play up freely; SG/SF need reb B- or worse; PF/C prohibited
    if pos == 'PG':
        starter['SG'] = True
    elif pos in ('SG', 'SF'):
        starter['SG'] = grade_lte(reb, 'B-')
    else:  # PF, C
        starter['SG'] = False

    # SF: PG/SG/SF freely; PF/C need reb B+ or worse AND out C or better
    if pos in ('PG', 'SG', 'SF'):
        starter['SF'] = True
    else:
        starter['SF'] = grade_lte(reb, 'B+') and grade_gte(out, 'C')

    # PF: anyone can play up to PF (PF/C interchangeable, smaller positions play up)
    starter['PF'] = True

    # C: anyone can play up to C
    starter['C'] = True

    # --- BACKUP RULES: no restrictions, any player can back up any position ---
    backup['PG'] = True
    backup['SG'] = True
    backup['SF'] = True
    backup['PF'] = True
    backup['C'] = True

    return {'starter': starter, 'backup': backup}


def get_violation_reason(player, pos, slot):
    """Return human-readable reason why a player can't fill this slot."""
    p_pos = player['pos']
    reb = player['reb']
    out = player['out']

    if slot == 0:  # starter
        if pos == 'PG':
            return f"{player['name']} must be listed at PG or have the Can Play PG stip to start at PG"
        if pos == 'SG':
            if p_pos in ('PF', 'C'):
                return "PF/C cannot start at SG"
            return f"Reb must be B- or worse (is {reb})"
        if pos == 'SF' and p_pos in ('PF', 'C'):
            reasons = []
            if not grade_lte(reb, 'B+'):
                reasons.append(f"Reb must be B+ or worse (is {reb})")
            if not grade_gte(out, 'C'):
                reasons.append(f"Outside must be C or better (is {out})")
            return "; ".join(reasons)
    else:  # backup
        if pos == 'PG':
            return "PF/C cannot backup PG"

    return "Not eligible for this slot"


def get_version():
    try:
        with open(os.path.join(os.path.dirname(__file__), 'VERSION')) as f:
            return f.read().strip()
    except Exception:
        return '?'

@app.route('/')
def index():
    resp = make_response(render_template('index.html', version=get_version(), league_year=get_league_year()))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp

@app.route('/mockups')
def mockups():
    return render_template('mockups.html')

@app.route('/mockup-trade-finder')
def mockup_trade_finder():
    return render_template('mockup_trade_finder.html')

@app.route('/mockup-team-select')
def mockup_team_select():
    return render_template('mockup_team_select.html')




def _parse_roster_from_soup(soup):
    """Parse players and stats from a BeautifulSoup object of a roster page.
    Returns {'players': [...], 'team_name': str} or raises ValueError on failure."""
    team_name = ''
    title_tag = soup.find('title')
    if title_tag:
        team_name = title_tag.get_text(strip=True)
    if not team_name:
        h1 = soup.find('h1')
        if h1:
            team_name = h1.get_text(strip=True)

    players = []
    valid_positions = {'PG', 'SG', 'SF', 'PF', 'C'}

    for table in soup.find_all('table'):
        all_rows = table.find_all('tr')
        if not all_rows:
            continue
        header_row_index = None
        col_names = []
        for i, row in enumerate(all_rows):
            candidate = [c.get_text(strip=True).lower() for c in row.find_all(['th', 'td'])]
            if 'pos' in candidate and 'reb' in candidate and 'hn' in candidate:
                col_names = candidate
                header_row_index = i
                break
        if header_row_index is None:
            continue
        rows = all_rows[header_row_index + 1:]
        for row in rows:
            cells = [td.get_text(strip=True) for td in row.find_all('td')]
            if len(cells) < len(col_names):
                continue
            d = dict(zip(col_names, cells))
            name = d.get('name', '').strip()
            pos = d.get('pos', '').strip().upper()
            if not name or pos not in valid_positions:
                continue
            height_str = d.get('height', '')
            height_inches = parse_height_inches(height_str)
            wt_match = re.search(r'(\d+)', str(d.get('weight', '0')))
            weight = int(wt_match.group(1)) if wt_match else 0
            player = {
                'name': name, 'pos': pos, 'age': d.get('age', ''),
                'height': height_str, 'height_inches': height_inches, 'weight': weight,
                'in_rating': d.get('in', ''), 'out': d.get('out', ''),
                'hn': d.get('hn', ''), 'df': d.get('df', ''),
                'reb': d.get('reb', ''), 'pot': d.get('pot', ''),
            }
            elig = compute_eligibility(player)
            player['eligible_starter'] = elig['starter']
            player['eligible_backup'] = elig['backup']
            players.append(player)
        if players:
            break

    if not players:
        raise ValueError('Could not find a player abilities table on this page.')

    stat_cols = ['ppg', 'rpg', 'apg', 'spg', 'bpg', 'tpg', 'fg%', 'ft%', '3p%']
    stats_map = {}
    for table in soup.find_all('table'):
        all_rows = table.find_all('tr')
        header_row_index = None
        col_names = []
        for i, row in enumerate(all_rows):
            candidate = [c.get_text(strip=True).lower() for c in row.find_all(['th', 'td'])]
            if 'ppg' in candidate and 'rpg' in candidate:
                col_names = candidate
                header_row_index = i
                break
        if header_row_index is None:
            continue
        for row in all_rows[header_row_index + 1:]:
            name_tag = row.find('a')
            cells = [td.get_text(strip=True) for td in row.find_all('td')]
            if not name_tag or len(cells) < len(col_names):
                continue
            pname = name_tag.get_text(strip=True)
            d = dict(zip(col_names, cells))
            stats_map[pname] = {k: d.get(k, '') for k in stat_cols}
        if stats_map:
            break

    for p in players:
        s = stats_map.get(p['name'], {})
        p['ppg'] = s.get('ppg', '')
        p['rpg'] = s.get('rpg', '')
        p['apg'] = s.get('apg', '')
        p['spg'] = s.get('spg', '')
        p['bpg'] = s.get('bpg', '')
        p['tpg'] = s.get('tpg', '')
        p['fg_pct'] = s.get('fg%', '')
        p['ft_pct'] = s.get('ft%', '')
        p['three_pct'] = s.get('3p%', '')

    pos_order = {'PG': 0, 'SG': 1, 'SF': 2, 'PF': 3, 'C': 4}
    players.sort(key=lambda p: (pos_order.get(p['pos'], 5), p['name']))
    return {'players': players, 'team_name': team_name}


@app.route('/parse_roster_html', methods=['POST'])
def parse_roster_html():
    """Parse a roster page from raw HTML pasted by the user's browser."""
    html = (request.json or {}).get('html', '').strip()
    if not html:
        return jsonify({'error': 'No HTML provided'}), 400
    try:
        soup = BeautifulSoup(html, 'html.parser')
        result = _parse_roster_from_soup(soup)
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Error parsing roster: {str(e)}'}), 500


@app.route('/fetch_roster', methods=['POST'])
def fetch_roster():
    url = (request.get_json() or {}).get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    try:
        # Check server-side cache (30-minute TTL) to avoid hammering simleaguenirvana.com
        try:
            conn = get_db()
            cache_row = conn.execute(
                "SELECT data, fetched_at FROM roster_cache WHERE team_url = ?", (url,)
            ).fetchone()
            conn.close()
            if cache_row:
                fetched_at = cache_row[1]
                if isinstance(fetched_at, str):
                    fetched_at = datetime.fromisoformat(fetched_at)
                if datetime.utcnow() - fetched_at < timedelta(minutes=30):
                    return jsonify(json.loads(cache_row[0]))
        except Exception as e:
            app.logger.warning("non-critical error suppressed: %s", e)

        # Pass SLN cookie so auth-required roster pages load
        try:
            _rc = get_db()
            _cr = _rc.execute("SELECT value FROM settings WHERE key='sln_cookie'").fetchone()
            _rc.close()
            _ck = ((_cr[0] if _cr else None) or os.environ.get('SLN_COOKIE', '')).strip()
        except Exception:
            _ck = os.environ.get('SLN_COOKIE', '').strip()
        _rh = {'Cookie': _ck} if _ck else None
        resp = _fetch_url(url, timeout=20, headers=_rh)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        result = _parse_roster_from_soup(soup)

        # Save to cache
        try:
            conn = get_db()
            now = datetime.utcnow().isoformat()
            if USE_POSTGRES:
                conn.execute(
                    "INSERT INTO roster_cache (team_url, data, fetched_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (team_url) DO UPDATE SET data = EXCLUDED.data, fetched_at = EXCLUDED.fetched_at",
                    (url, json.dumps(result), now)
                )
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO roster_cache (team_url, data, fetched_at) VALUES (?, ?, ?)",
                    (url, json.dumps(result), now)
                )
            conn.commit()
            conn.close()
        except Exception as e:
            app.logger.warning("non-critical error suppressed: %s", e)

        return jsonify(result)

    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out. Please check the URL.'}), 400
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to fetch page: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': f'Error parsing roster: {str(e)}'}), 500


@app.route('/fetch_free_agents', methods=['POST'])
def fetch_free_agents():
    url = 'https://www.simleaguenirvana.com/fa/fa-pos.htm'
    try:
        # FA page requires authentication — pass the stored SLN cookie
        try:
            _db = get_db()
            _cookie_row = _db.execute("SELECT value FROM settings WHERE key='sln_cookie'").fetchone()
            _db.close()
            _cookie = ((_cookie_row[0] if _cookie_row else None) or os.environ.get('SLN_COOKIE', '')).strip()
        except Exception:
            _cookie = os.environ.get('SLN_COOKIE', '').strip()
        _headers = {'Cookie': _cookie} if _cookie else None
        resp = _fetch_url(url, timeout=20, headers=_headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        players = []
        valid_positions = {'PG', 'SG', 'SF', 'PF', 'C'}

        for table in soup.find_all('table'):
            all_rows = table.find_all('tr')
            if not all_rows:
                continue

            header_row_index = None
            col_names = []
            for i, row in enumerate(all_rows):
                candidate = [
                    c.get_text(strip=True).lower()
                    for c in row.find_all(['th', 'td'])
                ]
                if 'pos' in candidate and 'reb' in candidate and 'hn' in candidate:
                    col_names = candidate
                    header_row_index = i
                    break

            if header_row_index is None:
                continue

            rows = all_rows[header_row_index + 1:]
            for row in rows:
                cells = [td.get_text(strip=True) for td in row.find_all('td')]
                if len(cells) < len(col_names):
                    continue

                d = dict(zip(col_names, cells))
                name = d.get('name', '').strip()
                pos = d.get('pos', '').strip().upper()

                if not name or pos not in valid_positions:
                    continue

                height_str = d.get('height', '')
                height_inches = parse_height_inches(height_str)

                wt_match = re.search(r'(\d+)', str(d.get('weight', '0')))
                weight = int(wt_match.group(1)) if wt_match else 0

                player = {
                    'name': name,
                    'pos': pos,
                    'age': d.get('age', ''),
                    'height': height_str,
                    'height_inches': height_inches,
                    'weight': weight,
                    'in_rating': d.get('in', ''),
                    'out': d.get('out', ''),
                    'hn': d.get('hn', ''),
                    'df': d.get('df', ''),
                    'reb': d.get('reb', ''),
                    'pot': d.get('pot', ''),
                    'last_team': d.get('last team', ''),
                    'ppg': '', 'rpg': '', 'apg': '', 'spg': '',
                    'bpg': '', 'tpg': '', 'fg_pct': '', 'ft_pct': '', 'three_pct': '',
                    'is_fa': True,
                }

                elig = compute_eligibility(player)
                player['eligible_starter'] = elig['starter']
                player['eligible_backup'] = elig['backup']

                players.append(player)

            if players:
                break

        if not players:
            return jsonify({'error': 'Could not find free agent data on this page.'}), 400

        pos_order = {'PG': 0, 'SG': 1, 'SF': 2, 'PF': 3, 'C': 4}
        players.sort(key=lambda p: (pos_order.get(p['pos'], 5), p['name']))

        return jsonify({'players': players})

    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out.'}), 400
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to fetch FA page: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': f'Error parsing FA data: {str(e)}'}), 500


def parse_salary(salary_str):
    """Parse salary string like '$2,863,892' to integer."""
    if not salary_str:
        return 0
    clean = re.sub(r'[$,\s]', '', str(salary_str))
    try:
        return int(float(clean))
    except (ValueError, TypeError):
        return 0


def _parse_draft_picks_from_soup(soup):
    """Parse the 'Draft Picks' section from an SLN roster page.
    Returns a list of {year, round, original_team} dicts."""
    picks = []
    for table in soup.find_all('table'):
        if 'draft picks' not in table.get_text(' ', strip=True).lower():
            continue
        current_year = None
        in_picks = False
        for row in table.find_all('tr'):
            cells = [td.get_text(strip=True) for td in row.find_all(['td', 'th'])]
            if not cells:
                continue
            if not in_picks:
                if any('draft picks' in c.lower() for c in cells):
                    in_picks = True
                continue
            # Year row?
            year_found = False
            for c in cells:
                m = re.match(r'^(20[2-9]\d)$', c.strip())
                if m:
                    current_year = int(m.group(1))
                    year_found = True
                    break
            if year_found:
                continue
            # Header row?
            low = [c.lower() for c in cells]
            if 'round' in low or 'team' in low:
                continue
            # Data row: first cell is round (1 or 2), second is team name
            if current_year and len(cells) >= 2:
                round_str = cells[0].strip()
                team_str = cells[1].strip()
                if re.match(r'^[12]$', round_str) and team_str:
                    picks.append({'year': current_year, 'round': int(round_str), 'original_team': team_str})
        if picks:
            break
    return picks


@app.route('/fetch_salary_roster', methods=['POST'])
def fetch_salary_roster():
    url = (request.get_json() or {}).get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    cache_key = 'salary:' + url
    try:
        conn = get_db()
        cache_row = conn.execute(
            "SELECT data, fetched_at FROM roster_cache WHERE team_url = ?", (cache_key,)
        ).fetchone()
        conn.close()
        if cache_row:
            fetched_at = cache_row[1]
            if isinstance(fetched_at, str):
                fetched_at = datetime.fromisoformat(fetched_at)
            if datetime.utcnow() - fetched_at < timedelta(hours=4):
                cached_data = json.loads(cache_row[0])
                # Bust old cache entries that don't have rating fields
                players_list = cached_data.get('players', [])
                if players_list and players_list[0].get('in_rat'):
                    return jsonify(cached_data)
    except Exception as e:
        app.logger.warning("non-critical error suppressed: %s", e)

    try:
        # Pass SLN cookie for auth-required pages
        try:
            _sc = get_db()
            try:
                _scr = _sc.execute("SELECT value FROM settings WHERE key='sln_cookie'").fetchone()
                _sck = ((_scr[0] if _scr else None) or os.environ.get('SLN_COOKIE', '')).strip()
            finally:
                _sc.close()
        except Exception:
            _sck = os.environ.get('SLN_COOKIE', '').strip()
        _sh = {'Cookie': _sck} if _sck else None
        resp = None
        for _attempt in range(3):
            resp = _fetch_url(url, timeout=20, headers=_sh)
            if resp.status_code != 429:
                break
            time.sleep(2 ** _attempt)  # 1s, 2s, 4s backoff
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        team_name = ''
        title_tag = soup.find('title')
        if title_tag:
            team_name = title_tag.get_text(strip=True)

        players = []

        # SLN pages use nested tables. find_all('tr') on the outer table
        # recurses into all nested tables, producing wrong row counts and
        # shifted indices. Using recursive=False gets only the direct child
        # rows of each table, which correctly isolates the salary table.
        # Note: SLN uses <td> for ALL cells including column headers (no <th>).
        for table in soup.find_all('table'):
            rows = table.find_all('tr', recursive=False)
            if not rows:
                tbody = table.find('tbody', recursive=False)
                if tbody:
                    rows = tbody.find_all('tr', recursive=False)
            if not rows:
                continue

            # Find the header row that has both 'name' and 'year 1' columns
            header_row_index = None
            year1_idx = None
            for i, row in enumerate(rows):
                cols = [c.get_text(strip=True).lower() for c in row.find_all('td')]
                if 'name' in cols and 'year 1' in cols:
                    year1_idx = cols.index('year 1')
                    header_row_index = i
                    break

            if header_row_index is None:
                continue

            for row in rows[header_row_index + 1:]:
                # Player name is always wrapped in an <a> tag on SLN pages
                name_tag = row.find('a')
                if not name_tag:
                    continue
                name = name_tag.get_text(strip=True)
                if not name:
                    continue
                td_cells = [c.get_text(strip=True) for c in row.find_all('td')]
                if len(td_cells) <= year1_idx:
                    continue
                salary = parse_salary(td_cells[year1_idx])
                players.append({'name': name, 'salary': salary})

            if players:
                break

        if not players:
            return jsonify({'error': 'Could not find salary data (Year 1 column) on this page.'}), 400

        # Parse abilities from same page using the working _parse_roster_from_soup function
        # (avoids nested-table issues that break a naive loop approach)
        try:
            abilities_result = _parse_roster_from_soup(soup)
            abilities_map = {p['name'].lower(): {
                'in_rat': p.get('in_rating', ''),
                'out': p.get('out', ''),
                'hn': p.get('hn', ''),
                'df': p.get('df', ''),
                'reb': p.get('reb', ''),
            } for p in abilities_result['players']}
            if not abilities_map:
                app.logger.warning('fetch_salary_roster: abilities_map empty after parse for %s', url)
        except Exception as e:
            app.logger.warning('fetch_salary_roster: abilities parsing failed for %s: %s', url, e)
            abilities_map = {}

        # Log any salary players that didn't match an abilities entry (name mismatch)
        unmatched = [p['name'] for p in players if not abilities_map.get(p['name'].lower())]
        if unmatched and abilities_map:
            app.logger.warning('fetch_salary_roster: %d unmatched names for %s: %s', len(unmatched), url, unmatched[:5])

        # Merge ratings into salary players
        for p in players:
            ab = abilities_map.get(p['name'].lower(), {})
            p['in_rat'] = ab.get('in_rat', '')
            p['out'] = ab.get('out', '')
            p['hn'] = ab.get('hn', '')
            p['df'] = ab.get('df', '')
            p['reb'] = ab.get('reb', '')

        # Add cut players Year 1 salary to total
        cut_salary = 0
        for table in soup.find_all('table'):
            rows = table.find_all('tr', recursive=False)
            if not rows:
                tbody = table.find('tbody', recursive=False)
                if tbody:
                    rows = tbody.find_all('tr', recursive=False)
            for row in rows:
                all_tds = [c.get_text(strip=True) for c in row.find_all('td')]
                lower = [t.lower() for t in all_tds]
                if 'cut players:' not in lower:
                    continue
                total_pos = next((i for i, t in enumerate(lower) if t == 'total'), None)
                if total_pos is not None and total_pos + 1 < len(all_tds):
                    cut_salary = parse_salary(all_tds[total_pos + 1])
                break
            if cut_salary:
                break

        total_salary = sum(p['salary'] for p in players) + cut_salary
        draft_picks = _parse_draft_picks_from_soup(soup)
        result = {'players': players, 'team_name': team_name, 'total_salary': total_salary, 'draft_picks': draft_picks}

        try:
            conn = get_db()
            now = datetime.utcnow().isoformat()
            if USE_POSTGRES:
                conn.execute(
                    "INSERT INTO roster_cache (team_url, data, fetched_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (team_url) DO UPDATE SET data = EXCLUDED.data, fetched_at = EXCLUDED.fetched_at",
                    (cache_key, json.dumps(result), now)
                )
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO roster_cache (team_url, data, fetched_at) VALUES (?, ?, ?)",
                    (cache_key, json.dumps(result), now)
                )
            conn.commit()
            conn.close()
        except Exception as e:
            app.logger.warning("non-critical error suppressed: %s", e)

        return jsonify(result)

    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out.'}), 400
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to fetch page: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': f'Error parsing salary data: {str(e)}'}), 500


def _parse_salary_roster_from_soup(soup):
    """Parse salary + abilities from a BeautifulSoup roster page. Returns the same
    structure as fetch_salary_roster so client-side fallback works identically."""
    team_name = ''
    title_tag = soup.find('title')
    if title_tag:
        team_name = title_tag.get_text(strip=True)

    players = []
    for table in soup.find_all('table'):
        rows = table.find_all('tr', recursive=False)
        if not rows:
            tbody = table.find('tbody', recursive=False)
            if tbody:
                rows = tbody.find_all('tr', recursive=False)
        if not rows:
            continue
        header_row_index = None
        year1_idx = None
        for i, row in enumerate(rows):
            cols = [c.get_text(strip=True).lower() for c in row.find_all('td')]
            if 'name' in cols and 'year 1' in cols:
                year1_idx = cols.index('year 1')
                header_row_index = i
                break
        if header_row_index is None:
            continue
        for row in rows[header_row_index + 1:]:
            name_tag = row.find('a')
            if not name_tag:
                continue
            name = name_tag.get_text(strip=True)
            if not name:
                continue
            td_cells = [c.get_text(strip=True) for c in row.find_all('td')]
            if len(td_cells) <= year1_idx:
                continue
            salary = parse_salary(td_cells[year1_idx])
            players.append({'name': name, 'salary': salary})
        if players:
            break

    if not players:
        raise ValueError('Could not find salary data (Year 1 column) on this page.')

    try:
        abilities_result = _parse_roster_from_soup(soup)
        abilities_map = {p['name'].lower(): {
            'in_rat': p.get('in_rating', ''), 'out': p.get('out', ''),
            'hn': p.get('hn', ''), 'df': p.get('df', ''), 'reb': p.get('reb', ''),
        } for p in abilities_result['players']}
    except Exception:
        abilities_map = {}

    for p in players:
        ab = abilities_map.get(p['name'].lower(), {})
        p['in_rat'] = ab.get('in_rat', '')
        p['out'] = ab.get('out', '')
        p['hn'] = ab.get('hn', '')
        p['df'] = ab.get('df', '')
        p['reb'] = ab.get('reb', '')

    cut_salary = 0
    for table in soup.find_all('table'):
        rows = table.find_all('tr', recursive=False)
        if not rows:
            tbody = table.find('tbody', recursive=False)
            if tbody:
                rows = tbody.find_all('tr', recursive=False)
        for row in rows:
            all_tds = [c.get_text(strip=True) for c in row.find_all('td')]
            lower = [t.lower() for t in all_tds]
            if 'cut players:' not in lower:
                continue
            total_pos = next((i for i, t in enumerate(lower) if t == 'total'), None)
            if total_pos is not None and total_pos + 1 < len(all_tds):
                cut_salary = parse_salary(all_tds[total_pos + 1])
            break
        if cut_salary:
            break

    total_salary = sum(p['salary'] for p in players) + cut_salary
    draft_picks = _parse_draft_picks_from_soup(soup)
    return {'players': players, 'team_name': team_name, 'total_salary': total_salary, 'draft_picks': draft_picks}


@app.route('/parse_salary_roster_html', methods=['POST'])
def parse_salary_roster_html():
    html = (request.json or {}).get('html', '').strip()
    if not html:
        return jsonify({'error': 'No HTML provided'}), 400
    try:
        soup = BeautifulSoup(html, 'html.parser')
        result = _parse_salary_roster_from_soup(soup)
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Error parsing salary data: {str(e)}'}), 500


# ── Static draft player pool (snapshot: 2026-08-28) ─────────────────────────
# To update this list, re-fetch https://www.simleaguenirvana.com/draft/draftplayers-pot.htm
DRAFT_PLAYER_POOL = [
    {'id':'','name':'Bryson Alford','pos':'C','ht':"7'1\"",'wt':'230','age':'20','in_rat':'C','out_rat':'C','hn':'C-','df':'C+','reb':'C+','pot':'A'},
    {'id':'','name':'Lennox Leonard','pos':'C','ht':"7'0\"",'wt':'260','age':'20','in_rat':'B','out_rat':'C-','hn':'D-','df':'B','reb':'C+','pot':'A'},
    {'id':'','name':'Rannier Fowler','pos':'PG','ht':"6'4\"",'wt':'195','age':'20','in_rat':'C+','out_rat':'B','hn':'B+','df':'C','reb':'C','pot':'A'},
    {'id':'','name':'Gwindell Saunders','pos':'PF','ht':"6'9\"",'wt':'235','age':'20','in_rat':'B','out_rat':'C+','hn':'C-','df':'C+','reb':'B','pot':'A'},
    {'id':'','name':'Jose Luis Cueva','pos':'SG','ht':"6'5\"",'wt':'190','age':'21','in_rat':'C+','out_rat':'B','hn':'C','df':'C+','reb':'C-','pot':'A'},
    {'id':'','name':'King Richards','pos':'SF','ht':"6'7\"",'wt':'220','age':'21','in_rat':'B','out_rat':'C+','hn':'C','df':'C+','reb':'C+','pot':'A'},
    {'id':'','name':'Ayren Hayes','pos':'SF','ht':"6'8\"",'wt':'225','age':'21','in_rat':'C','out_rat':'B-','hn':'C','df':'A','reb':'C','pot':'A'},
    {'id':'','name':'Kaenan Kennedy','pos':'PF','ht':"6'10\"",'wt':'230','age':'21','in_rat':'C+','out_rat':'C+','hn':'C+','df':'B','reb':'C','pot':'A'},
    {'id':'','name':'Joel Read','pos':'PG','ht':"6'3\"",'wt':'195','age':'21','in_rat':'C','out_rat':'B','hn':'B+','df':'C','reb':'D+','pot':'B'},
    {'id':'','name':'Cain Livingston','pos':'SG','ht':"6'7\"",'wt':'195','age':'19','in_rat':'C+','out_rat':'C','hn':'C','df':'B','reb':'C','pot':'B'},
    {'id':'','name':'Holden McCoy','pos':'C','ht':"7'1\"",'wt':'240','age':'21','in_rat':'C','out_rat':'B+','hn':'D','df':'B+','reb':'C','pot':'B'},
    {'id':'','name':'Samuel Malone','pos':'SF','ht':"6'7\"",'wt':'225','age':'22','in_rat':'C+','out_rat':'C+','hn':'C','df':'C+','reb':'C','pot':'B'},
    {'id':'','name':'Davis Roman','pos':'PG','ht':"6'3\"",'wt':'185','age':'21','in_rat':'C','out_rat':'C','hn':'B-','df':'C+','reb':'D+','pot':'B'},
    {'id':'','name':'Kyson Jenkins','pos':'PG','ht':"6'4\"",'wt':'205','age':'20','in_rat':'C','out_rat':'B','hn':'C','df':'B','reb':'C+','pot':'B'},
    {'id':'','name':'Derrick Frye','pos':'SG','ht':"6'6\"",'wt':'200','age':'21','in_rat':'C','out_rat':'C+','hn':'C+','df':'C+','reb':'C-','pot':'B'},
    {'id':'','name':'Luke Rees','pos':'SG','ht':"6'4\"",'wt':'190','age':'22','in_rat':'C','out_rat':'B+','hn':'C','df':'C','reb':'C-','pot':'B'},
    {'id':'','name':'Blake Watson','pos':'PF','ht':"6'10\"",'wt':'240','age':'21','in_rat':'B-','out_rat':'B','hn':'D+','df':'B-','reb':'C','pot':'B'},
    {'id':'','name':'Bronson David','pos':'PG','ht':"6'4\"",'wt':'190','age':'21','in_rat':'C','out_rat':'C+','hn':'A-','df':'B-','reb':'D+','pot':'B'},
    {'id':'','name':'Blaise Hill','pos':'PG','ht':"6'3\"",'wt':'195','age':'22','in_rat':'C','out_rat':'B','hn':'C+','df':'C+','reb':'D+','pot':'B'},
    {'id':'','name':'Jacob Murray','pos':'C','ht':"6'11\"",'wt':'245','age':'22','in_rat':'B-','out_rat':'D','hn':'D+','df':'B-','reb':'B-','pot':'B'},
    {'id':'','name':'Clinton Mercado','pos':'C','ht':"6'11\"",'wt':'235','age':'21','in_rat':'C+','out_rat':'C+','hn':'C+','df':'C+','reb':'C+','pot':'B'},
    {'id':'','name':'Emmitt Morrow','pos':'SF','ht':"6'8\"",'wt':'220','age':'21','in_rat':'C+','out_rat':'B','hn':'C+','df':'C+','reb':'C','pot':'B'},
    {'id':'','name':'Ewan Evans','pos':'PF','ht':"6'11\"",'wt':'225','age':'21','in_rat':'B-','out_rat':'C-','hn':'D+','df':'C','reb':'C+','pot':'B'},
    {'id':'','name':'Michael Thompson','pos':'PF','ht':"6'10\"",'wt':'230','age':'22','in_rat':'C+','out_rat':'C-','hn':'D','df':'B','reb':'C+','pot':'B'},
    {'id':'','name':'Owen Fisher','pos':'PF','ht':"6'11\"",'wt':'230','age':'22','in_rat':'C+','out_rat':'C+','hn':'C-','df':'C+','reb':'C+','pot':'B'},
    {'id':'','name':'Evan Cunningham','pos':'SF','ht':"6'6\"",'wt':'210','age':'21','in_rat':'C+','out_rat':'C+','hn':'C+','df':'C+','reb':'C','pot':'B'},
    {'id':'','name':'Conor Cooley','pos':'PG','ht':"6'4\"",'wt':'180','age':'22','in_rat':'C+','out_rat':'C+','hn':'B+','df':'B-','reb':'C','pot':'C'},
    {'id':'','name':'Udonavon Rose','pos':'SG','ht':"6'6\"",'wt':'210','age':'22','in_rat':'B+','out_rat':'B','hn':'B','df':'B','reb':'C+','pot':'C'},
    {'id':'','name':'Alexander Griffiths','pos':'PF','ht':"6'10\"",'wt':'235','age':'21','in_rat':'C','out_rat':'C','hn':'C-','df':'C+','reb':'B-','pot':'C'},
    {'id':'','name':'Blaine Walters','pos':'C','ht':"6'11\"",'wt':'265','age':'23','in_rat':'B-','out_rat':'C','hn':'D','df':'C+','reb':'B-','pot':'C'},
    {'id':'','name':'Tommy Lowe','pos':'C','ht':"6'10\"",'wt':'250','age':'21','in_rat':'C','out_rat':'C','hn':'D+','df':'B','reb':'B','pot':'C'},
    {'id':'','name':'Toby Odom','pos':'SF','ht':"6'7\"",'wt':'220','age':'22','in_rat':'C+','out_rat':'C','hn':'C-','df':'B+','reb':'C+','pot':'C'},
    {'id':'','name':'Jackson Elliot','pos':'SG','ht':"6'7\"",'wt':'205','age':'21','in_rat':'C','out_rat':'B','hn':'B','df':'B','reb':'C','pot':'C'},
    {'id':'','name':'Malaki Hayes','pos':'SF','ht':"6'6\"",'wt':'220','age':'22','in_rat':'C','out_rat':'B','hn':'C','df':'B','reb':'C','pot':'C'},
    {'id':'','name':'Harrison Justice','pos':'C','ht':"6'11\"",'wt':'240','age':'21','in_rat':'B','out_rat':'C','hn':'D','df':'C','reb':'C','pot':'C'},
    {'id':'','name':'Soren Harmon','pos':'SG','ht':"6'5\"",'wt':'210','age':'22','in_rat':'C+','out_rat':'B','hn':'C','df':'B','reb':'C-','pot':'C'},
    {'id':'','name':'Dominik Charles','pos':'SF','ht':"6'7\"",'wt':'215','age':'22','in_rat':'B','out_rat':'C','hn':'C','df':'B','reb':'C','pot':'C'},
    {'id':'','name':'Trent Decker','pos':'PF','ht':"6'10\"",'wt':'230','age':'23','in_rat':'C','out_rat':'C-','hn':'D+','df':'B-','reb':'C','pot':'C'},
    {'id':'','name':'Sincere Dunlap','pos':'PF','ht':"6'10\"",'wt':'250','age':'23','in_rat':'C','out_rat':'C','hn':'D+','df':'B','reb':'B','pot':'C'},
    {'id':'','name':'Marc Chaney','pos':'PF','ht':"6'10\"",'wt':'240','age':'22','in_rat':'B-','out_rat':'D+','hn':'D+','df':'C','reb':'C+','pot':'C'},
    {'id':'','name':'Cory O\'Neil','pos':'PG','ht':"6'3\"",'wt':'190','age':'21','in_rat':'C','out_rat':'C+','hn':'B','df':'C','reb':'D+','pot':'C'},
    {'id':'','name':'Zac Booth','pos':'PG','ht':"6'3\"",'wt':'185','age':'22','in_rat':'C','out_rat':'B-','hn':'B-','df':'B-','reb':'C-','pot':'C'},
    {'id':'','name':'Robert Robertson','pos':'SF','ht':"6'7\"",'wt':'215','age':'21','in_rat':'C','out_rat':'B-','hn':'C-','df':'B-','reb':'C','pot':'C'},
    {'id':'','name':'Aaron Green','pos':'SF','ht':"6'8\"",'wt':'225','age':'23','in_rat':'B-','out_rat':'B-','hn':'C-','df':'C','reb':'C','pot':'C'},
    {'id':'','name':'Ryan Mayo','pos':'SF','ht':"6'7\"",'wt':'215','age':'22','in_rat':'C+','out_rat':'C','hn':'C+','df':'C+','reb':'C','pot':'C'},
    {'id':'','name':'Kenneth Shaw','pos':'PF','ht':"6'8\"",'wt':'245','age':'23','in_rat':'C+','out_rat':'C-','hn':'D+','df':'C','reb':'C','pot':'C'},
    {'id':'','name':'Amos Oliver','pos':'PF','ht':"6'11\"",'wt':'230','age':'22','in_rat':'B','out_rat':'C-','hn':'D+','df':'C+','reb':'C+','pot':'C'},
    {'id':'','name':'Quentin Ward','pos':'SF','ht':"6'7\"",'wt':'215','age':'22','in_rat':'C','out_rat':'C','hn':'C-','df':'C','reb':'C','pot':'C'},
    {'id':'','name':'Noah Kidd','pos':'SG','ht':"6'4\"",'wt':'185','age':'22','in_rat':'C','out_rat':'C+','hn':'C','df':'B+','reb':'C-','pot':'C'},
    {'id':'','name':'Omari Bruce','pos':'C','ht':"7'0\"",'wt':'245','age':'22','in_rat':'B-','out_rat':'C+','hn':'C-','df':'C','reb':'C+','pot':'C'},
    {'id':'','name':'Tyler Foster','pos':'SG','ht':"6'6\"",'wt':'195','age':'23','in_rat':'C+','out_rat':'C','hn':'C','df':'B-','reb':'C-','pot':'C'},
    {'id':'','name':'Gaige Curtis','pos':'SF','ht':"6'7\"",'wt':'220','age':'22','in_rat':'B-','out_rat':'C','hn':'C','df':'C+','reb':'C','pot':'C'},
    {'id':'','name':'George Graham','pos':'C','ht':"6'10\"",'wt':'255','age':'22','in_rat':'C+','out_rat':'C-','hn':'C-','df':'B-','reb':'C','pot':'C'},
    {'id':'','name':'Josh Hunter','pos':'PG','ht':"6'1\"",'wt':'190','age':'23','in_rat':'C','out_rat':'C+','hn':'C+','df':'C+','reb':'D+','pot':'C'},
    {'id':'','name':'Uriah Savage','pos':'SG','ht':"6'4\"",'wt':'195','age':'21','in_rat':'C+','out_rat':'C+','hn':'C','df':'C','reb':'C','pot':'C'},
    {'id':'','name':'Danny Wheeler','pos':'SF','ht':"6'8\"",'wt':'220','age':'23','in_rat':'C+','out_rat':'C','hn':'C','df':'C+','reb':'C','pot':'C'},
    {'id':'','name':'Rex Stanley','pos':'PF','ht':"6'9\"",'wt':'235','age':'22','in_rat':'C','out_rat':'C','hn':'C-','df':'C','reb':'C','pot':'C'},
    {'id':'','name':'Asaad Stephens','pos':'C','ht':"6'11\"",'wt':'245','age':'23','in_rat':'C+','out_rat':'D+','hn':'C-','df':'C+','reb':'C','pot':'C'},
    {'id':'','name':'Jaydee McClain','pos':'C','ht':"6'9\"",'wt':'245','age':'23','in_rat':'C+','out_rat':'D+','hn':'D','df':'C','reb':'C+','pot':'C'},
    {'id':'','name':'Collin Cooke','pos':'SF','ht':"6'9\"",'wt':'220','age':'23','in_rat':'C','out_rat':'C','hn':'C','df':'B-','reb':'C','pot':'C'},
    {'id':'','name':'Zean Clarke','pos':'C','ht':"6'11\"",'wt':'240','age':'22','in_rat':'C','out_rat':'C','hn':'C-','df':'C+','reb':'C+','pot':'C'},
    {'id':'','name':'Lyonel Randolph','pos':'C','ht':"6'10\"",'wt':'260','age':'22','in_rat':'C+','out_rat':'C-','hn':'C-','df':'C','reb':'C','pot':'C'},
    {'id':'','name':'Jaivon Jacobs','pos':'C','ht':"6'10\"",'wt':'240','age':'23','in_rat':'C','out_rat':'C-','hn':'C-','df':'C+','reb':'C','pot':'C'},
    {'id':'','name':'Jaxson Cote','pos':'SG','ht':"6'6\"",'wt':'205','age':'22','in_rat':'C','out_rat':'C+','hn':'C','df':'C+','reb':'C-','pot':'C'},
    {'id':'','name':'Valentin Lane','pos':'PG','ht':"6'3\"",'wt':'185','age':'22','in_rat':'C','out_rat':'C+','hn':'C','df':'C','reb':'C-','pot':'C'},
    {'id':'','name':'Melvin Wilkins','pos':'SG','ht':"6'5\"",'wt':'200','age':'23','in_rat':'C','out_rat':'C+','hn':'B','df':'C','reb':'C-','pot':'C'},
    {'id':'','name':'Alexzander Wilson','pos':'PG','ht':"6'3\"",'wt':'190','age':'22','in_rat':'C-','out_rat':'C+','hn':'B-','df':'C','reb':'D+','pot':'D'},
    {'id':'','name':'Patrick Bell','pos':'PF','ht':"6'9\"",'wt':'240','age':'23','in_rat':'C','out_rat':'D+','hn':'C','df':'C','reb':'C','pot':'D'},
    {'id':'','name':'Bradley Powell','pos':'SG','ht':"6'6\"",'wt':'190','age':'23','in_rat':'C','out_rat':'C','hn':'C','df':'C','reb':'C-','pot':'D'},
    {'id':'','name':'Jaiden Coleman','pos':'PG','ht':"6'3\"",'wt':'180','age':'23','in_rat':'C','out_rat':'C+','hn':'C','df':'C+','reb':'D+','pot':'D'},
    {'id':'','name':'Kai Riley','pos':'SF','ht':"6'6\"",'wt':'210','age':'23','in_rat':'C','out_rat':'C+','hn':'C-','df':'C','reb':'C-','pot':'D'},
]

@app.route('/flush_salary_cache', methods=['POST'])
def flush_salary_cache():
    """Delete all cached salary roster entries so TF re-fetches fresh data from SLN."""
    try:
        conn = get_db()
        if USE_POSTGRES:
            conn.execute("DELETE FROM roster_cache WHERE team_url LIKE 'salary:%'")
            conn.commit()
        else:
            conn.execute("DELETE FROM roster_cache WHERE team_url LIKE 'salary:%'")
            conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


DRAFT_POOL_VERSION = ','.join(p['name'] for p in DRAFT_PLAYER_POOL)

@app.route('/fetch_draft_players', methods=['POST'])
def fetch_draft_players():
    players = [dict(p, id=str(i+1), out=p.get('out_rat',''), in_rating=p.get('in_rat','')) for i, p in enumerate(DRAFT_PLAYER_POOL)]
    return jsonify({'players': players, 'pool_version': DRAFT_POOL_VERSION})


@app.route('/clear_draft_notes', methods=['POST'])
def clear_draft_notes():
    db = get_db()
    row = db.execute('SELECT data FROM draft_state WHERE id = 1').fetchone()
    if row:
        data = json.loads(row[0])
        data['playerNotes'] = {}
        db.execute('UPDATE draft_state SET data=?, updated_at=CURRENT_TIMESTAMP WHERE id=1', (json.dumps(data),))
        db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/save_draft', methods=['POST'])
def save_draft():
    data = request.get_json()
    if data is None:
        return jsonify({'error': 'No data'}), 400
    db = get_db()
    db.execute('''INSERT INTO draft_state (id, data, updated_at)
                  VALUES (1, ?, CURRENT_TIMESTAMP)
                  ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at''',
               (json.dumps(data),))
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/load_draft', methods=['GET'])
def load_draft():
    db = get_db()
    row = db.execute('SELECT data FROM draft_state WHERE id = 1').fetchone()
    db.close()
    if row:
        return jsonify(json.loads(row[0]))
    return jsonify({})


LEAGUE_YEAR_DEFAULT = 2036

def get_league_year():
    try:
        db = get_db()
        row = db.execute("SELECT value FROM settings WHERE key='league_year'").fetchone()
        db.close()
        if row:
            return int(row[0])
    except Exception as e:
        app.logger.warning("non-critical error suppressed: %s", e)
    return LEAGUE_YEAR_DEFAULT


TEAM_NAME_TO_ABBR = {
    'boston celtics': 'BOS', 'miami heat': 'MIA', 'new jersey nets': 'NJN',
    'new york knicks': 'NYK', 'orlando magic': 'ORL', 'philadelphia 76ers': 'PHI',
    'washington bullets': 'WAS', 'atlanta hawks': 'ATL', 'charlotte hornets': 'CHA',
    'chicago bulls': 'CHI', 'cleveland cavaliers': 'CLE', 'detroit pistons': 'DET',
    'indiana pacers': 'IND', 'milwaukee bucks': 'MIL', 'toronto raptors': 'TOR',
    'dallas mavericks': 'DAL', 'denver nuggets': 'DEN', 'houston rockets': 'HOU',
    'minnesota timberwolves': 'MIN', 'san antonio spurs': 'SAS', 'utah jazz': 'UTA',
    'vancouver grizzlies': 'VAN', 'golden state warriors': 'GSW',
    'los angeles clippers': 'LAC', 'los angeles lakers': 'LAL', 'phoenix suns': 'PHX',
    'portland trail blazers': 'POR', 'sacramento kings': 'SAC',
    'seattle supersonics': 'SEA',
    # Short forms
    'celtics': 'BOS', 'heat': 'MIA', 'nets': 'NJN', 'knicks': 'NYK',
    'magic': 'ORL', '76ers': 'PHI', 'sixers': 'PHI', 'bullets': 'WAS', 'wizards': 'WAS',
    'hawks': 'ATL', 'hornets': 'CHA', 'bulls': 'CHI', 'cavaliers': 'CLE', 'cavs': 'CLE',
    'pistons': 'DET', 'pacers': 'IND', 'bucks': 'MIL', 'raptors': 'TOR',
    'mavericks': 'DAL', 'mavs': 'DAL', 'nuggets': 'DEN', 'rockets': 'HOU',
    'timberwolves': 'MIN', 'wolves': 'MIN', 'spurs': 'SAS', 'sa': 'SAS', 'jazz': 'UTA', 'nj': 'NJN',
    'grizzlies': 'VAN', 'warriors': 'GSW', 'clippers': 'LAC', 'lakers': 'LAL',
    'suns': 'PHX', 'trail blazers': 'POR', 'blazers': 'POR', 'kings': 'SAC',
    'supersonics': 'SEA', 'sonics': 'SEA',
}

ALL_ABBRS = set(TEAM_NAME_TO_ABBR.values())


def find_abbr(text):
    """Find a team abbreviation in a text string."""
    t = text.lower().strip()
    # Direct abbr match
    if t.upper() in ALL_ABBRS:
        return t.upper()
    # Full/partial name match (longest first)
    for name in sorted(TEAM_NAME_TO_ABBR.keys(), key=len, reverse=True):
        if name in t:
            return TEAM_NAME_TO_ABBR[name]
    return None




def parse_owed_picks_from_thread(post_text):
    """Parse owed picks from the SLN owed-picks thread first post.
    Format:
      YYYY:
      TEAM 1st to TEAM
      TEAM 2nd to TEAM [optional notes]
    Returns list of {from_abbr, year, round, to_abbr}.
    """
    owed = []
    seen = set()
    current_year = None

    def _add_swap(from_teams, rnd, qualifier, to_abbr, year):
        if len(from_teams) < 2 or not qualifier:
            return
        idx = 1 if qualifier == 'better' else 0
        from_abbr = find_abbr(from_teams[min(idx, len(from_teams) - 1)])
        swap_partner = find_abbr(from_teams[min(1 - idx, len(from_teams) - 1)])
        if from_abbr and swap_partner and from_abbr != to_abbr:
            key = (from_abbr, year, rnd, to_abbr)
            if key not in seen:
                seen.add(key)
                owed.append({
                    'from_abbr': from_abbr, 'year': year, 'round': rnd,
                    'to_abbr': to_abbr, 'qualifier': qualifier, 'swap_partner': swap_partner,
                })

    for raw_line in post_text.replace('\r', '').split('\n'):
        # Strip leading asterisk (used as bullet in some formats)
        line = raw_line.strip().lstrip('*').strip()
        if not line:
            continue

        # Year header line: "2038:" or "2038"
        year_header = re.match(r'^(20[3-9]\d)\s*:?\s*$', line)
        if year_header:
            current_year = int(year_header.group(1))
            continue

        if not current_year:
            continue

        # Combined swap format: "Better of X/Y 1st to A, Worse to B"
        # Handles single-line encoding of both sides of a pick swap.
        combined_m = re.match(r'^(better|worse)\s+of\s+', line, re.I)
        if combined_m:
            q1 = combined_m.group(1).lower()
            rest = line[combined_m.end():]
            tm = re.match(r'(.+?)\s+(1st|2nd)\s+to\s+(.+)', rest, re.I)
            if tm:
                teams_str = tm.group(1)
                rnd = 1 if tm.group(2).lower() == '1st' else 2
                remainder = tm.group(3)
                comma_parts = remainder.split(',', 1)
                sub_entries = [(q1, comma_parts[0].strip())]
                if len(comma_parts) > 1:
                    q2_m = re.match(r'\s*(worse|better)\s+to\s+(\S+)', comma_parts[1], re.I)
                    if q2_m:
                        sub_entries.append((q2_m.group(1).lower(), q2_m.group(2).strip()))
                from_teams = [t.strip() for t in teams_str.split('/') if t.strip()]
                for q, dest_raw in sub_entries:
                    to_abbr = find_abbr(dest_raw.split()[0] if dest_raw else '')
                    if to_abbr:
                        _add_swap(from_teams, rnd, q, to_abbr, current_year)
            continue

        # Must contain 1st or 2nd
        round_m = re.search(r'\b(1st|2nd)\b', line, re.I)
        if not round_m:
            continue
        rnd = 1 if round_m.group(1).lower() == '1st' else 2

        # Detect pick-swap qualifier: (Worse) / (Better)
        qualifier_m = re.search(r'\b(worse|better)\b', line, re.I)
        qualifier = qualifier_m.group(1).lower() if qualifier_m else None

        # Split on " to "
        parts = re.split(r'\s+to\s+', line, maxsplit=1, flags=re.I)
        if len(parts) < 2:
            continue

        from_part = re.sub(r'\s*\b(1st|2nd)\b.*', '', parts[0], flags=re.I).strip()
        # Strip "N of " prefix (e.g., "2 of BOS/NJ/ATL")
        from_part = re.sub(r'^\d+\s+of\s+', '', from_part, flags=re.I).strip()

        # to_part: stop at notes like "(", "*", " via ", or comma
        to_part = re.split(r'\s*[\(\*]|\s+via\s+|\s+\(', parts[1])[0].strip()
        to_part = to_part.split(',')[0].strip()

        to_abbr = find_abbr(to_part)
        if not to_abbr:
            continue

        # Handle dual-team from like "SA/MIA"
        from_teams = [t.strip() for t in from_part.split('/') if t.strip()]

        if len(from_teams) == 2 and qualifier:
            # Pick swap: "SA/MIA 1st to CHA (Worse)" / "SA/MIA 1st to ATL (Better)"
            # Worse → first team is from_abbr; Better → second team is from_abbr
            _add_swap(from_teams, rnd, qualifier, to_abbr, current_year)
        else:
            # Normal entry — one per team in the from-list
            for ft in from_teams:
                from_abbr = find_abbr(ft)
                if from_abbr and from_abbr != to_abbr:
                    key = (from_abbr, current_year, rnd, to_abbr)
                    if key not in seen:
                        seen.add(key)
                        owed.append({'from_abbr': from_abbr, 'year': current_year, 'round': rnd, 'to_abbr': to_abbr})

    return owed


@app.route('/api/status', methods=['GET'])
def api_status():
    return jsonify({'status': 'ok'})


@app.route('/api/clear_salary_cache', methods=['POST'])
def clear_salary_cache():
    try:
        conn = get_db()
        result = conn.execute("DELETE FROM roster_cache WHERE team_url LIKE 'salary:%'")
        deleted = result.rowcount
        conn.commit()
        conn.close()
        return jsonify({'deleted': deleted, 'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/picks', methods=['GET'])
def get_picks():
    db = get_db()
    row = db.execute('SELECT data, updated_at FROM owed_picks WHERE id = 1').fetchone()
    db.close()
    league_year = get_league_year()
    if row:
        return jsonify({'owed': json.loads(row[0]), 'updated_at': row[1], 'syncing': False, 'league_year': league_year})
    return jsonify({'owed': [], 'updated_at': None, 'syncing': False, 'league_year': league_year})




@app.route('/api/picks/from-paste', methods=['POST'])
def picks_from_paste():
    """Accept pasted text from the SLN owed-picks thread and save all parsed picks.
    Replaces whatever is currently stored — paste is the source of truth.
    """
    body = request.get_json() or {}
    text = (body.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'text required'}), 400

    picks = parse_owed_picks_from_thread(text)
    if not picks:
        return jsonify({'error': 'No picks found — make sure you copied the full post text'}), 400

    db = get_db()
    db.execute(
        '''INSERT INTO owed_picks (id, data, updated_at) VALUES (1, ?, CURRENT_TIMESTAMP)
               ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at''',
        (json.dumps(picks),)
    )
    db.commit()
    db.close()
    return jsonify({'ok': True, 'total': len(picks)})


@app.route('/api/picks/update', methods=['POST'])
def update_picks():
    body = request.get_json()
    owed = body.get('owed', [])
    if not isinstance(owed, list):
        return jsonify({'error': 'owed must be a list'}), 400
    db = get_db()
    db.execute(
        '''INSERT INTO owed_picks (id, data, updated_at) VALUES (1, ?, CURRENT_TIMESTAMP)
               ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at''',
        (json.dumps(owed),)
    )
    db.commit()
    db.close()
    return jsonify({'ok': True, 'count': len(owed)})


@app.route('/api/settings/league-year', methods=['GET', 'POST'])
def settings_league_year():
    if request.method == 'GET':
        return jsonify({'league_year': get_league_year()})
    body = request.get_json() or {}
    year = body.get('year')
    if not isinstance(year, int) or year < 2020 or year > 2060:
        return jsonify({'error': 'Invalid year'}), 400
    db = get_db()
    db.execute("INSERT INTO settings (key, value) VALUES ('league_year', ?) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (str(year),))
    db.commit()
    db.close()
    return jsonify({'ok': True, 'league_year': year})


@app.route('/api/camp-history', methods=['GET'])
def camp_history():
    global _camp_cache, _camp_cache_time
    now = time.time()
    if _camp_cache is not None and now - _camp_cache_time < _CAMP_CACHE_TTL:
        return jsonify(_camp_cache)
    try:
        resp = _fetch_url(_SHEETS_CSV_URL, timeout=15)
        resp.raise_for_status()
        players = _parse_camp_csv(resp.text)
        _camp_cache      = {'players': players}
        _camp_cache_time = now
        return jsonify(_camp_cache)
    except Exception as e:
        return jsonify({'error': str(e), 'players': {}}), 500


@app.route('/api/stat-book', methods=['GET'])
def stat_book():
    global _stat_cache, _stat_cache_time
    now = time.time()
    if _stat_cache is not None and now - _stat_cache_time < _STAT_CACHE_TTL:
        return jsonify(_stat_cache)
    try:
        resp = _fetch_url(_STAT_BOOK_URL, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        tag = soup.find('script', {'type': 'application/json'})
        if not tag:
            return jsonify({'error': 'data not found', 'seasons': [], 'players': []}), 500
        data = json.loads(tag.string)
        _stat_cache      = data
        _stat_cache_time = now
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e), 'seasons': [], 'players': []}), 500


def _get_camp_data():
    """Return cached camp data, refreshing if stale."""
    global _camp_cache, _camp_cache_time
    now = time.time()
    if _camp_cache is not None and now - _camp_cache_time < _CAMP_CACHE_TTL:
        return _camp_cache.get('players', {})
    try:
        resp = _fetch_url(_SHEETS_CSV_URL, timeout=15)
        resp.raise_for_status()
        players = _parse_camp_csv(resp.text)
        _camp_cache      = {'players': players}
        _camp_cache_time = now
        return players
    except Exception:
        return (_camp_cache or {}).get('players', {})


def _build_camp_context(camp_players):
    """Serialize camp data into compact text for LLM context."""
    if not camp_players:
        return ''
    lines = ['--- PLAYER CAMP DATA (training camp ratings by year) ---']
    for key in sorted(camp_players.keys()):
        p = camp_players[key]
        name = p.get('name', key)
        entries = p.get('entries', [])
        total = p.get('total')
        if not entries and not total:
            continue
        parts = [f'{e["year"]}:{e["rating"]}' for e in entries]
        line = f'{name}: {", ".join(parts)}'
        if total:
            line += f' | Total: {total}'
        lines.append(line)
    return '\n'.join(lines)


def _get_stat_data():
    """Return cached stat book data, refreshing if stale."""
    global _stat_cache, _stat_cache_time
    now = time.time()
    if _stat_cache is not None and now - _stat_cache_time < _STAT_CACHE_TTL:
        return _stat_cache
    try:
        resp = _fetch_url(_STAT_BOOK_URL, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        tag = soup.find('script', {'type': 'application/json'})
        if tag:
            data = json.loads(tag.string)
            _stat_cache      = data
            _stat_cache_time = now
            return data
    except Exception:
        pass
    return _stat_cache or {}


def _build_stat_context(data):
    """Build comprehensive stat book context for LLM — all rings, all awards, all careers."""
    from collections import defaultdict

    players_raw = data.get('players', {})
    players = list(players_raw.values()) if isinstance(players_raw, dict) else players_raw
    seasons  = data.get('seasons', [])
    champs   = data.get('champs', {})

    season_year = {s['key']: s['order'] for s in seasons}

    # Championship rings — every player on every championship roster
    ring_map = defaultdict(list)
    for sk, rn in champs.items():
        year = season_year.get(sk, sk)
        for p in players:
            if p.get('season') == sk and p.get('rn') == rn:
                ring_map[p['name']].append(year)

    # Award history — season by season (all seasons, all awards)
    award_by_season = defaultdict(dict)
    award_career    = defaultdict(lambda: defaultdict(int))
    for p in players:
        for aw in (p.get('awards') or []):
            yr = season_year.get(p.get('season'), p.get('season'))
            award_by_season[yr].setdefault(aw, []).append(p['name'])
            award_career[p['name']][aw] += 1

    # Career stats + team history — every player with 2+ seasons or any ring/award
    career       = defaultdict(lambda: {'g': 0, 'pts': 0, 'reb': 0, 'ast': 0, 'stl': 0, 'blk': 0, 'seasons': 0})
    team_history = defaultdict(list)   # name -> [(year, team), ...]
    for p in players:
        if p.get('season') == 'current':
            continue
        n = p['name']
        g = p.get('g', 0) or 0
        yr = season_year.get(p.get('season'), p.get('season'))
        team = p.get('team', '')
        career[n]['g']       += g
        career[n]['pts']     += (p.get('ppg') or 0) * g
        career[n]['reb']     += (p.get('rpg') or 0) * g
        career[n]['ast']     += (p.get('apg') or 0) * g
        career[n]['stl']     += (p.get('spg') or 0) * g
        career[n]['blk']     += (p.get('bpg') or 0) * g
        career[n]['seasons'] += 1
        team_history[n].append((yr, team))

    def cavg(n, stat):
        g = career[n]['g']
        return round(career[n][stat] / g, 1) if g else 0

    lines = []

    lines.append('=== SIM LEAGUE NIRVANA (SLN) STAT BOOK ===')
    played = [s for s in reversed(seasons) if s.get('played')]
    lines.append(f'Seasons covered: {played[-1]["order"] if played else "?"} – {played[0]["order"] if played else "?"}')
    lines.append('')

    # ── Championship rings (ALL ring holders) ──
    lines.append('--- CHAMPIONSHIP RINGS (all players, most rings first) ---')
    for name, years in sorted(ring_map.items(), key=lambda x: (-len(x[1]), x[0])):
        lines.append(f'{name}: {len(years)} ({",".join(str(y) for y in sorted(years))})')
    lines.append('')

    # ── Season-by-season award history (every season, every award) ──
    lines.append('--- SEASON-BY-SEASON AWARDS ---')
    for yr in sorted(award_by_season.keys()):
        parts = [f'{aw}:{",".join(names)}' for aw, names in sorted(award_by_season[yr].items())]
        lines.append(f'{yr}: {" | ".join(parts)}')
    lines.append('')

    # ── Award career totals (all winners, sorted by count) ──
    lines.append('--- ALL-TIME AWARD COUNTS ---')
    award_types = ['MVP', 'DPOY', 'ROY', '6th Man',
                   'All-League 1st', 'All-League 2nd', 'All-League 3rd',
                   'All-Defensive 1st', 'All-Defensive 2nd',
                   'All-Rookie 1st', 'All-Rookie 2nd']
    for aw in award_types:
        winners = sorted(
            [(n, c[aw]) for n, c in award_career.items() if c.get(aw, 0) > 0],
            key=lambda x: -x[1]
        )
        lines.append(f'{aw}: {", ".join(f"{n}({c})" for n,c in winners)}')
    lines.append('')

    # ── Per-team history: stats and awards earned WHILE on each team ──
    # (replaces career summaries — this is the accurate per-franchise view)
    team_player_stats = defaultdict(lambda: defaultdict(
        lambda: {'g': 0, 'pts': 0, 'reb': 0, 'ast': 0, 'seasons': 0, 'awards': [], 'years': []}
    ))
    for p in players:
        if p.get('season') == 'current':
            continue
        team = p.get('team', '')
        name = p['name']
        yr   = season_year.get(p.get('season'), p.get('season'))
        g    = p.get('g', 0) or 0
        team_player_stats[team][name]['g']       += g
        team_player_stats[team][name]['pts']     += (p.get('ppg') or 0) * g
        team_player_stats[team][name]['reb']     += (p.get('rpg') or 0) * g
        team_player_stats[team][name]['ast']     += (p.get('apg') or 0) * g
        team_player_stats[team][name]['seasons'] += 1
        team_player_stats[team][name]['years'].append(yr)
        for aw in (p.get('awards') or []):
            team_player_stats[team][name]['awards'].append(f'{aw}({yr})')

    team_champs = defaultdict(list)
    for sk, rn in champs.items():
        yr = season_year.get(sk, sk)
        for p in players:
            if p.get('season') == sk and p.get('rn') == rn:
                team_champs[p.get('team', '')].append(yr)
                break

    lines.append('--- TEAM HISTORIES (stats/awards earned while on each team) ---')
    for team in sorted(team_player_stats.keys()):
        champ_years = sorted(set(team_champs.get(team, [])))
        champ_str = f' Championships:{",".join(str(y) for y in champ_years)}' if champ_years else ''
        lines.append(f'{team}|{champ_str}')
        roster = team_player_stats[team]
        # Top 12 by PPG while on this team; require at least 41 games (half a season)
        top = sorted(
            [(pn, s) for pn, s in roster.items() if s['g'] >= 41],
            key=lambda x: -(x[1]['pts'] / x[1]['g'])
        )[:12]
        for pname, s in top:
            ppg = round(s['pts'] / s['g'], 1)
            rpg = round(s['reb'] / s['g'], 1)
            apg = round(s['ast'] / s['g'], 1)
            yr_range = f'{min(s["years"])}-{max(s["years"])}'
            ring_here = [y for y in champ_years if y in s['years']]
            ring_str  = f' ring:{",".join(str(y) for y in ring_here)}' if ring_here else ''
            # Deduplicate awards and keep compact
            aw_dedup = sorted(set(s['awards']))
            aw_str   = f' aw:{",".join(aw_dedup)}' if aw_dedup else ''
            lines.append(f' {pname}({yr_range}):{ppg}/{rpg}/{apg} {int(s["g"])}G{ring_str}{aw_str}')
    lines.append('')

    # ── Current season rosters ──
    current = [p for p in players if p.get('season') == 'current']
    if current:
        lines.append('--- CURRENT SEASON ROSTERS ---')
        by_team = defaultdict(list)
        for p in current:
            by_team[p.get('team', '?')].append(f'{p["name"]}({p.get("ppg",0):.1f}ppg)')
        for team in sorted(by_team):
            lines.append(f'{team}: {", ".join(by_team[team])}')

    return '\n'.join(lines)


def _build_player_maps(players):
    """Build name→token and token→name maps using stat book player IDs."""
    name_to_token = {}
    token_to_name = {}
    for p in players:
        name = p.get('name', '').strip()
        pid  = p.get('id')
        if name and pid and name not in name_to_token:
            token = f'PLYR{pid}'
            name_to_token[name] = token
            token_to_name[token] = name
    return name_to_token, token_to_name


def _anonymize(text, name_to_token):
    """Replace player names with PLYR{id} tokens (longest names first)."""
    for name in sorted(name_to_token.keys(), key=len, reverse=True):
        text = text.replace(name, name_to_token[name])
    return text


def _deanonymize(text, token_to_name):
    """Replace PLYR{id} tokens back with real player names."""
    return re.sub(r'PLYR\d+', lambda m: token_to_name.get(m.group(0), m.group(0)), text)


@app.route('/api/faq-query', methods=['POST'])
def faq_query():
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'ANTHROPIC_API_KEY not configured'}), 503

    body = request.get_json(silent=True) or {}
    question   = (body.get('question') or '').strip()
    faq_rules  = (body.get('faq_rules') or '').strip()
    if not question:
        return jsonify({'error': 'No question provided'}), 400

    data = _get_stat_data()
    if not data:
        return jsonify({'error': 'Stat book data unavailable'}), 503

    # Build anonymization maps from stat book player list
    players_raw = data.get('players', {})
    all_players = list(players_raw.values()) if isinstance(players_raw, dict) else players_raw
    name_to_token, token_to_name = _build_player_maps(all_players)

    stat_context = _anonymize(_build_stat_context(data), name_to_token)
    camp_context = _anonymize(_build_camp_context(_get_camp_data()), name_to_token)

    user_content = ''
    if faq_rules:
        user_content += f'=== SLN LEAGUE RULES & FAQ ===\n{faq_rules}\n\n'
    user_content += f'=== SLN STAT BOOK ===\n{stat_context}\n\n'
    if camp_context:
        user_content += f'{camp_context}\n\n'
    user_content += f'Question: {question}'

    system_msg = (
        'You are the assistant for SLN (Sim League Nirvana), a fantasy basketball simulation league. '
        'Every question is about SLN. You have three equally authoritative sources — use ALL of them:\n'
        '1. SLN LEAGUE RULES & FAQ: league rules, salary cap, trades, free agency, draft, season format, dues, etc.\n'
        '2. SLN STAT BOOK: every player career, team history, championships, awards, and game stats.\n'
        '   Player names are replaced with PLYR{id} tokens — use these tokens in your answer.\n'
        '3. PLAYER CAMP DATA: training camp ratings and development history by year for each player.\n'
        'Answer using only the provided data. If something is not in the data, say: "I don\'t see that in the SLN data."'
    )

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-sonnet-4-5',
            max_tokens=600,
            system=system_msg,
            messages=[{'role': 'user', 'content': user_content}]
        )
        answer = _deanonymize(msg.content[0].text, token_to_name)
        return jsonify({'answer': answer})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
