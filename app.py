from flask import Flask, render_template, request, jsonify, make_response
import os
import time
import requests
try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper()
except ImportError:
    import requests as _req
    class _FallbackScraper:
        def get(self, *a, **kw): return _req.get(*a, **kw)
    _scraper = _FallbackScraper()

SCRAPER_API_KEY = os.environ.get('SCRAPER_API_KEY', '')

def _fetch_url(url, timeout=20, headers=None):
    """Fetch a URL, routing through ScraperAPI if SCRAPER_API_KEY is set.
    Pass headers= to forward cookies/auth (uses keep_headers=true with ScraperAPI)."""
    if SCRAPER_API_KEY:
        params = {'api_key': SCRAPER_API_KEY, 'url': url}
        if headers:
            params['keep_headers'] = 'true'
            return requests.get('https://api.scraperapi.com', params=params, headers=headers, timeout=timeout)
        return requests.get('https://api.scraperapi.com', params=params, timeout=timeout)
    if headers:
        return _scraper.get(url, headers=headers, timeout=timeout)
    return _scraper.get(url, timeout=timeout)
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


_sln_login_last_error = ''


class _SAPISession:
    """Wraps ScraperAPI API-endpoint calls behind a .get() interface.
    All requests share the same session_number so ScraperAPI maintains
    the same proxy IP and cookie jar across GET login → POST creds → forum fetch.
    """
    def __init__(self, api_key, session_num):
        self._key = api_key
        self._snum = session_num
        self._ua = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'

    def get(self, url, headers=None, timeout=30, **_):
        return requests.get(
            'https://api.scraperapi.com',
            params={'api_key': self._key, 'session_number': self._snum, 'url': url},
            headers={'User-Agent': self._ua},
            timeout=timeout,
        )


