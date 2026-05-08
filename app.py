from flask import Flask, render_template, request, jsonify, make_response
import os
import requests
from bs4 import BeautifulSoup
import re
import sqlite3
import json
import threading
from datetime import datetime, timedelta

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = timedelta(days=7)

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
            self._cur.execute(sql, params)
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
    resp = make_response(render_template('index.html', version=get_version()))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp

@app.route('/mockups')
def mockups():
    return render_template('mockups.html')

@app.route('/mockup-team-select')
def mockup_team_select():
    return render_template('mockup_team_select.html')


@app.route('/fetch_roster', methods=['POST'])
def fetch_roster():
    url = request.json.get('url', '').strip()
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
        except Exception:
            pass

        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            )
        }
        resp = requests.get(url, timeout=15, headers=headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        # Try to get team name from page title or heading
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

            # Scan through rows to find the actual column header row
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

            # Identify the abilities table by required columns
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
                }

                elig = compute_eligibility(player)
                player['eligible_starter'] = elig['starter']
                player['eligible_backup'] = elig['backup']

                players.append(player)

            if players:
                break

        if not players:
            return jsonify({
                'error': 'Could not find a player abilities table on this page. '
                         'Check the URL and make sure the page is accessible.'
            }), 400

        # Scrape Player Statistics table (PPG, RPG, APG, SPG, BPG, TPG, FG%, FT%, 3P%)
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

        # Sort by position order then name
        pos_order = {'PG': 0, 'SG': 1, 'SF': 2, 'PF': 3, 'C': 4}
        players.sort(key=lambda p: (pos_order.get(p['pos'], 5), p['name']))

        result = {'players': players, 'team_name': team_name}

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
        except Exception:
            pass

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
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            )
        }
        resp = requests.get(url, timeout=15, headers=headers)
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


@app.route('/fetch_salary_roster', methods=['POST'])
def fetch_salary_roster():
    url = request.json.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    try:
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            )
        }
        resp = requests.get(url, timeout=15, headers=headers)
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
        return jsonify({'players': players, 'team_name': team_name, 'total_salary': total_salary})

    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out.'}), 400
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to fetch page: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': f'Error parsing salary data: {str(e)}'}), 500


