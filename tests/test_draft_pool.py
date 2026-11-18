import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app


class BundledDraftPoolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = patch.multiple(app, USE_POSTGRES=False, DB_PATH=os.path.join(temp.name, 'draft.db'))
        config.start()
        self.addCleanup(config.stop)
        self.client = app.app.test_client()
        self.state = self.client.get('/load_draft').get_json()

    def test_pool_uses_only_bundled_players_even_with_a_legacy_live_cache(self):
        db = app.get_db()
        db.execute('INSERT INTO settings (key, value) VALUES (?, ?)',
                   ('draft_pool_live', json.dumps({'players': [{'name': 'Wrong class'}], 'pool_version': 'live-old'})))
        db.commit()
        db.close()
        with patch.object(app.requests, 'get', side_effect=AssertionError('Must not fetch SLN')):
            response = self.client.post('/fetch_draft_players', json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'players': self.state['pool'], 'pool_version': app.DRAFT_POOL_VERSION})

    def test_rollback_restores_static_players_and_preserves_active_draft(self):
        state = self.state
        selected = state['pool'][0]
        state.update(pool_version='sln-live-hash', pool=[{'id': 'new', 'name': 'Wrong class'}],
                     order=[{'name': 'Team A', 'draftedPlayer': selected['name']}, {'name': 'Team B'}],
                     currentPick=1, skippedPick=0,
                     results=[{'pick': 1, 'playerName': selected['name']}],
                     playerNotes={selected['id']: 'Preserve this note'}, ts=9999999999999)
        db = app.get_db()
        db.execute('UPDATE draft_state SET data=? WHERE id=1', (json.dumps(state),))
        db.commit()
        db.close()
        restored = self.client.get('/load_draft').get_json()
        for field in ['order', 'results', 'playerNotes', 'currentPick', 'skippedPick', 'state_revision']:
            self.assertEqual(restored[field], state[field], field)
        self.assertEqual(restored['pool_version'], app.DRAFT_POOL_VERSION)
        self.assertEqual(restored['pool'], app._draft_pool_players(state['results']))
        self.assertNotIn(selected['name'], [p['name'] for p in restored['pool']])
        self.assertGreater(restored['ts'], state['ts'])
        self.assertEqual(self.client.get('/load_draft').get_json(), restored)

    def test_save_excludes_drafted_players_and_undo_restores_only_undone_selection(self):
        state = self.state
        first, second = state['pool'][:2]
        state.update(order=[{'name': 'Team A', 'draftedPlayer': first['name']},
                            {'name': 'Team B', 'draftedPlayer': second['name']}], currentPick=2,
                     results=[{'pick': 1, 'playerName': first['name']}, {'pick': 2, 'playerName': second['name']}])
        self.assertEqual(self.client.post('/save_draft', json=state).status_code, 200)
        saved = self.client.get('/load_draft').get_json()
        self.assertEqual(len(saved['pool']), 69)
        self.assertFalse({first['name'], second['name']} & {p['name'] for p in saved['pool']})
        saved['results'].pop()
        saved['order'][1].pop('draftedPlayer')
        saved['currentPick'] = 1
        self.assertEqual(self.client.post('/save_draft', json=saved).status_code, 200)
        undone = self.client.get('/load_draft').get_json()
        self.assertEqual(len(undone['pool']), 70)
        self.assertIn(second, undone['pool'])
        self.assertNotIn(first, undone['pool'])

    def test_stale_live_pool_writes_and_legacy_note_clears_are_rejected(self):
        stale = copy.deepcopy(self.state)
        stale.update(pool_version='sln-live-hash', playerNotes={})
        self.assertEqual(self.client.post('/save_draft', json=stale).status_code, 409)
        self.assertEqual(self.client.post('/clear_draft_notes',
                                         json={'state_revision': app.DRAFT_STATE_REVISION}).status_code, 409)
        self.assertEqual(self.client.get('/load_draft').get_json(), self.state)


if __name__ == '__main__':
    unittest.main()