def _sln_auto_login():
    """Authenticate to SLN and return an _SAPISession for forum fetching.

    Strategy (in order):
    1. Stored SLN_COOKIE env var — fastest; works if phpBB has no IP-based
       session tracking (many phpBB installs disable it).
    2. Autologin _k key from SLN_COOKIE via proxy — phpBB's "remember me" key
       creates a fresh session on the proxy IP without a login form POST.
    3. Full API-endpoint login with manual cookie forwarding — slowest fallback.

    All forum fetches go through the same ScraperAPI session_number, keeping
    the proxy IP consistent with whatever session phpBB created.
    """
    global _sln_login_last_error
    _raw_key = os.environ.get('SCRAPER_API_KEY', '')
    api_key = _raw_key.strip()
    if not api_key:
        _sln_login_last_error = f'SCRAPER_API_KEY empty (raw len={len(_raw_key)})'
        return None
    username = os.environ.get('SLN_USERNAME', '').strip()
    password = os.environ.get('SLN_PASSWORD', '').strip()
    if not username or not password:
        _sln_login_last_error = 'SLN_USERNAME or SLN_PASSWORD not set'
        return None

    import random, time, urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    ua = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
    base_url  = 'https://simleaguenirvana.com'
    login_url = f'{base_url}/ucp.php?mode=login'

    stored_cookie = os.environ.get('SLN_COOKIE', '').strip()

    # ── Path 1: stored SLN_COOKIE directly via ScraperAPI ────────────────────
    # Works when phpBB has IP-based session tracking disabled (common config).
    if stored_cookie:
        try:
            session_num = random.randint(1, 9999)
            resp = requests.get(
                'https://api.scraperapi.com',
                params={'api_key': api_key, 'session_number': session_num,
                        'url': SLN_THREAD_URL, 'keep_headers': 'true'},
                headers={'User-Agent': ua, 'Cookie': stored_cookie},
                timeout=30,
            )
            if not BeautifulSoup(resp.text, 'html.parser').find('form', id='login'):
                app.logger.info('SLN auth: stored cookie accepted (path 1)')
                _sln_login_last_error = ''
                return _SAPISession(api_key, session_num)
            app.logger.info('SLN auth: stored cookie rejected — trying _k path')
        except Exception as e:
            app.logger.warning('SLN auth path 1 failed: %s', e)

    # ── Path 2: autologin _k key via ScraperAPI proxy ────────────────────────
    # phpBB's "remember me" key creates a new session on the current proxy IP
    # without requiring a form POST or form_token.
    if stored_cookie:
        parts = {}
        for p in stored_cookie.split(';'):
            p = p.strip()
            if '=' in p:
                k, v = p.split('=', 1)
                parts[k.strip()] = v.strip()
        k_name = next((k for k in parts if k.endswith('_k')), None)
        k_val  = parts.get(k_name, '') if k_name else ''
        if k_name and k_val:
            for attempt in range(2):
                try:
                    session_num = random.randint(1, 9999)
                    proxy = f'http://scraperapi.session-{session_num}:{api_key}@proxy.scraperapi.com:8001'
                    s = requests.Session()
                    s.proxies.update({'http': proxy, 'https': proxy})
                    s.verify = False
                    s.cookies.set(k_name, k_val, domain='simleaguenirvana.com', path='/')
                    s.get(f'{base_url}/', headers={'User-Agent': ua}, timeout=25)
                    cookies = {c.name: c.value for c in s.cookies}
                    uid_key = next((k for k in cookies if k.endswith('_u')), None)
                    if uid_key and cookies.get(uid_key, '1') != '1':
                        app.logger.info('SLN auth: _k autologin succeeded (path 2)')
                        _sln_login_last_error = ''
                        return s
                    app.logger.info('SLN auth: _k path attempt %d uid=%s',
                                    attempt + 1, cookies.get(uid_key, '?') if uid_key else 'none')
                except Exception as e:
                    app.logger.warning('SLN auth path 2 attempt %d failed: %s', attempt + 1, e)

    # ── Path 3: full login form via ScraperAPI API endpoint ──────────────────
    last_err = 'all paths failed'
    for attempt in range(3):
        if attempt:
            time.sleep(3)
        try:
            session_num = random.randint(1, 9999)
            sapi = {'api_key': api_key, 'session_number': session_num}

            get_resp = requests.get('https://api.scraperapi.com',
                                    params={**sapi, 'url': login_url},
                                    headers={'User-Agent': ua}, timeout=35)
            jar = {c.name: c.value for c in get_resp.cookies}
            for raw_sc in get_resp.raw.headers.getlist('Set-Cookie'):
                nv = raw_sc.split(';')[0].strip()
                if '=' in nv:
                    k, v = nv.split('=', 1)
                    jar.setdefault(k.strip(), v.strip())
            app.logger.info('SLN path 3 attempt %d: GET cookies=%s', attempt + 1, list(jar.keys()))

            soup = BeautifulSoup(get_resp.text, 'html.parser')
            form = soup.find('form', id='login') or soup.find('form')
            hidden = {}
            if form:
                for inp in form.find_all('input', type='hidden'):
                    if inp.get('name'):
                        hidden[inp['name']] = inp.get('value', '')
            action = (form.get('action') or 'ucp.php?mode=login') if form else 'ucp.php?mode=login'
            if action.startswith('./'):
                action = action[2:]
            post_target = f'{base_url}/{action}'
            payload = {**hidden, 'username': username, 'password': password,
                       'autologin': 'on', 'login': 'Login'}
            time.sleep(2)
            post_headers = {'User-Agent': ua, 'Referer': login_url,
                            'Content-Type': 'application/x-www-form-urlencoded'}
            if jar:
                post_headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in jar.items())
            post_resp = requests.post(
                'https://api.scraperapi.com',
                params={**sapi, 'url': post_target, 'keep_headers': 'true'},
                data=payload, headers=post_headers, timeout=35,
            )
            if BeautifulSoup(post_resp.text, 'html.parser').find('form', id='login'):
                last_err = f'path 3 POST still shows login form (attempt {attempt+1}, cookies={list(jar.keys())})'
                app.logger.warning('SLN auth: %s', last_err)
                continue

            _sln_login_last_error = ''
            return _SAPISession(api_key, session_num)

        except Exception as e:
            last_err = f'{type(e).__name__}: {e} (path 3 attempt {attempt+1})'
            app.logger.warning('SLN auth path 3 attempt %d failed: %s', attempt + 1, e)

    _sln_login_last_error = last_err
    return None


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
        result = {'players': players, 'team_name': team_name, 'total_salary': total_salary}

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
    return {'players': players, 'team_name': team_name, 'total_salary': total_salary}


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