# ── Static draft player pool (snapshot: 2026-05-04) ─────────────────────────
# To update this list, re-fetch https://www.simleaguenirvana.com/draft/draftplayers-pot.htm
DRAFT_PLAYER_POOL = [
    # Order matches https://www.simleaguenirvana.com/draft/draftplayers-pot.htm (snapshot: 2026-05-05)
    {'id':'','name':'Legend Gracey',      'pos':'PF','ht':"6'7\"" ,'wt':'225','age':'21','in_rat':'B+','out_rat':'C' ,'hn':'B' ,'df':'C-','reb':'C' ,'pot':'A'},
    {'id':'','name':'Chief Peace',        'pos':'SG','ht':"6'4\"" ,'wt':'190','age':'20','in_rat':'C' ,'out_rat':'C' ,'hn':'B+','df':'C' ,'reb':'C' ,'pot':'A'},
    {'id':'','name':'Messiah Gregory',    'pos':'PG','ht':"6'2\"" ,'wt':'185','age':'20','in_rat':'B' ,'out_rat':'D+','hn':'C' ,'df':'B' ,'reb':'C+','pot':'B'},
    {'id':'','name':'Kobe Corbo-Banks',   'pos':'SG','ht':"6'5\"" ,'wt':'200','age':'20','in_rat':'C' ,'out_rat':'B' ,'hn':'C' ,'df':'C' ,'reb':'C-','pot':'B'},
    {'id':'','name':'Future McConnell',   'pos':'SF','ht':"6'8\"" ,'wt':'215','age':'20','in_rat':'B' ,'out_rat':'B' ,'hn':'C-','df':'B' ,'reb':'C+','pot':'B'},
    {'id':'','name':'Krizzio Eustaquio',  'pos':'SF','ht':"6'6\"" ,'wt':'210','age':'21','in_rat':'B-','out_rat':'B-','hn':'C-','df':'C+','reb':'C' ,'pot':'B'},
    {'id':'','name':'Che Ali Griffith',   'pos':'C' ,'ht':"6'10\"",'wt':'240','age':'20','in_rat':'B' ,'out_rat':'C+','hn':'D+','df':'B' ,'reb':'C+','pot':'B'},
    {'id':'','name':'King Vincent',       'pos':'C' ,'ht':"6'10\"",'wt':'230','age':'21','in_rat':'C+','out_rat':'C' ,'hn':'D' ,'df':'C' ,'reb':'B' ,'pot':'B'},
    {'id':'','name':'Justus Thomas',      'pos':'PF','ht':"6'8\"" ,'wt':'245','age':'21','in_rat':'C+','out_rat':'B' ,'hn':'C' ,'df':'C+','reb':'C+','pot':'B'},
    {'id':'','name':'Kye Branson',        'pos':'PG','ht':"6'2\"" ,'wt':'180','age':'20','in_rat':'C' ,'out_rat':'B' ,'hn':'B' ,'df':'B-','reb':'C-','pot':'B'},
    {'id':'','name':'Cash Rader',         'pos':'SF','ht':"6'7\"" ,'wt':'225','age':'20','in_rat':'C+','out_rat':'B+','hn':'C' ,'df':'B-','reb':'C' ,'pot':'B'},
    {'id':'','name':'Zaiden Middleton',   'pos':'PG','ht':"6'3\"" ,'wt':'200','age':'21','in_rat':'C' ,'out_rat':'B-','hn':'C+','df':'B' ,'reb':'C-','pot':'B'},
    {'id':'','name':'Hezekiah McGrew',    'pos':'PF','ht':"6'9\"" ,'wt':'230','age':'20','in_rat':'C' ,'out_rat':'C' ,'hn':'C-','df':'B' ,'reb':'B-','pot':'B'},
    {'id':'','name':'Scooby Harris',      'pos':'SG','ht':"6'7\"" ,'wt':'190','age':'21','in_rat':'B' ,'out_rat':'C' ,'hn':'C+','df':'C+','reb':'C-','pot':'B'},
    {'id':'','name':'Cainen Bell',        'pos':'C' ,'ht':"7'0\"" ,'wt':'240','age':'22','in_rat':'C+','out_rat':'D+','hn':'D+','df':'B+','reb':'B-','pot':'B'},
    {'id':'','name':'Judah Butler',       'pos':'SG','ht':"6'5\"" ,'wt':'190','age':'21','in_rat':'C' ,'out_rat':'C+','hn':'C+','df':'C+','reb':'C' ,'pot':'B'},
    {'id':'','name':'Tazwell Lewis',      'pos':'SF','ht':"6'8\"" ,'wt':'225','age':'23','in_rat':'C' ,'out_rat':'B-','hn':'C-','df':'B-','reb':'C' ,'pot':'B'},
    {'id':'','name':'Austin Cineas',      'pos':'C' ,'ht':"6'10\"",'wt':'235','age':'22','in_rat':'C+','out_rat':'B+','hn':'D+','df':'C+','reb':'B' ,'pot':'B'},
    {'id':'','name':'Jaxsen Jollif',      'pos':'SF','ht':"6'7\"" ,'wt':'220','age':'22','in_rat':'B-','out_rat':'C' ,'hn':'C-','df':'B' ,'reb':'C' ,'pot':'B'},
    {'id':'','name':'Grady Levine',       'pos':'PG','ht':"6'3\"" ,'wt':'190','age':'21','in_rat':'C' ,'out_rat':'C+','hn':'B+','df':'C' ,'reb':'C-','pot':'B'},
    {'id':'','name':'Edward Houghton',    'pos':'PF','ht':"6'8\"" ,'wt':'235','age':'22','in_rat':'C+','out_rat':'C-','hn':'C-','df':'C+','reb':'C' ,'pot':'B'},
    {'id':'','name':'Sam Hill',           'pos':'PF','ht':"6'9\"" ,'wt':'235','age':'22','in_rat':'B-','out_rat':'B-','hn':'C-','df':'C+','reb':'C' ,'pot':'B'},
    {'id':'','name':"D'Ante Fields",      'pos':'C' ,'ht':"7'1\"" ,'wt':'255','age':'22','in_rat':'B-','out_rat':'D' ,'hn':'D-','df':'B' ,'reb':'C' ,'pot':'B'},
    {'id':'','name':'Enzo Pettigrew',     'pos':'SG','ht':"6'6\"" ,'wt':'205','age':'21','in_rat':'C' ,'out_rat':'B-','hn':'C' ,'df':'C+','reb':'C' ,'pot':'B'},
    {'id':'','name':'Kian Baxter',        'pos':'PG','ht':"6'4\"" ,'wt':'210','age':'22','in_rat':'C+','out_rat':'C' ,'hn':'B-','df':'C' ,'reb':'C' ,'pot':'B'},
    {'id':'','name':'Aquarius Rhodes Jr.','pos':'PG','ht':"6'2\"" ,'wt':'195','age':'21','in_rat':'C' ,'out_rat':'C+','hn':'B' ,'df':'C+','reb':'C-','pot':'B'},
    {'id':'','name':'Oscar Moss',         'pos':'SF','ht':"6'7\"" ,'wt':'220','age':'22','in_rat':'C+','out_rat':'C' ,'hn':'C-','df':'C+','reb':'C+','pot':'B'},
    {'id':'','name':'Kaijin Nelson',      'pos':'PF','ht':"6'11\"",'wt':'225','age':'22','in_rat':'C' ,'out_rat':'C-','hn':'C-','df':'B' ,'reb':'C+','pot':'B'},
    {'id':'','name':'Joe Powell',         'pos':'PG','ht':"6'3\"" ,'wt':'185','age':'22','in_rat':'C-','out_rat':'C+','hn':'C' ,'df':'C+','reb':'C-','pot':'C'},
    {'id':'','name':'Jazeel Kelley',      'pos':'SG','ht':"6'6\"" ,'wt':'190','age':'23','in_rat':'C' ,'out_rat':'C+','hn':'C-','df':'C+','reb':'C-','pot':'C'},
    {'id':'','name':'Boban Nastasic',     'pos':'SG','ht':"6'6\"" ,'wt':'215','age':'21','in_rat':'C' ,'out_rat':'B+','hn':'B+','df':'B+','reb':'C-','pot':'C'},
    {'id':'','name':'Sabian Carr',        'pos':'SF','ht':"6'8\"" ,'wt':'225','age':'22','in_rat':'C' ,'out_rat':'B+','hn':'C-','df':'B+','reb':'C' ,'pot':'C'},
    {'id':'','name':'Shakur Mathis',      'pos':'C' ,'ht':"6'10\"",'wt':'240','age':'22','in_rat':'C+','out_rat':'C-','hn':'D+','df':'B-','reb':'C+','pot':'C'},
    {'id':'','name':'Davarius Daniels',   'pos':'C' ,'ht':"6'11\"",'wt':'245','age':'22','in_rat':'C+','out_rat':'C+','hn':'C-','df':'C+','reb':'C+','pot':'C'},
    {'id':'','name':'Kian Gomez',         'pos':'PF','ht':"6'10\"",'wt':'230','age':'22','in_rat':'B' ,'out_rat':'C+','hn':'C-','df':'C' ,'reb':'C+','pot':'C'},
    {'id':'','name':'Ray Crane',          'pos':'SF','ht':"6'6\"" ,'wt':'220','age':'22','in_rat':'B-','out_rat':'B-','hn':'C' ,'df':'C' ,'reb':'C' ,'pot':'C'},
    {'id':'','name':'Spencer Fox',        'pos':'PF','ht':"6'9\"" ,'wt':'230','age':'23','in_rat':'C' ,'out_rat':'B' ,'hn':'C-','df':'B' ,'reb':'B-','pot':'C'},
    {'id':'','name':'Tevari Henry',       'pos':'C' ,'ht':"6'10\"",'wt':'235','age':'24','in_rat':'B-','out_rat':'D' ,'hn':'D' ,'df':'C+','reb':'C' ,'pot':'C'},
    {'id':'','name':'Navier Webb',        'pos':'C' ,'ht':"6'11\"",'wt':'260','age':'22','in_rat':'C' ,'out_rat':'C' ,'hn':'D' ,'df':'C+','reb':'C+','pot':'C'},
    {'id':'','name':'Jenson Stevens',     'pos':'PG','ht':"6'1\"" ,'wt':'175','age':'23','in_rat':'D+','out_rat':'C+','hn':'C' ,'df':'C+','reb':'D' ,'pot':'C'},
    {'id':'','name':'Colin Knight',       'pos':'SG','ht':"6'6\"" ,'wt':'195','age':'21','in_rat':'C' ,'out_rat':'C' ,'hn':'C' ,'df':'C' ,'reb':'C-','pot':'C'},
    {'id':'','name':'Callum Khan',        'pos':'SF','ht':"6'7\"" ,'wt':'225','age':'23','in_rat':'B-','out_rat':'C-','hn':'C' ,'df':'C' ,'reb':'C+','pot':'C'},
    {'id':'','name':'Brodie Rowland',     'pos':'SG','ht':"6'5\"" ,'wt':'200','age':'21','in_rat':'B' ,'out_rat':'C+','hn':'C' ,'df':'C+','reb':'D+','pot':'C'},
    {'id':'','name':'Keylon Poole',       'pos':'SF','ht':"6'9\"" ,'wt':'215','age':'22','in_rat':'C' ,'out_rat':'C' ,'hn':'C' ,'df':'C+','reb':'C' ,'pot':'C'},
    {'id':'','name':'Otto Chaney',        'pos':'SF','ht':"6'9\"" ,'wt':'215','age':'22','in_rat':'C' ,'out_rat':'B-','hn':'D+','df':'C' ,'reb':'C' ,'pot':'C'},
    {'id':'','name':'Sawyer Harrison',    'pos':'PF','ht':"6'9\"" ,'wt':'230','age':'23','in_rat':'C+','out_rat':'C+','hn':'C-','df':'C' ,'reb':'C' ,'pot':'C'},
    {'id':'','name':'Destin Greer',       'pos':'C' ,'ht':"6'11\"",'wt':'225','age':'23','in_rat':'C' ,'out_rat':'B' ,'hn':'C-','df':'C' ,'reb':'C+','pot':'C'},
    {'id':'','name':'Abram Miranda',      'pos':'SF','ht':"6'8\"" ,'wt':'230','age':'22','in_rat':'C' ,'out_rat':'B' ,'hn':'C' ,'df':'C+','reb':'C' ,'pot':'C'},
    {'id':'','name':'Arthur Andrews',     'pos':'PF','ht':"6'10\"",'wt':'220','age':'22','in_rat':'C' ,'out_rat':'C' ,'hn':'C-','df':'B-','reb':'C+','pot':'C'},
    {'id':'','name':'Zalen Bass',         'pos':'C' ,'ht':"6'11\"",'wt':'245','age':'22','in_rat':'C+','out_rat':'C-','hn':'D' ,'df':'C+','reb':'B-','pot':'C'},
    {'id':'','name':'Tommy Waters',       'pos':'PF','ht':"6'9\"" ,'wt':'230','age':'22','in_rat':'C+','out_rat':'C-','hn':'D+','df':'C' ,'reb':'C+','pot':'C'},
    {'id':'','name':'Kieran Reed',        'pos':'PG','ht':"6'2\"" ,'wt':'180','age':'23','in_rat':'C' ,'out_rat':'C+','hn':'C' ,'df':'C' ,'reb':'C-','pot':'C'},
    {'id':'','name':'Randall Golden',     'pos':'SG','ht':"6'6\"" ,'wt':'200','age':'21','in_rat':'C' ,'out_rat':'B' ,'hn':'C-','df':'C' ,'reb':'C-','pot':'C'},
    {'id':'','name':'Everett Farley',     'pos':'PG','ht':"6'4\"" ,'wt':'190','age':'23','in_rat':'C' ,'out_rat':'C' ,'hn':'C+','df':'C+','reb':'D+','pot':'C'},
    {'id':'','name':'Jon Harris',         'pos':'SF','ht':"6'6\"" ,'wt':'210','age':'22','in_rat':'C' ,'out_rat':'C' ,'hn':'C' ,'df':'C+','reb':'C' ,'pot':'C'},
    {'id':'','name':'Byron Cross',        'pos':'PF','ht':"6'10\"",'wt':'240','age':'22','in_rat':'C' ,'out_rat':'C-','hn':'D+','df':'C+','reb':'B-','pot':'C'},
    {'id':'','name':'Kohen Torres',       'pos':'PG','ht':"6'2\"" ,'wt':'190','age':'23','in_rat':'C' ,'out_rat':'B-','hn':'B' ,'df':'C' ,'reb':'C-','pot':'C'},
    {'id':'','name':'Bo Becker',          'pos':'SG','ht':"6'7\"" ,'wt':'210','age':'22','in_rat':'B-','out_rat':'C+','hn':'C' ,'df':'C+','reb':'C-','pot':'C'},
    {'id':'','name':'Dashaud Parker',     'pos':'SF','ht':"6'6\"" ,'wt':'220','age':'22','in_rat':'C' ,'out_rat':'C' ,'hn':'C' ,'df':'C' ,'reb':'C' ,'pot':'C'},
    {'id':'','name':'Jevonne Bishop',     'pos':'C' ,'ht':"6'11\"",'wt':'230','age':'22','in_rat':'C' ,'out_rat':'C-','hn':'C-','df':'C' ,'reb':'C+','pot':'C'},
    {'id':'','name':'Takeo Calhoun',      'pos':'C' ,'ht':"6'9\"" ,'wt':'255','age':'23','in_rat':'C+','out_rat':'C-','hn':'C-','df':'C+','reb':'C' ,'pot':'C'},
    {'id':'','name':'Leon Harris',        'pos':'PG','ht':"6'3\"" ,'wt':'175','age':'22','in_rat':'C' ,'out_rat':'C+','hn':'C' ,'df':'C' ,'reb':'D+','pot':'C'},
    {"id":'','name':"Ka'jai Dorsey",      'pos':'SG','ht':"6'6\"" ,'wt':'200','age':'22','in_rat':'C' ,'out_rat':'B+','hn':'C' ,'df':'C' ,'reb':'C-','pot':'C'},
    {'id':'','name':'Killion Mack',       'pos':'C' ,'ht':"6'10\"",'wt':'240','age':'23','in_rat':'C' ,'out_rat':'D+','hn':'D' ,'df':'C+','reb':'C+','pot':'D'},
    {'id':'','name':'Jamyron Wheeler',    'pos':'SF','ht':"6'8\"" ,'wt':'215','age':'23','in_rat':'C' ,'out_rat':'C' ,'hn':'C-','df':'C' ,'reb':'C-','pot':'D'},
    {'id':'','name':'Ladarrell Reed',     'pos':'C' ,'ht':"7'0\"" ,'wt':'250','age':'22','in_rat':'C' ,'out_rat':'D+','hn':'C-','df':'C' ,'reb':'C+','pot':'D'},
    {'id':'','name':'Kaleem Barnes',      'pos':'SF','ht':"6'7\"" ,'wt':'210','age':'23','in_rat':'C' ,'out_rat':'C+','hn':'D' ,'df':'C' ,'reb':'C' ,'pot':'D'},
    {'id':'','name':'Shawn Randolph',     'pos':'PF','ht':"6'9\"" ,'wt':'230','age':'23','in_rat':'C' ,'out_rat':'C-','hn':'D+','df':'C+','reb':'C' ,'pot':'D'},
    {'id':'','name':'Harvey Burke',       'pos':'PF','ht':"6'10\"",'wt':'225','age':'22','in_rat':'C' ,'out_rat':'C-','hn':'D+','df':'C' ,'reb':'C' ,'pot':'D'},
    {'id':'','name':'Kyler Goodwin',      'pos':'PG','ht':"6'0\"" ,'wt':'165','age':'23','in_rat':'D+','out_rat':'C' ,'hn':'B+','df':'C-','reb':'D' ,'pot':'D'},
    {'id':'','name':'Troy Allison',       'pos':'SG','ht':"6'4\"" ,'wt':'190','age':'22','in_rat':'C' ,'out_rat':'C' ,'hn':'C' ,'df':'C' ,'reb':'C-','pot':'D'},
]

