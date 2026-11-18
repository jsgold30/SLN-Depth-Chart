"""Refresh the draft class atomically from SLN's six sorted player tables."""
import hashlib
import json
import math
import re
import threading
import time
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup
import requests

BASE_URL = 'https://www.simleaguenirvana.com/draft/'
SORT_FIELDS = {'pot': 'srp', 'in': 'sri', 'out': 'sro', 'hn': 'srh', 'df': 'srd', 'reb': 'srr'}
MIN_PLAYERS = 58


def parse_players(html):
    soup = BeautifulSoup(html, 'html.parser')
    required = ['name', 'pos', 'height', 'weight', 'age', 'in', 'out', 'hn', 'df', 'reb', 'pot']
    for table in soup.find_all('table'):
        header = None
        players = []
        for row in table.find_all('tr'):
            if row.find_parent('table') is not table:
                continue
            cells = [cell.get_text(' ', strip=True) for cell in row.find_all(['td', 'th'], recursive=False)]
            lowered = [cell.lower() for cell in cells]
            if all(field in lowered for field in required):
                header = {field: lowered.index(field) for field in required}
                continue
            if not header or not cells:
                continue
            if len(cells) <= max(header.values()):
                raise ValueError('Incomplete draft table')
            values = {key: cells[index] for key, index in header.items()}
            if values['pos'] not in {'PG', 'SG', 'SF', 'PF', 'C'}:
                raise ValueError('Invalid draft player position')
            if any(not re.fullmatch(r'[A-F][+-]?', values[key]) for key in ['in', 'out', 'hn', 'df', 'reb', 'pot']):
                raise ValueError('Invalid draft player ratings')
            height = values['height'].replace("''", '"')
            if not re.fullmatch(r"\d'\d{1,2}\"", height) or not values['weight'].isdigit() or not values['age'].isdigit():
                raise ValueError('Invalid draft player attributes')
            players.append({'name': values['name'], 'pos': values['pos'], 'ht': height,
                            'wt': values['weight'], 'age': values['age'], 'in_rat': values['in'],
                            'in_rating': values['in'], 'out_rat': values['out'], 'out': values['out'],
                            'hn': values['hn'], 'df': values['df'], 'reb': values['reb'], 'pot': values['pot']})
        if header:
            names = [player['name'] for player in players]
            if len(players) < MIN_PLAYERS or len({name.casefold() for name in names}) != len(names) or any(not name for name in names):
                raise ValueError('Incomplete or duplicate draft player list')
            return players
    raise ValueError('Draft player table not found')


def fetch_pool(previous):
    base = None
    for index, (sort, field) in enumerate(SORT_FIELDS.items()):
        if index:
            time.sleep(1.5)
        response = requests.get(BASE_URL + 'draftplayers-' + sort + '.htm', timeout=15,
                                headers={'User-Agent': 'SLN Assistant GM draft pool reader'})
        response.raise_for_status()
        players = parse_players(response.text)
        by_name = {p['name']: p for p in players}
        if base is None:
            base = by_name
        elif set(by_name) != set(base):
            raise ValueError('Draft class pages are not yet consistent')
        for rank, player in enumerate(players, 1):
            base[player['name']][field] = rank
    ids = {p['name'].casefold(): p['id'] for p in previous}
    result = list(base.values())
    for player in result:
        player['id'] = ids.get(player['name'].casefold()) or 'sln-' + hashlib.sha256(player['name'].casefold().encode()).hexdigest()[:16]
    return result


def pool_version(players):
    canonical = sorted(players, key=lambda p: p['name'])
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