# ── Static draft player pool (snapshot: 2026-06-29) ─────────────────────────
# To update this list, re-fetch https://www.simleaguenirvana.com/draft/draftplayers-pot.htm
DRAFT_PLAYER_POOL = [
    # Order matches https://www.simleaguenirvana.com/draft/draftplayers-pot.htm (snapshot: 2026-06-29)
    {'id':'','name':'Gideon Frost','pos':'SF','ht':"6'8\"",'wt':'225','age':'20','in_rat':'B','out_rat':'B','hn':'C+','df':'C+','reb':'C+','pot':'A','sri':3,'sro':8,'srh':14,'srd':27,'srr':21,'srp':1},
    {'id':'','name':'Jayce Pate','pos':'PF','ht':"6'10\"",'wt':'240','age':'20','in_rat':'C','out_rat':'C','hn':'C-','df':'C','reb':'C+','pot':'A','sri':33,'sro':54,'srh':42,'srd':64,'srr':16,'srp':2},
    {'id':'','name':'Zeke Prince','pos':'SG','ht':"6'6\"",'wt':'205','age':'20','in_rat':'C','out_rat':'B','hn':'C','df':'C','reb':'C+','pot':'A','sri':37,'sro':4,'srh':22,'srd':71,'srr':20,'srp':3},
    {'id':'','name':'Jenson Carpenter','pos':'PF','ht':"6'11\"",'wt':'250','age':'21','in_rat':'C','out_rat':'A-','hn':'D-','df':'C+','reb':'C','pot':'A','sri':34,'sro':1,'srh':70,'srd':44,'srr':26,'srp':4},
    {'id':'','name':'Terry Hayes','pos':'SG','ht':"6'5\"",'wt':'185','age':'19','in_rat':'C','out_rat':'C+','hn':'C','df':'C+','reb':'C-','pot':'A','sri':40,'sro':23,'srh':17,'srd':35,'srr':51,'srp':5},
    {'id':'','name':'Korbin Houston','pos':'SG','ht':"6'7\"",'wt':'210','age':'21','in_rat':'C+','out_rat':'B-','hn':'C+','df':'C+','reb':'C','pot':'A','sri':15,'sro':16,'srh':9,'srd':29,'srr':49,'srp':6},
    {'id':'','name':'Daniel Gordon','pos':'PF','ht':"6'10\"",'wt':'230','age':'21','in_rat':'B','out_rat':'C','hn':'C-','df':'B','reb':'C','pot':'A','sri':7,'sro':42,'srh':43,'srd':6,'srr':29,'srp':7},
    {'id':'','name':'Jayden McGee','pos':'C','ht':"6'11\"",'wt':'225','age':'19','in_rat':'B-','out_rat':'C','hn':'D','df':'C','reb':'C','pot':'A','sri':10,'sro':50,'srh':65,'srd':52,'srr':31,'srp':8},
    {'id':'','name':'Anderius Mack','pos':'PG','ht':"6'2\"",'wt':'190','age':'21','in_rat':'C','out_rat':'B-','hn':'B+','df':'C','reb':'C-','pot':'A','sri':56,'sro':11,'srh':2,'srd':48,'srr':59,'srp':9},
    {'id':'','name':'Emery Moon','pos':'PG','ht':"6'3\"",'wt':'200','age':'21','in_rat':'C+','out_rat':'C+','hn':'C+','df':'C+','reb':'C','pot':'A','sri':31,'sro':17,'srh':8,'srd':40,'srr':43,'srp':10},
    {'id':'','name':'Troy Sykes','pos':'SF','ht':"6'7\"",'wt':'220','age':'21','in_rat':'B','out_rat':'C','hn':'C','df':'B-','reb':'C','pot':'A','sri':2,'sro':41,'srh':34,'srd':10,'srr':36,'srp':11},
    {'id':'','name':'Rory Goff','pos':'SF','ht':"6'8\"",'wt':'225','age':'21','in_rat':'C','out_rat':'B','hn':'C+','df':'B','reb':'C','pot':'A','sri':49,'sro':9,'srh':10,'srd':8,'srr':48,'srp':12},
    {'id':'','name':'Zareen Hubbard','pos':'PG','ht':"6'4\"",'wt':'190','age':'21','in_rat':'C','out_rat':'B','hn':'C','df':'C+','reb':'C-','pot':'B','sri':59,'sro':5,'srh':24,'srd':34,'srr':58,'srp':13},
    {'id':'','name':'Felix Parks','pos':'SG','ht':"6'6\"",'wt':'195','age':'21','in_rat':'B','out_rat':'C+','hn':'C','df':'B-','reb':'C-','pot':'B','sri':5,'sro':35,'srh':20,'srd':9,'srr':55,'srp':14},
    {'id':'','name':'Alex Scheuermann','pos':'PG','ht':"6'5\"",'wt':'205','age':'21','in_rat':'C+','out_rat':'C','hn':'B','df':'C','reb':'C+','pot':'B','sri':24,'sro':40,'srh':3,'srd':65,'srr':19,'srp':15},
    {'id':'','name':'Dorian Duncan','pos':'PF','ht':"6'11\"",'wt':'225','age':'20','in_rat':'B-','out_rat':'B-','hn':'C-','df':'C','reb':'B-','pot':'B','sri':8,'sro':12,'srh':52,'srd':46,'srr':3,'srp':16},
    {'id':'','name':'Travone Ramsey','pos':'C','ht':"7'0\"",'wt':'240','age':'21','in_rat':'B-','out_rat':'C','hn':'D','df':'C+','reb':'B-','pot':'B','sri':14,'sro':37,'srh':67,'srd':28,'srr':4,'srp':17},
    {'id':'','name':'Ontrel Monroe','pos':'PG','ht':"6'3\"",'wt':'205','age':'21','in_rat':'C','out_rat':'C+','hn':'C+','df':'B','reb':'C-','pot':'B','sri':69,'sro':25,'srh':13,'srd':3,'srr':63,'srp':18},
    {'id':'','name':'Dane Dunn','pos':'SG','ht':"6'6\"",'wt':'195','age':'21','in_rat':'C+','out_rat':'C+','hn':'C','df':'C+','reb':'C','pot':'B','sri':25,'sro':34,'srh':21,'srd':37,'srr':45,'srp':19},
    {'id':'','name':'Declan Miller','pos':'PF','ht':"6'10\"",'wt':'235','age':'20','in_rat':'C+','out_rat':'C-','hn':'C-','df':'B','reb':'B-','pot':'B','sri':18,'sro':59,'srh':54,'srd':5,'srr':5,'srp':20},
    {'id':'','name':'Sonny Burke','pos':'SF','ht':"6'7\"",'wt':'210','age':'21','in_rat':'B-','out_rat':'C+','hn':'C+','df':'B-','reb':'C','pot':'B','sri':11,'sro':28,'srh':12,'srd':12,'srr':42,'srp':21},
    {'id':'','name':'Micah Frederick','pos':'SF','ht':"6'6\"",'wt':'225','age':'23','in_rat':'C+','out_rat':'C+','hn':'C-','df':'C','reb':'C+','pot':'B','sri':16,'sro':36,'srh':41,'srd':50,'srr':11,'srp':22},
    {'id':'','name':'Terrell Mosley','pos':'SF','ht':"6'6\"",'wt':'220','age':'22','in_rat':'C+','out_rat':'C+','hn':'C-','df':'B+','reb':'C','pot':'B','sri':29,'sro':29,'srh':48,'srd':2,'srr':32,'srp':23},
    {'id':'','name':'Major Burns','pos':'C','ht':"6'10\"",'wt':'240','age':'23','in_rat':'C+','out_rat':'C-','hn':'C-','df':'C+','reb':'C+','pot':'B','sri':21,'sro':61,'srh':39,'srd':32,'srr':17,'srp':24},
    {'id':'','name':'Caleb Guy','pos':'C','ht':"6'10\"",'wt':'235','age':'22','in_rat':'C+','out_rat':'C','hn':'D+','df':'C+','reb':'C+','pot':'B','sri':28,'sro':53,'srh':64,'srd':26,'srr':18,'srp':25},
    {'id':'','name':'Kaydron Thornton','pos':'C','ht':"6'11\"",'wt':'230','age':'21','in_rat':'C','out_rat':'B','hn':'D+','df':'B-','reb':'C+','pot':'B','sri':46,'sro':10,'srh':58,'srd':14,'srr':9,'srp':26},
    {'id':'','name':"Da'wan Edwards",'pos':'PG','ht':"6'3\"",'wt':'195','age':'22','in_rat':'C','out_rat':'B-','hn':'C+','df':'B-','reb':'D+','pot':'B','sri':66,'sro':13,'srh':7,'srd':15,'srr':69,'srp':27},
    {'id':'','name':'Titan Williams','pos':'SF','ht':"6'8\"",'wt':'210','age':'21','in_rat':'C','out_rat':'B-','hn':'C','df':'C+','reb':'C','pot':'B','sri':41,'sro':15,'srh':26,'srd':31,'srr':35,'srp':28},
    {'id':'','name':'Vance Tillman','pos':'PF','ht':"6'9\"",'wt':'230','age':'21','in_rat':'B-','out_rat':'C-','hn':'C-','df':'C+','reb':'C+','pot':'B','sri':12,'sro':63,'srh':51,'srd':23,'srr':10,'srp':29},
    {'id':'','name':'Dylan Shaw','pos':'C','ht':"7'1\"",'wt':'255','age':'22','in_rat':'C+','out_rat':'C-','hn':'D','df':'C','reb':'B','pot':'B','sri':27,'sro':62,'srh':68,'srd':63,'srr':1,'srp':30},
    {'id':'','name':'Quincy Knight','pos':'SF','ht':"6'7\"",'wt':'215','age':'22','in_rat':'C+','out_rat':'B-','hn':'C','df':'C+','reb':'C','pot':'B','sri':23,'sro':14,'srh':33,'srd':38,'srr':39,'srp':31},
    {'id':'','name':'Patrick Donaldson','pos':'PF','ht':"6'10\"",'wt':'230','age':'21','in_rat':'C+','out_rat':'C-','hn':'C+','df':'B','reb':'C+','pot':'B','sri':32,'sro':64,'srh':15,'srd':4,'srr':13,'srp':32},
    {'id':'','name':'Ari Hoover','pos':'PG','ht':"6'3\"",'wt':'180','age':'22','in_rat':'C','out_rat':'C+','hn':'B','df':'C+','reb':'C-','pot':'C','sri':67,'sro':21,'srh':4,'srd':24,'srr':62,'srp':33},
    {'id':'','name':'Orion Knowles','pos':'SG','ht':"6'5\"",'wt':'205','age':'22','in_rat':'C','out_rat':'B','hn':'D+','df':'B','reb':'C-','pot':'C','sri':36,'sro':7,'srh':59,'srd':7,'srr':56,'srp':34},
    {'id':'','name':'DeShawn Hamilton','pos':'C','ht':"6'11\"",'wt':'245','age':'22','in_rat':'C','out_rat':'C','hn':'D+','df':'B-','reb':'B-','pot':'C','sri':52,'sro':52,'srh':60,'srd':11,'srr':6,'srp':35},
    {'id':'','name':'Therman Calhoun','pos':'C','ht':"6'10\"",'wt':'250','age':'22','in_rat':'B','out_rat':'C','hn':'D-','df':'C','reb':'B','pot':'C','sri':4,'sro':48,'srh':71,'srd':58,'srr':2,'srp':36},
    {'id':'','name':'Brennan Barrett','pos':'SF','ht':"6'8\"",'wt':'225','age':'22','in_rat':'B','out_rat':'C+','hn':'C','df':'B-','reb':'C','pot':'C','sri':6,'sro':27,'srh':28,'srd':13,'srr':41,'srp':37},
    {'id':'','name':'Damien Kinney','pos':'PG','ht':"6'1\"",'wt':'190','age':'22','in_rat':'D+','out_rat':'C+','hn':'B-','df':'C+','reb':'D','pot':'C','sri':71,'sro':30,'srh':5,'srd':19,'srr':71,'srp':38},
    {'id':'','name':'Collin Warner','pos':'PF','ht':"6'10\"",'wt':'230','age':'22','in_rat':'C','out_rat':'C-','hn':'D+','df':'C','reb':'C','pot':'C','sri':44,'sro':56,'srh':56,'srd':51,'srr':24,'srp':39},
    {'id':'','name':'Johan Erickson','pos':'SG','ht':"6'4\"",'wt':'200','age':'22','in_rat':'C','out_rat':'B','hn':'C','df':'C','reb':'C','pot':'C','sri':60,'sro':6,'srh':32,'srd':53,'srr':37,'srp':40},
    {'id':'','name':'Dexter Ward','pos':'PG','ht':"6'3\"",'wt':'180','age':'22','in_rat':'C','out_rat':'C+','hn':'C','df':'C','reb':'C-','pot':'C','sri':65,'sro':32,'srh':19,'srd':47,'srr':61,'srp':41},
    {'id':'','name':'Clark Carson','pos':'SG','ht':"6'5\"",'wt':'190','age':'23','in_rat':'C','out_rat':'B+','hn':'C-','df':'C','reb':'C-','pot':'C','sri':58,'sro':2,'srh':35,'srd':59,'srr':54,'srp':42},
    {'id':'','name':'Jack West','pos':'SF','ht':"6'7\"",'wt':'220','age':'22','in_rat':'C','out_rat':'C','hn':'C+','df':'C+','reb':'C','pot':'C','sri':47,'sro':45,'srh':11,'srd':25,'srr':50,'srp':43},
    {'id':'','name':'Elijah Luna','pos':'SF','ht':"6'7\"",'wt':'220','age':'22','in_rat':'C','out_rat':'C+','hn':'C','df':'C+','reb':'C-','pot':'C','sri':48,'sro':24,'srh':23,'srd':42,'srr':52,'srp':44},
    {'id':'','name':'Graeme McKinney','pos':'SF','ht':"6'8\"",'wt':'220','age':'22','in_rat':'C+','out_rat':'B+','hn':'C','df':'C+','reb':'C','pot':'C','sri':22,'sro':3,'srh':29,'srd':20,'srr':47,'srp':45},
    {'id':'','name':'Jerry Rogers','pos':'PF','ht':"6'9\"",'wt':'235','age':'23','in_rat':'C','out_rat':'C-','hn':'D+','df':'C+','reb':'C','pot':'C','sri':43,'sro':58,'srh':63,'srd':43,'srr':33,'srp':46},
    {'id':'','name':'Angel Chavez','pos':'PF','ht':"6'9\"",'wt':'230','age':'22','in_rat':'C+','out_rat':'C','hn':'C-','df':'B-','reb':'C','pot':'C','sri':30,'sro':49,'srh':50,'srd':17,'srr':28,'srp':47},
    {'id':'','name':'Kendrick Ferrell','pos':'PF','ht':"6'11\"",'wt':'240','age':'22','in_rat':'B+','out_rat':'C-','hn':'D+','df':'C','reb':'C+','pot':'C','sri':1,'sro':57,'srh':62,'srd':56,'srr':7,'srp':48},
    {'id':'','name':'Ahmad Brewer','pos':'PF','ht':"6'8\"",'wt':'245','age':'23','in_rat':'C+','out_rat':'C-','hn':'D+','df':'C+','reb':'C+','pot':'C','sri':17,'sro':68,'srh':57,'srd':39,'srr':12,'srp':49},
    {'id':'','name':'Theo Barker','pos':'C','ht':"6'10\"",'wt':'240','age':'23','in_rat':'C+','out_rat':'D+','hn':'D','df':'B-','reb':'C','pot':'C','sri':20,'sro':69,'srh':66,'srd':18,'srr':27,'srp':50},
    {'id':'','name':'Lucas Hopkins','pos':'C','ht':"6'11\"",'wt':'240','age':'23','in_rat':'C','out_rat':'C+','hn':'C-','df':'C','reb':'C+','pot':'C','sri':55,'sro':20,'srh':53,'srd':55,'srr':22,'srp':51},
    {'id':'','name':'Nixon Davis','pos':'PG','ht':"6'3\"",'wt':'195','age':'23','in_rat':'C','out_rat':'C','hn':'B-','df':'C','reb':'C-','pot':'C','sri':63,'sro':39,'srh':6,'srd':61,'srr':57,'srp':52},
    {'id':'','name':'Lewis Buckner','pos':'PG','ht':"6'2\"",'wt':'185','age':'23','in_rat':'C','out_rat':'C+','hn':'C','df':'C+','reb':'D+','pot':'C','sri':68,'sro':31,'srh':16,'srd':33,'srr':68,'srp':53},
    {'id':'','name':'Owen Hamilton','pos':'SG','ht':"6'7\"",'wt':'215','age':'22','in_rat':'C','out_rat':'C+','hn':'C','df':'C+','reb':'C-','pot':'C','sri':53,'sro':18,'srh':25,'srd':41,'srr':66,'srp':54},
    {'id':'','name':'Jay Jones','pos':'SF','ht':"6'7\"",'wt':'215','age':'22','in_rat':'C','out_rat':'C','hn':'C-','df':'C','reb':'C','pot':'C','sri':42,'sro':44,'srh':37,'srd':54,'srr':40,'srp':55},
    {'id':'','name':'Gerald Barton','pos':'C','ht':"6'10\"",'wt':'245','age':'22','in_rat':'C','out_rat':'C-','hn':'D','df':'C+','reb':'C+','pot':'C','sri':51,'sro':66,'srh':69,'srd':21,'srr':8,'srp':56},
    {'id':'','name':'Greyson Jones','pos':'SF','ht':"6'7\"",'wt':'215','age':'22','in_rat':'C','out_rat':'C+','hn':'C-','df':'B-','reb':'C','pot':'C','sri':38,'sro':26,'srh':40,'srd':16,'srr':46,'srp':57},
    {'id':'','name':'Derrick Booth','pos':'SG','ht':"6'6\"",'wt':'200','age':'22','in_rat':'B-','out_rat':'C+','hn':'C-','df':'C','reb':'C','pot':'C','sri':13,'sro':22,'srh':36,'srd':60,'srr':44,'srp':58},
    {'id':'','name':'Oliver Jenkins','pos':'PF','ht':"6'9\"",'wt':'225','age':'23','in_rat':'C','out_rat':'C+','hn':'C-','df':'C','reb':'C','pot':'C','sri':45,'sro':19,'srh':38,'srd':66,'srr':25,'srp':59},
    {'id':'','name':'Konner Reynolds','pos':'C','ht':"6'11\"",'wt':'270','age':'22','in_rat':'C','out_rat':'C-','hn':'C-','df':'B+','reb':'C+','pot':'C','sri':39,'sro':65,'srh':44,'srd':1,'srr':14,'srp':60},
    {'id':'','name':'Morgan Holland','pos':'C','ht':"6'10\"",'wt':'230','age':'22','in_rat':'B-','out_rat':'C-','hn':'C-','df':'C','reb':'C','pot':'C','sri':9,'sro':67,'srh':55,'srd':68,'srr':34,'srp':61},
    {'id':'','name':'Fabian Singleton','pos':'SF','ht':"6'9\"",'wt':'225','age':'23','in_rat':'C','out_rat':'C','hn':'C','df':'C+','reb':'C','pot':'C','sri':54,'sro':55,'srh':27,'srd':30,'srr':38,'srp':62},
    {'id':'','name':'Liam Banks','pos':'SG','ht':"6'6\"",'wt':'200','age':'23','in_rat':'C','out_rat':'C+','hn':'C-','df':'C','reb':'C-','pot':'C','sri':62,'sro':33,'srh':46,'srd':62,'srr':53,'srp':63},
    {'id':'','name':'Joshua Barnes','pos':'PG','ht':"6'2\"",'wt':'180','age':'23','in_rat':'C-','out_rat':'C','hn':'C','df':'C','reb':'D+','pot':'C','sri':70,'sro':46,'srh':18,'srd':45,'srr':70,'srp':64},
    {'id':'','name':'Bryce Avery','pos':'C','ht':"6'9\"",'wt':'245','age':'22','in_rat':'C','out_rat':'C-','hn':'C-','df':'C','reb':'C+','pot':'C','sri':35,'sro':60,'srh':45,'srd':67,'srr':15,'srp':65},
    {'id':'','name':'Brogan Fowler','pos':'C','ht':"7'0\"",'wt':'250','age':'23','in_rat':'C+','out_rat':'D+','hn':'C-','df':'C','reb':'C','pot':'C','sri':26,'sro':70,'srh':49,'srd':57,'srr':30,'srp':66},
    {'id':'','name':'Nathan Mason','pos':'SG','ht':"6'5\"",'wt':'190','age':'23','in_rat':'C+','out_rat':'C','hn':'C','df':'C+','reb':'C-','pot':'C','sri':19,'sro':43,'srh':31,'srd':36,'srr':65,'srp':67},
    {'id':'','name':'Chace Carr','pos':'PG','ht':"6'2\"",'wt':'190','age':'23','in_rat':'C','out_rat':'C','hn':'B+','df':'C+','reb':'D+','pot':'D','sri':64,'sro':38,'srh':1,'srd':22,'srr':67,'srp':68},
    {'id':'','name':'Rocco Green','pos':'SG','ht':"6'4\"",'wt':'195','age':'23','in_rat':'C','out_rat':'C','hn':'C','df':'C','reb':'C-','pot':'D','sri':57,'sro':47,'srh':30,'srd':49,'srr':64,'srp':69},
    {'id':'','name':'Jairo Payne','pos':'SF','ht':"6'6\"",'wt':'215','age':'22','in_rat':'C','out_rat':'C','hn':'C-','df':'C','reb':'C-','pot':'D','sri':61,'sro':51,'srh':47,'srd':70,'srr':60,'srp':70},
    {'id':'','name':'David Nielsen','pos':'PF','ht':"6'11\"",'wt':'245','age':'23','in_rat':'C','out_rat':'D+','hn':'D+','df':'C','reb':'C','pot':'D','sri':50,'sro':71,'srh':61,'srd':69,'srr':23,'srp':71},
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

def get_roster_pick_years():
    y = get_league_year()
    return [y + 1]

def get_forum_pick_years():
    y = get_league_year()
    return list(range(y + 1, y + 7))  # 6 years ahead: y+1 through y+6

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
    """Core sync logic: scrapes roster pages (year+1) and forum thread (year+1 through year+6).
    Returns (owed_list, errors_list).
    """
    # Login via ScraperAPI sticky session — both login and forum use the same
    # proxy IP, satisfying phpBB's session IP validation.
    sln_session = _sln_auto_login()

    owed = []
    errors = []
    seen = set()
    pub_headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}

    # ── Step 1: Roster pages for years 2036-2037 ─────────────────────────────
    owned_map = {}
    for roster_file, owner_abbr in ROSTER_MAP.items():
        try:
            resp = _fetch_url(ROSTER_BASE + roster_file, timeout=15)
            if resp.status_code == 200:
                picks = parse_roster_draft_picks(resp.text, owner_abbr, get_roster_pick_years())
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
        for year in get_roster_pick_years():
            for rnd in (1, 2):
                if (year, rnd, abbr) not in all_owned:
                    key = (abbr, year, rnd, 'EXT')
                    if key not in seen:
                        seen.add(key)
                        owed.append({'from_abbr': abbr, 'year': year, 'round': rnd, 'to_abbr': 'EXT'})

    # ── Step 2: Forum thread first post (years y+1 through y+6) ─────────────
    if sln_session:
        try:
            resp = sln_session.get(SLN_THREAD_URL, headers=pub_headers, timeout=25)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')
                post_el = (soup.find('div', class_='content') or
                           soup.find('div', class_='postbody') or
                           soup.find('div', class_='post'))
                if post_el:
                    forum_picks = parse_owed_picks_from_thread(post_el.get_text(separator='\n'))
                    for o in forum_picks:
                        if o['year'] in get_forum_pick_years():
                            key = (o['from_abbr'], o['year'], o['round'], o['to_abbr'])
                            if key not in seen:
                                seen.add(key)
                                owed.append(o)
            else:
                errors.append(f'Forum thread HTTP {resp.status_code}')
        except Exception as e:
            errors.append(f'Forum thread: {e}')
    else:
        errors.append(f'Login failed ({_sln_login_last_error or "unknown"}) — skipped forum thread picks')

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
    except Exception as e:
        app.logger.warning("non-critical error suppressed: %s", e)
    finally:
        _sync_running = False