@app.route('/fetch_draft_players', methods=['POST'])
def fetch_draft_players():
    players = [dict(p, id=str(i+1), out=p.get('out_rat',''), in_rating=p.get('in_rat','')) for i, p in enumerate(DRAFT_PLAYER_POOL)]
    return jsonify({'players': players})


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


SLN_THREAD_URL = 'https://simleaguenirvana.com/viewtopic.php?t=18'

LEAGUE_YEAR = 2035
ROSTER_PICK_YEARS = [LEAGUE_YEAR + 1, LEAGUE_YEAR + 2]   # 2036, 2037
FORUM_PICK_YEARS  = list(range(LEAGUE_YEAR + 3, LEAGUE_YEAR + 7))  # 2038-2041

ROSTER_MAP = {
    'roster1.htm': 'BOS', 'roster2.htm': 'MIA', 'roster3.htm': 'NJN',
    'roster4.htm': 'NYK', 'roster5.htm': 'ORL', 'roster6.htm': 'PHI',
    'roster7.htm': 'WAS', 'roster8.htm': 'ATL', 'roster9.htm': 'CHA',
    'roster10.htm': 'CHI', 'roster11.htm': 'CLE', 'roster12.htm': 'DET',
    'roster13.htm': 'IND', 'roster14.htm': 'MIL', 'roster15.htm': 'TOR',
    'roster16.htm': 'DAL', 'roster17.htm': 'DEN', 'roster18.htm': 'HOU',
    'roster19.htm': 'MIN', 'roster20.htm': 'SAS', 'roster21.htm': 'UTA',
    'roster22.htm': 'VAN', 'roster23.htm': 'GSW', 'roster24.htm': 'LAC',
    'roster25.htm': 'LAL', 'roster26.htm': 'PHX', 'roster27.htm': 'POR',
    'roster28.htm': 'SAC', 'roster29.htm': 'SEA',
}
ROSTER_BASE = 'https://www.simleaguenirvana.com/rosters/'

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
    'timberwolves': 'MIN', 'wolves': 'MIN', 'spurs': 'SAS', 'sa': 'SAS', 'jazz': 'UTA',
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