class DraftPoolSync:
    def __init__(self, get_db, context, baseline, baseline_version):
        self.get_db, self.context = get_db, context
        self.baseline, self.baseline_version = baseline, baseline_version

    @staticmethod
    def put(db, key, value):
        db.execute('INSERT INTO settings (key, value) VALUES (?, ?) '
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def snapshot(self):
        db = self.get_db()
        try:
            row = db.execute("SELECT value FROM settings WHERE key='draft_pool_live'").fetchone()
            return json.loads(row[0]) if row else {'players': self.baseline, 'pool_version': self.baseline_version, 'source': 'snapshot'}
        finally:
            db.close()

    def get(self, enabled=True):
        refreshing = False
        warning = None
        if enabled:
            now = time.time()
            db = self.get_db()
            try:
                claimed = db.execute('''INSERT INTO settings (key, value) VALUES ('draft_pool_next', ?)
                                        ON CONFLICT(key) DO UPDATE SET value=excluded.value
                                        WHERE CAST(settings.value AS DOUBLE PRECISION) <= ? RETURNING value''',
                                     (str(now + 240), now)).fetchone()
                row = db.execute("SELECT value FROM settings WHERE key='draft_pool_refreshing'").fetchone()
                refreshing = bool(row and float(row[0]) > now)
                warning_row = db.execute("SELECT value FROM settings WHERE key='draft_pool_warning'").fetchone()
                warning = warning_row[0] if warning_row else None
                if claimed:
                    self.put(db, 'draft_pool_refreshing', str(now + 240))
                    db.commit()
                    refreshing = True
            finally:
                db.close()
            if claimed:
                try:
                    threading.Thread(target=self.run, daemon=True).start()
                except Exception:
                    warning = 'SLN draft pool refresh could not start. Showing the saved player pool.'
                    self.finish(warning, 300)
                    refreshing = False
        return dict(self.snapshot(), refreshing=refreshing, warning=warning)

    def publish(self, players):
        version = pool_version(players)
        db = self.get_db()
        try:
            if db._is_pg:
                db.execute("INSERT INTO settings (key, value) VALUES ('draft_pool_lock', '1') ON CONFLICT(key) DO NOTHING")
                db.execute("SELECT value FROM settings WHERE key='draft_pool_lock' FOR UPDATE")
            else:
                db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM draft_state WHERE id=1' + (' FOR UPDATE' if db._is_pg else '')).fetchone()
            if row:
                state = json.loads(row[0])
                if state.get('pool_version') != version:
                    drafted = {result.get('playerName') for result in state.get('results', [])}
                    state.update(pool=[p for p in players if p['name'] not in drafted], pool_version=version,
                                 pool_client_version=2, ts=max(int(time.time() * 1000), state.get('ts', 0) + 1))
                    db.execute('UPDATE draft_state SET data=?, updated_at=CURRENT_TIMESTAMP WHERE id=1', (json.dumps(state),))
            payload = {'players': players, 'pool_version': version, 'source': 'sln', 'fetched_at': time.time()}
            self.put(db, 'draft_pool_live', json.dumps(payload))
            db.commit()
        finally:
            db.close()

    def run(self):
        with self.context():
            try:
                players = fetch_pool(self.snapshot()['players'])
                self.publish(players)
            except Exception as error:
                delay = 300
                if isinstance(error, requests.HTTPError) and error.response is not None and error.response.status_code == 429:
                    try:
                        delay = max(delay, int(error.response.headers.get('Retry-After', '300')))
                    except ValueError:
                        try:
                            deadline = parsedate_to_datetime(error.response.headers.get('Retry-After', '')).timestamp()
                            delay = max(delay, math.ceil(deadline - time.time()))
                        except (TypeError, ValueError, OverflowError):
                            pass
                self.finish('SLN draft pool could not be refreshed. Showing the saved player pool.', delay)
            else:
                self.finish('', 3600)

    def finish(self, warning, delay):
        db = self.get_db()
        try:
            self.put(db, 'draft_pool_warning', warning)
            self.put(db, 'draft_pool_next', str(time.time() + delay))
            self.put(db, 'draft_pool_refreshing', '0')
            db.commit()
        finally:
            db.close()