# Auto-sync picks on startup, then every 6 hours (non-blocking)
def _startup_sync():
    import time
    time.sleep(5)  # Wait for app to fully initialize
    while True:
        _bg_sync()
        time.sleep(6 * 3600)

threading.Thread(target=_startup_sync, daemon=True).start()


@app.route('/api/status', methods=['GET'])
def api_status():
    key = SCRAPER_API_KEY
    return jsonify({
        'scraper_api': 'configured' if key else 'not set',
        'scraper_api_key_preview': (key[:4] + '…' + key[-4:]) if key else None,
    })


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


@app.route('/api/picks/sync', methods=['POST'])
def sync_picks():
    """Trigger a background sync and return the current cached picks immediately.
    The sync can take 60-120s (roster pages + ScraperAPI login + forum fetch),
    so running it synchronously would exceed Railway's HTTP timeout.
    The frontend's existing poll loop will pick up the updated data when done.
    """
    threading.Thread(target=_bg_sync, daemon=True).start()
    db = get_db()
    row = db.execute('SELECT data, updated_at FROM owed_picks WHERE id = 1').fetchone()
    db.close()
    owed = json.loads(row[0]) if row else []
    return jsonify({'ok': True, 'count': len(owed), 'owed': owed, 'errors': [],
                    'syncing': True, 'league_year': get_league_year()})