def parse_roster_draft_picks(html, owner_abbr, target_years):
    """Parse Draft Picks table from a roster page.
    Returns list of {year, round, original_abbr} — picks the owner currently holds.
    """
    soup = BeautifulSoup(html, 'html.parser')
    picks = []

    # Find the anchor/element that is INSIDE the Draft Picks table.
    # The pattern on SLN roster pages is: <a name="draft">Draft Picks</a>
    # which is inside the first <td> of the table — so we use find_parent('table').
    draft_anchor = soup.find('a', attrs={'name': 'draft'})
    if draft_anchor:
        table = draft_anchor.find_parent('table')
    else:
        # Fallback: find first <td>/<th> whose text is "Draft Picks" and get its parent table
        table = None
        for tag in soup.find_all(['td', 'th', 'b', 'strong', 'u']):
            if tag.get_text(strip=True).lower() == 'draft picks':
                table = tag.find_parent('table')
                if table:
                    break
    if not table:
        return picks

    rows = table.find_all('tr')
    if len(rows) < 2:
        return picks

    # Skip the title row ("Draft Picks") — find the row that has year headers
    year_col_map = {}  # col_index -> year
    year_row_idx = None
    for ri, row in enumerate(rows):
        col = 0
        found = {}
        for cell in row.find_all(['th', 'td']):
            colspan = int(cell.get('colspan', 1))
            txt = cell.get_text(strip=True)
            m = re.search(r'\b(20[3-9]\d)\b', txt)
            if m:
                year = int(m.group(1))
                for c in range(col, col + colspan):
                    found[c] = year
            col += colspan
        if found:
            year_col_map = found
            year_row_idx = ri
            break

    if not year_col_map:
        return picks

    # Determine round/team column types per year section
    # Within each year's colspan, first col = round, second = team
    year_sections = {}  # year -> (round_col, team_col)
    seen_year = {}
    for c, year in sorted(year_col_map.items()):
        if year not in seen_year:
            seen_year[year] = c
            year_sections[year] = (c, c + 1)

    # Skip year-header row and any sub-header row(s) containing "round"/"team"
    data_rows = rows[year_row_idx + 1:]
    while data_rows:
        cells_text = [c.get_text(strip=True).lower() for c in data_rows[0].find_all(['th', 'td'])]
        if any(t in ('round', 'team', 'r', 't') for t in cells_text):
            data_rows = data_rows[1:]
        else:
            break

    for row in data_rows:
        cells = row.find_all(['td', 'th'])
        cell_texts = [c.get_text(strip=True) for c in cells]
        for year, (round_col, team_col) in year_sections.items():
            if year not in target_years:
                continue
            rnd_txt  = cell_texts[round_col]  if round_col  < len(cell_texts) else ''
            team_txt = cell_texts[team_col]   if team_col   < len(cell_texts) else ''
            if not rnd_txt or not team_txt:
                continue
            try:
                rnd = int(rnd_txt)
            except ValueError:
                continue
            if rnd not in (1, 2):
                continue
            orig = find_abbr(team_txt)
            if orig:
                picks.append({'year': year, 'round': rnd, 'original_abbr': orig})

    return picks


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

    for raw_line in post_text.replace('\r', '').split('\n'):
        line = raw_line.strip()
        if not line:
            continue

        # Year header line: "2038:" or "2038"
        year_header = re.match(r'^(20[3-9]\d)\s*:?\s*$', line)
        if year_header:
            current_year = int(year_header.group(1))
            continue

        if not current_year:
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
        # to_part: stop at notes like "(", "*", " via "
        to_part = re.split(r'\s*[\(\*]|\s+via\s+|\s+\(', parts[1])[0].strip()

        to_abbr = find_abbr(to_part)
        if not to_abbr:
            continue

        # Handle dual-team from like "SA/MIA"
        from_teams = [t.strip() for t in from_part.split('/')]

        if len(from_teams) == 2 and qualifier:
            # Pick swap: "SA/MIA 1st to CHA (Worse)" / "SA/MIA 1st to ATL (Better)"
            # Worse → first team is from_abbr; Better → second team is from_abbr
            # This ensures each team appears as from_abbr exactly once per swap pair,
            # so each team's pick is correctly marked as owed away.
            idx = 1 if qualifier == 'better' else 0
            from_abbr = find_abbr(from_teams[idx])
            swap_partner = find_abbr(from_teams[1 - idx])
            if from_abbr and swap_partner and from_abbr != to_abbr:
                key = (from_abbr, current_year, rnd, to_abbr)
                if key not in seen:
                    seen.add(key)
                    owed.append({
                        'from_abbr': from_abbr,
                        'year': current_year,
                        'round': rnd,
                        'to_abbr': to_abbr,
                        'qualifier': qualifier,
                        'swap_partner': swap_partner,
                    })
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


