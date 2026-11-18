import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests
import app
import draft_pool_sync as sync


def player(name, identifier):
    return {'id': identifier, 'name': name, 'pos': 'PG', 'ht': "6'5\"", 'wt': '200', 'age': '20',
            'in_rat': 'B', 'in_rating': 'B', 'out_rat': 'B', 'out': 'B', 'hn': 'B', 'df': 'B', 'reb': 'B', 'pot': 'A'}


def table(names):
    header = '<tr>' + ''.join('<td>' + text + '</td>' for text in ['Name', 'Pos', 'Height', 'Weight', 'Age', 'In', 'Out', 'Hn', 'Df', 'Reb', 'Pot']) + '</tr>'
    rows = ''.join('<tr>' + ''.join('<td>' + value + '</td>' for value in [name, 'PG', "6\'5\'\'", '200', '20', 'B', 'B', 'B', 'B', 'B', 'A']) + '</tr>' for name in names)
    return '<table>' + header + rows + '</table>'


def response(html, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = html.encode()
    result.url = sync.BASE_URL + 'draftplayers-pot.htm'
    return result


class DraftPoolParsingTests(unittest.TestCase):
    def test_all_six_site_ranks_are_recorded_and_ids_survive_reordering(self):
        names = ['Player A', 'Player B']
        pages = [response(table(names if i % 2 == 0 else names[::-1])) for i in range(6)]
        with patch.object(sync, 'MIN_PLAYERS', 2), patch.object(sync.requests, 'get', side_effect=pages) as get, patch.object(sync.time, 'sleep') as sleep:
            players = sync.fetch_pool([player('Player B', 'existing-note-id')])
        self.assertEqual(get.call_count, 6)
        self.assertEqual(sleep.call_count, 5)
        self.assertEqual(players[1]['id'], 'existing-note-id')
        self.assertEqual(players[0]['ht'], "6'5\"")
        for i, field in enumerate(sync.SORT_FIELDS.values()):
            self.assertEqual(players[0][field], 1 if i % 2 == 0 else 2)

    def test_empty_duplicate_and_inconsistent_pages_are_rejected(self):
        for html in ['<div>Login required</div>', table([]), table(['A', 'A'])]:
            with patch.object(sync, 'MIN_PLAYERS', 2), self.assertRaises(ValueError):
                sync.parse_players(html)
        with patch.object(sync, 'MIN_PLAYERS', 2), patch.object(sync.requests, 'get', side_effect=[response(table(['A', 'B'])), response(table(['A', 'C']))]), patch.object(sync.time, 'sleep'), self.assertRaises(ValueError):
            sync.fetch_pool([])

    def test_version_changes_for_ratings_and_rank_changes_but_not_list_order(self):
        players = [player('A', '1'), player('B', '2')]
        self.assertEqual(sync.pool_version(players), sync.pool_version(players[::-1]))
        changed = copy.deepcopy(players)
        changed[0]['pot'] = 'B'
        self.assertNotEqual(sync.pool_version(players), sync.pool_version(changed))
        changed = copy.deepcopy(players)
        changed[0]['sri'] = 3
        self.assertNotEqual(sync.pool_version(players), sync.pool_version(changed))


class DraftPoolPublishingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = patch.multiple(app, USE_POSTGRES=False, DB_PATH=os.path.join(temp.name, 'draft.db'), DRAFT_POOL_AUTO_SYNC=True)
        config.start()
        self.addCleanup(config.stop)
        self.client = app.app.test_client()
        self.original = self.client.get('/load_draft').get_json()
        self.first = self.original['pool'][0]

    def test_publish_keeps_notes_order_results_and_excludes_already_drafted_players(self):
        state = self.original
        state.update(order=[{'name': 'Team A', 'draftedPlayer': self.first['name']}], currentPick=1, results=[{'pick': 1, 'playerName': self.first['name']}],
                     playerNotes={self.first['id']: 'Keep scouting note'})
        self.assertEqual(self.client.post('/save_draft', json=state).status_code, 200)
        fresh = [dict(self.first, pot='B'), player('New Player', 'sln-new')]
        app.draft_pool_sync.publish(fresh)
        saved = self.client.get('/load_draft').get_json()
        self.assertEqual(saved['order'], state['order'])
        self.assertEqual(saved['results'], state['results'])
        self.assertEqual(saved['playerNotes'], state['playerNotes'])
        self.assertEqual([p['name'] for p in saved['pool']], ['New Player'])
        self.assertEqual(saved['pool_version'], sync.pool_version(fresh))
        self.assertGreater(saved['ts'], state['ts'])

    def test_unchanged_pool_does_not_rewrite_saved_draft_or_resurrect_players(self):
        players = [dict(self.first)]
        app.draft_pool_sync.publish(players)
        before = self.client.get('/load_draft').get_json()
        app.draft_pool_sync.publish(players)
        self.assertEqual(self.client.get('/load_draft').get_json(), before)

    def test_failure_retains_last_valid_pool_and_rate_limit_delay(self):
        app.draft_pool_sync.publish([self.first])
        before = app.draft_pool_sync.snapshot()
        limited = response('', 429)
        limited.headers['Retry-After'] = '900'
        with patch.object(sync, 'fetch_pool', side_effect=requests.HTTPError(response=limited)):
            app.draft_pool_sync.run()
        self.assertEqual(app.draft_pool_sync.snapshot(), before)
        with patch.object(sync.threading, 'Thread') as thread:
            result = self.client.post('/fetch_draft_players', json={}).get_json()
            thread.assert_not_called()
            self.assertIn('saved', result['warning'])
            self.assertEqual(result['players'], [self.first])

    def test_refresh_is_claimed_once_across_requests_and_fallback_is_available(self):
        with patch.object(sync.threading, 'Thread') as thread:
            first = self.client.post('/fetch_draft_players', json={}).get_json()
            second = self.client.post('/fetch_draft_players', json={}).get_json()
            self.assertEqual(thread.call_count, 1)
            self.assertEqual(len(first['players']), 71)
            self.assertTrue(first['refreshing'])
            self.assertTrue(second['refreshing'])

    def test_stale_page_cannot_wipe_notes_or_overwrite_new_live_pool(self):
        stale = self.original
        app.draft_pool_sync.publish([self.first])
        self.assertEqual(self.client.post('/save_draft', json=stale).status_code, 409)
        self.assertEqual(self.client.post('/clear_draft_notes', json={'state_revision': app.DRAFT_STATE_REVISION}).status_code, 409)
        new = self.client.get('/load_draft').get_json()
        new['playerNotes'] = {self.first['id']: 'Current note'}
        self.assertEqual(self.client.post('/save_draft', json=new).status_code, 200)


if __name__ == '__main__':
    unittest.main()
