"""Read-only forum access and a database-coordinated background refresh."""
import json
import math
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

FORUM_URL = 'https://simleaguenirvana.com/viewtopic.php?t=18&hilit=owed+picks'
FORUM_HOSTS = {'simleaguenirvana.com', 'www.simleaguenirvana.com'}


class ForumLoginRequired(ValueError):
    pass


def login_failure_message(html):
    """Map forum feedback to safe diagnostics without exposing response text or credentials."""
    soup = BeautifulSoup(html, 'html.parser')
    errors = ' '.join(node.get_text(' ', strip=True) for node in soup.select('.error, .errorbox, .successbox')).lower()
    if 'submitted form was invalid' in errors or ('form' in errors and 'invalid' in errors):
        return 'The forum rejected the login form verification. Saved picks are retained.'
    if any(word in errors for word in ['captcha', 'maximum number', 'too many login']):
        return 'The forum requires an interactive login/security check. Saved picks are retained.'
    if any(word in errors for word in ['incorrect', 'invalid username', 'invalid password']):
        return 'The forum rejected the configured SLN username/password. Check the staging service variables. Saved picks are retained.'
    return None


def first_post_text(html):
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('.postbody .content')
    if content is None:
        raise ForumLoginRequired('Forum login required. Check the SLN account access. Saved picks are retained.')
    for quote in content.select('blockquote'):
        quote.decompose()
    for br in content.find_all('br'):
        br.replace_with('\n')
    for block in content.find_all(['p', 'div', 'li']):
        block.append('\n')
    return '\n'.join(' '.join(line.split()) for line in content.get_text().splitlines())


def fetch_first_post(credentials):
    with requests.Session() as session:
        session.headers['User-Agent'] = 'SLN Assistant GM owed-picks reader'
        if credentials.get('cookie'):
            session.headers['Cookie'] = credentials['cookie']
        response = session.get(FORUM_URL, timeout=10)
        response.raise_for_status()
        try:
            return first_post_text(response.text)
        except ForumLoginRequired:
            if not credentials.get('username') or not credentials.get('password'):
                raise ForumLoginRequired('The SLN session is expired and a complete SLN_USERNAME/SLN_PASSWORD login is not configured for this service. Saved picks are retained.') from None
        # Use the forum's actual hidden/CSRF fields and keep authentication in this session.
        soup = BeautifulSoup(response.text, 'html.parser')
        password_input = soup.find('input', attrs={'name': 'password'})
        form = password_input.find_parent('form') if password_input else None
        if form is None:
            raise ForumLoginRequired('Forum login form unavailable. Saved picks are retained.')
        login_url = urljoin(response.url, form.get('action', ''))
        if urlsplit(login_url).scheme != 'https' or urlsplit(login_url).hostname not in FORUM_HOSTS:
            raise ForumLoginRequired('Unrecognized forum login destination. Saved picks are retained.')
        payload = {field['name']: field.get('value', '') for field in form.find_all('input', type='hidden') if field.get('name')}
        payload.update(username=credentials['username'], password=credentials['password'], login='Login')
        session.headers.pop('Cookie', None)
        # phpBB check_form_key rejects tokens submitted in their creation second.
        # Preserve the issued token and allow one full second to elapse on the forum.
        time.sleep(1.1)
        response = session.post(login_url, data=payload, timeout=10, allow_redirects=False)
        response.raise_for_status()
        failure = login_failure_message(response.text)
        if failure:
            raise ForumLoginRequired(failure)
        # Fetch the known topic URL rather than following a form-controlled redirect.
        response = session.get(FORUM_URL, timeout=10)
        response.raise_for_status()
        return first_post_text(response.text)


class OwedPickSync:
    def __init__(self, get_db, import_text, context):
        self.get_db = get_db
        self.import_text = import_text
        self.context = context

    @staticmethod
    def _put(db, key, value):
        db.execute('INSERT INTO settings (key, value) VALUES (?, ?) '
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def schedule(self, credentials, enabled=True, force=False):
        if not enabled:
            return {'syncing': False, 'sync_error': None, 'auto_sync': False}
        if not credentials.get('cookie') and not (credentials.get('username') and credentials.get('password')):
            return {'syncing': False, 'auto_sync': True,
                    'sync_error': 'Automatic forum refresh needs an SLN login. Saved picks and manual paste remain available.'}
        now = time.time()
        db = self.get_db()
        try:
            # Serialize status/lease changes across workers (including forced refreshes).
            db.execute("INSERT INTO settings (key, value) VALUES ('owed_sync_next', '0') ON CONFLICT(key) DO NOTHING")
            if db._is_pg:
                db.execute("SELECT value FROM settings WHERE key='owed_sync_next' FOR UPDATE")
            row = db.execute("SELECT value FROM settings WHERE key='owed_sync_status'").fetchone()
            status = json.loads(row[0]) if row else {}
            # A user refresh can bypass the hourly success cache, but not failures or rapid clicks.
            if force and not status.get('syncing') and not status.get('sync_error') and now - status.get('attempted_at', 0) >= 60:
                db.execute("UPDATE settings SET value='0' WHERE key='owed_sync_next'")
            claimed = db.execute('''INSERT INTO settings (key, value) VALUES ('owed_sync_next', ?)
                                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                                    WHERE CAST(settings.value AS DOUBLE PRECISION) <= ? RETURNING value''',
                                 (str(now + 90), now)).fetchone()
            if not claimed:
                return dict(status, auto_sync=True)
            snapshot_row = db.execute('SELECT data FROM owed_picks WHERE id=1').fetchone()
            snapshot = snapshot_row[0] if snapshot_row else None
            status.update(syncing=True, sync_error=None, attempted_at=now)
            self._put(db, 'owed_sync_status', json.dumps(status))
            db.commit()
        finally:
            db.close()
        try:
            threading.Thread(target=self.run, args=(credentials, snapshot), daemon=True).start()
        except Exception:
            self.finish('Could not start automatic refresh. Saved picks are retained.', 300)
            return {'syncing': False, 'auto_sync': True, 'sync_error': 'Could not start automatic refresh. Saved picks are retained.'}
        return dict(status, auto_sync=True)

    def run(self, credentials, snapshot):
        with self.context():
            try:
                text = fetch_first_post(credentials)
                self.import_text(text, snapshot)
            except ValueError as error:
                self.finish(str(error), 300)
            except requests.HTTPError as error:
                status = error.response.status_code if error.response is not None else None
                delay = 300
                if status == 429:
                    value = error.response.headers.get('Retry-After', '')
                    try:
                        delay = max(delay, int(value))
                    except ValueError:
                        try:
                            delay = max(delay, math.ceil(parsedate_to_datetime(value).timestamp() - time.time()))
                        except (TypeError, ValueError, OverflowError):
                            pass
                self.finish('The forum is temporarily unavailable. Saved picks are retained.', delay)
            except Exception:
                self.finish('Automatic forum refresh failed. Saved picks are retained.', 300)
            else:
                self.finish(None, 3600)

    def finish(self, error, delay):
        db = self.get_db()
        try:
            row = db.execute("SELECT value FROM settings WHERE key='owed_sync_status'").fetchone()
            status = json.loads(row[0]) if row else {}
            status.update(syncing=False, sync_error=error)
            if error is None:
                status['last_auto_sync'] = time.time()
            self._put(db, 'owed_sync_status', json.dumps(status))
            self._put(db, 'owed_sync_next', str(time.time() + delay))
            db.commit()
        finally:
            db.close()