_sync_lock = threading.Lock()
_sync_running = False


def _execute_picks_sync():
    """Core sync logic: scrapes roster pages (2036-2037) and forum thread (2038-2041).
    Returns (owed_list, errors_list).
    """
    db = get_db()
    cookie_row = db.execute("SELECT value FROM settings WHERE key='sln_cookie'").fetchone()
    cookie = (cookie_row[0] if cookie_row else None) or os.environ.get('SLN_COOKIE', '')
    db.close()

    owed = []
    errors = []
    seen = set()
    pub_headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}

    # ── Step 1: Roster pages for years 2036-2037 ─────────────────────────────
    owned_map = {}
    for roster_file, owner_abbr in ROSTER_MAP.items():
        try:
            resp = requests.get(ROSTER_BASE + roster_file, headers=pub_headers, timeout=10)
            if resp.status_code == 200:
                picks = parse_roster_draft_picks(resp.text, owner_abbr, ROSTER_PICK_YEARS)
                owned_map[owner_abbr] = picks
        except Exception as e:
            errors.append(f'Roster {roster_file}: {e}')

    all_owned = set()
    for picks in owned_map.values():
        for p in picks:
            all_owned.add((p['year'], p['round'], p['original_abbr']))

    for owner_abbr, picks in owned_map.items():
        for p in picks:
            orig = p['original_abbr']
            year = p['year']
            rnd  = p['round']
            if orig != owner_abbr:
                key = (orig, year, rnd, owner_abbr)
                if key not in seen:
                    seen.add(key)
                    owed.append({'from_abbr': orig, 'year': year, 'round': rnd, 'to_abbr': owner_abbr})

    for abbr in owned_map:
        for year in ROSTER_PICK_YEARS:
            for rnd in (1, 2):
                if (year, rnd, abbr) not in all_owned:
                    key = (abbr, year, rnd, 'EXT')
                    if key not in seen:
                        seen.add(key)
                        owed.append({'from_abbr': abbr, 'year': year, 'round': rnd, 'to_abbr': 'EXT'})

    # ── Step 2: Forum thread first post for years 2038-2041 ──────────────────
    if cookie:
        try:
            auth_headers = {**pub_headers, 'Cookie': cookie}
            resp = requests.get(SLN_THREAD_URL, headers=auth_headers, timeout=15)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')
                post_el = (soup.find('div', class_='content') or
                           soup.find('div', class_='postbody') or
                           soup.find('div', class_='post'))
                if post_el:
                    forum_picks = parse_owed_picks_from_thread(post_el.get_text(separator='\n'))
                    for o in forum_picks:
                        if o['year'] in FORUM_PICK_YEARS:
                            key = (o['from_abbr'], o['year'], o['round'], o['to_abbr'])
                            if key not in seen:
                                seen.add(key)
                                owed.append(o)
            else:
                errors.append(f'Forum thread HTTP {resp.status_code}')
        except Exception as e:
            errors.append(f'Forum thread: {e}')
    else:
        errors.append('No SLN cookie — skipped years 2038-2041 from forum thread')

    db = get_db()
    db.execute(
        '''INSERT INTO owed_picks (id, data, updated_at) VALUES (1, ?, CURRENT_TIMESTAMP)
               ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at''',
        (json.dumps(owed),)
    )
    db.commit()
    db.close()
    return owed, errors