@app.route('/api/picks/debug-forum', methods=['GET'])
def debug_forum():
    """Temporary debug endpoint: shows raw forum post text and parsed picks."""
    sln_session = _sln_auto_login()
    errors = []
    raw_text = ''
    parsed = []
    if sln_session:
        try:
            resp = sln_session.get(SLN_THREAD_URL, headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}, timeout=25)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')
                post_el = (soup.find('div', class_='content') or
                           soup.find('div', class_='postbody') or
                           soup.find('div', class_='post'))
                if post_el:
                    raw_text = post_el.get_text(separator='\n')
                    parsed = parse_owed_picks_from_thread(raw_text)
                else:
                    errors.append('No post element found — page may still be showing login')
            else:
                errors.append(f'HTTP {resp.status_code}')
        except Exception as e:
            errors.append(str(e))
    else:
        has_user = bool(os.environ.get('SLN_USERNAME'))
        has_pass = bool(os.environ.get('SLN_PASSWORD'))
        has_key  = bool(SCRAPER_API_KEY)
        errors.append(f'Login failed — SLN_USERNAME={has_user} SLN_PASSWORD={has_pass} SCRAPER_API_KEY={has_key} — {_sln_login_last_error or "no error captured"}')
    sas_owed = [p for p in parsed if p['from_abbr'] == 'SAS' or p['to_abbr'] == 'SAS']
    return jsonify({
        'login_ok': bool(sln_session),
        'errors': errors,
        'raw_lines': raw_text.split('\n')[:200],
        'total_parsed': len(parsed),
        'sas_picks': sas_owed,
        'all_parsed': parsed,
    })


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
        cookie_str = '; '.join(f'{k}={v.strip()}' for k, v in cookies.items()).strip()
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

    # Keep roster-page picks (years 2036-2037), fully replace forum-year picks with new paste
    kept = [p for p in existing if p.get('year') in get_roster_pick_years()]
    forum_to_add = [p for p in forum_picks if p['year'] in get_forum_pick_years()]
    kept.extend(forum_to_add)
    added = len(forum_to_add)

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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
