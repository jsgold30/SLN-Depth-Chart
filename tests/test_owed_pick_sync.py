import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

import requests
import app
import owed_pick_sync as sync


POST = '2045:\nSAS 1st to ORL\n2046:\nNone\n2047:\nNJN 1st to SAS\n2048:\nNone'
POST_HTML = '''<div class="postbody"><div class="content"><blockquote>2045:<br>ORL 1st to SAS</blockquote>
<strong>2045:</strong><br><a>SAS</a> 1st to <a>ORL</a><br>2046:<br>None<br>2047:<br>NJN 1st to SAS<br>2048:<br>None
</div></div><div class="postbody"><div class="content">2045:<br>BOS 1st to ORL</div></div>'''
LOGIN_HTML = '<form action="./ucp.php?mode=login"><input type="hidden" name="form_token" value="test-csrf"><input name="username"><input name="password" type="password"></form>'


def response(html, status=200):
    result = requests.Response()
    result.status_code = status
    result.url = sync.FORUM_URL
    result._content = html.encode()
    return result


class ForumReaderTests(unittest.TestCase):
    def test_first_post_only_preserves_inline_links_and_ignores_quoted_old_picks(self):
        text = sync.first_post_text(POST_HTML)
        self.assertEqual(app.parse_owed_picks_from_thread(text), app.parse_owed_picks_from_thread(POST))
        self.assertNotIn('BOS', text)
        self.assertNotIn('ORL 1st to SAS', text)

    def test_login_screen_is_never_treated_as_pick_data(self):
        with self.assertRaises(sync.ForumLoginRequired):
            sync.first_post_text(LOGIN_HTML)

    def session(self):
        session = Mock()
        session.headers = {}
        manager = MagicMock()
        manager.__enter__.return_value = session
        return session, manager

    def test_existing_cookie_is_reused_without_logging_in_again(self):
        session, manager = self.session()
        session.get.return_value = response(POST_HTML)
        with patch.object(sync.requests, 'Session', return_value=manager):
            sync.fetch_first_post({'cookie': 'test-cookie'})
        self.assertEqual(session.headers['Cookie'], 'test-cookie')
        session.post.assert_not_called()

    def test_expired_cookie_can_fall_back_to_username_password_with_csrf_fields(self):
        session, manager = self.session()
        session.get.side_effect = [response(LOGIN_HTML), response(POST_HTML)]
        session.post.return_value = response('', 302)
        with patch.object(sync.requests, 'Session', return_value=manager):
            text = sync.fetch_first_post({'cookie': 'expired-test-cookie', 'username': 'test-reader', 'password': 'test-password'})
        self.assertIn('SAS 1st to ORL', text)
        arguments = session.post.call_args.kwargs
        self.assertEqual(arguments['data']['form_token'], 'test-csrf')
        self.assertEqual(arguments['data']['username'], 'test-reader')
        self.assertFalse(arguments['allow_redirects'])
        self.assertNotIn('Cookie', session.headers)

    def test_no_credentials_are_posted_to_an_unrecognized_login_host(self):
        session, manager = self.session()
        session.get.return_value = response(LOGIN_HTML.replace('./ucp.php?mode=login', 'https://example.com/login'))
        with patch.object(sync.requests, 'Session', return_value=manager), self.assertRaises(sync.ForumLoginRequired):
            sync.fetch_first_post({'username': 'test-reader', 'password': 'test-password'})
        session.post.assert_not_called()


class OwedSyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = patch.multiple(app, USE_POSTGRES=False, DB_PATH=os.path.join(temp.name, 'picks.db'), OWED_PICKS_AUTO_SYNC=True)
        config.start()
        self.addCleanup(config.stop)
        credentials = patch.object(app, '_forum_credentials', return_value={'cookie': 'test-cookie'})
        credentials.start()
        self.addCleanup(credentials.stop)
        self.client = app.app.test_client()
        self.client.post('/api/picks/from-paste', json={'text': POST})
        db = app.get_db()
        try:
            self.snapshot = db.execute('SELECT data FROM owed_picks WHERE id=1').fetchone()[0]
        finally:
            db.close()

    def state(self):
        db = app.get_db()
        try:
            return json.loads(db.execute('SELECT data FROM owed_picks WHERE id=1').fetchone()[0])
        finally:
            db.close()

    def test_background_refresh_is_claimed_only_once_and_success_is_cached(self):
        with patch.object(sync.threading, 'Thread') as thread:
            first = self.client.get('/api/picks').get_json()
            self.assertTrue(first['syncing'])
            self.client.get('/api/picks')
            self.assertEqual(thread.call_count, 1)
            with patch.object(sync, 'fetch_first_post', return_value=POST.replace('SAS 1st to ORL', 'BOS 1st to SAS')):
                app.owed_pick_sync.run({'cookie': 'test-cookie'}, self.snapshot)
            data = self.client.get('/api/picks').get_json()
            self.assertFalse(data['syncing'])
            self.assertIsNone(data['sync_error'])
            self.assertIn('last_auto_sync', data)
            self.assertEqual(data['owed'][0]['from_abbr'], 'BOS')
            self.assertEqual(thread.call_count, 1)
            self.assertEqual(data['league_year'], 2043)

    def test_real_background_thread_completes_and_api_exposes_refreshed_picks(self):
        real_thread = sync.threading.Thread
        threads = []

        def create(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            threads.append(thread)
            return thread

        with patch.object(sync.threading, 'Thread', side_effect=create), \
                patch.object(sync, 'fetch_first_post', return_value=POST.replace('SAS 1st to ORL', 'BOS 1st to SAS')):
            first = self.client.get('/api/picks').get_json()
            self.assertTrue(first['syncing'])
            threads[0].join(timeout=2)
            self.assertFalse(threads[0].is_alive())
            refreshed = self.client.get('/api/picks').get_json()
            self.assertFalse(refreshed['syncing'])
            self.assertEqual(refreshed['owed'][0]['from_abbr'], 'BOS')
            self.assertEqual(len(threads), 1)

    def test_missing_credentials_do_not_start_network_work_or_change_picks(self):
        with patch.object(app, '_forum_credentials', return_value={}), patch.object(sync.threading, 'Thread') as thread:
            data = self.client.get('/api/picks').get_json()
            self.assertFalse(data['syncing'])
            self.assertIn('SLN login', data['sync_error'])
            thread.assert_not_called()
        self.assertEqual(self.state(), json.loads(self.snapshot))

    def test_login_failure_preserves_picks_and_applies_retry_backoff(self):
        with patch.object(sync.threading, 'Thread') as thread:
            self.client.get('/api/picks')
            with patch.object(sync, 'fetch_first_post', side_effect=sync.ForumLoginRequired('Login expired. Saved picks retained.')):
                app.owed_pick_sync.run({}, self.snapshot)
            data = self.client.get('/api/picks').get_json()
            self.assertFalse(data['syncing'])
            self.assertIn('Login expired', data['sync_error'])
            self.client.post('/api/picks/sync')
            self.assertEqual(thread.call_count, 1)
            self.assertEqual(self.state(), json.loads(self.snapshot))

    def test_partial_post_never_overwrites_the_complete_saved_record(self):
        with patch.object(sync, 'fetch_first_post', return_value='2045:\nBOS 1st to SAS'):
            app.owed_pick_sync.run({}, self.snapshot)
        self.assertEqual(self.state(), json.loads(self.snapshot))

    def test_manual_paste_during_refresh_wins_over_the_late_job(self):
        self.client.post('/api/picks/from-paste', json={'text': '2045:\nNJN 2nd to SAS'})
        newer = self.state()
        with patch.object(sync, 'fetch_first_post', return_value=POST):
            app.owed_pick_sync.run({}, self.snapshot)
        self.assertEqual(self.state(), newer)

    def test_rate_limit_retry_after_is_respected_and_does_not_erase_data(self):
        limited = response('', 429)
        limited.headers['Retry-After'] = '600'
        error = requests.HTTPError(response=limited)
        with patch.object(sync, 'fetch_first_post', side_effect=error):
            app.owed_pick_sync.run({}, self.snapshot)
        db = app.get_db()
        try:
            next_check = float(db.execute("SELECT value FROM settings WHERE key='owed_sync_next'").fetchone()[0])
            self.assertGreater(next_check - time.time(), 590)
        finally:
            db.close()
        self.assertEqual(self.state(), json.loads(self.snapshot))

    def test_year_change_still_selects_the_correct_four_years_after_auto_import(self):
        with patch.object(sync, 'fetch_first_post', return_value=POST + '\n2049:\nORL 2nd to SAS'):
            app.owed_pick_sync.run({}, self.snapshot)
        self.client.post('/api/settings/league-year', json={'year': 2044})
        data = self.client.get('/api/picks').get_json()
        self.assertEqual(data['owed_years'], [2046, 2047, 2048, 2049])
        self.assertEqual(data['owed'][-1]['year'], 2049)


if __name__ == '__main__':
    unittest.main()