def _bg_sync():
    """Run sync in background thread, guarded by lock."""
    global _sync_running
    with _sync_lock:
        if _sync_running:
            return
        _sync_running = True
    try:
        _execute_picks_sync()
    except Exception:
        pass
    finally:
        _sync_running = False


# Auto-sync picks when the app starts (non-blocking)
def _startup_sync():
    import time
    time.sleep(5)
    _bg_sync()

threading.Thread(target=_startup_sync, daemon=True).start()


@app.route('/api/picks', methods=['GET'])
def get_picks():
    db = get_db()
    row = db.execute('SELECT data, updated_at FROM owed_picks WHERE id = 1').fetchone()
    db.close()

    stale = True
    if row and row[1]:
        try:
            updated = datetime.strptime(row[1], '%Y-%m-%d %H:%M:%S')
            stale = (datetime.utcnow() - updated) > timedelta(hours=1)
        except Exception:
            pass

    if stale and not _sync_running:
        threading.Thread(target=_bg_sync, daemon=True).start()

    if row:
        return jsonify({'owed': json.loads(row[0]), 'updated_at': row[1], 'syncing': stale})
    return jsonify({'owed': [], 'updated_at': None, 'syncing': True})


@app.route('/api/picks/sync', methods=['POST'])
def sync_picks():
    """Sync owed picks:
    - Years 2036-2037: scrape all 29 roster pages (public, no cookie needed)
    - Years 2038-2041: scrape first post of SLN owed-picks thread (requires cookie)
    """
    owed, errors = _execute_picks_sync()
    return jsonify({'ok': True, 'count': len(owed), 'owed': owed, 'errors': errors})


@app.route('/api/picks/set-cookie', methods=['POST'])
def set_sln_cookie():
    """Store the SLN session cookie for scraping."""
    body = request.get_json()
    cookie = (body.get('cookie') or '').strip()
    if not cookie:
        return jsonify({'error': 'cookie required'}), 400
    db = get_db()
    db.execute("INSERT INTO settings (key, value) VALUES ('sln_cookie', ?) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (cookie,))
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/api/picks/login', methods=['POST'])
def sln_login():
    """Log into the SLN phpBB forum, store the resulting session cookie."""
    body = request.get_json() or {}
    username = (body.get('username') or '').strip()
    password = (body.get('password') or '').strip()
    if not username or not password:
        return jsonify({'error': 'username and password required'}), 400

    base_url  = 'https://simleaguenirvana.com'
    login_url = f'{base_url}/ucp.php?mode=login'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Referer': login_url,
    }

    try:
        session = requests.Session()

        # Fetch the login page to pick up the session cookie + CSRF token
        get_resp = session.get(login_url, headers=headers, timeout=10)
        soup = BeautifulSoup(get_resp.text, 'html.parser')
        form = soup.find('form', id='login') or soup.find('form')

        # Collect all hidden fields (last value wins for duplicates)
        hidden = {}
        if form:
            for inp in form.find_all('input', type='hidden'):
                if inp.get('name'):
                    hidden[inp['name']] = inp.get('value', '')

        # Resolve the form's action URL (it includes ?sid=...)
        action = (form.get('action') or 'ucp.php?mode=login') if form else 'ucp.php?mode=login'
        if action.startswith('./'):
            action = action[2:]
        post_url = f'{base_url}/{action}'

        payload = {
            **hidden,
            'username': username,
            'password': password,
            'autologin': 'on',   # "remember me" — extends session life
            'login':    'Login',
        }
        post_resp = session.post(post_url, data=payload, headers=headers,
                                 timeout=10, allow_redirects=True)

        # phpBB sets the _u cookie to the user's numeric ID (> 1) on success
        cookies = {c.name: c.value for c in session.cookies}
        uid_key = next((k for k in cookies if k.endswith('_u')), None)
        if not uid_key or cookies.get(uid_key, '1') == '1':
            # Try to surface the error phpBB showed
            soup2 = BeautifulSoup(post_resp.text, 'html.parser')
            err_el = soup2.find(class_='error') or soup2.find(class_='errorbox')
            msg = err_el.get_text(' ', strip=True)[:120] if err_el else 'Login failed — check username/password'
            return jsonify({'error': msg}), 401

        # Build cookie header string and persist it
        cookie_str = '; '.join(f'{k}={v}' for k, v in cookies.items())
        db = get_db()
        db.execute("INSERT INTO settings (key, value) VALUES ('sln_cookie', ?) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (cookie_str,))
        db.commit()
        db.close()

        # Kick off a fresh sync now that we have a valid cookie
        threading.Thread(target=_bg_sync, daemon=True).start()

        return jsonify({'ok': True, 'message': 'Logged in and sync started'})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/picks/from-paste', methods=['POST'])
def picks_from_paste():
    """Accept pasted text from the SLN owed-picks forum post.
    Parses years 3-6 picks and merges them with whatever roster-page data is in the DB.
    """
    body = request.get_json() or {}
    text = (body.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'text required'}), 400

    forum_picks = parse_owed_picks_from_thread(text)
    if not forum_picks:
        return jsonify({'error': 'No picks found — make sure you copied the full post text'}), 400

    db = get_db()
    row = db.execute('SELECT data FROM owed_picks WHERE id = 1').fetchone()
    existing = json.loads(row[0]) if row else []

    # Keep roster-page picks (years 2036-2037), replace forum-year picks with new paste
    kept = [p for p in existing if p.get('year') in ROSTER_PICK_YEARS]
    seen = {(p['from_abbr'], p['year'], p['round'], p['to_abbr']) for p in kept}

    added = 0
    for p in forum_picks:
        if p['year'] in FORUM_PICK_YEARS:
            key = (p['from_abbr'], p['year'], p['round'], p['to_abbr'])
            if key not in seen:
                seen.add(key)
                kept.append(p)
                added += 1

    db.execute(
        '''INSERT INTO owed_picks (id, data, updated_at) VALUES (1, ?, CURRENT_TIMESTAMP)
               ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at''',
        (json.dumps(kept),)
    )
    db.commit()
    db.close()
    return jsonify({'ok': True, 'added': added, 'total': len(kept)})


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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
