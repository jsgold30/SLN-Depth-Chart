import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app


class DraftStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = patch.multiple(app, USE_POSTGRES=False,
                                     DB_PATH=os.path.join(self.temp.name, 'draft.db'))
        self.config.start()
        self.addCleanup(self.config.stop)
        self.client = app.app.test_client()
        path = Path(app.__file__).parent / 'data' / 'draft_scout_notes_2042.json'
        self.reports = json.loads(path.read_text())['notes_by_name']

    def store(self, state):
        db = app.get_db()
        try:
            db.execute('INSERT INTO draft_state (id, data) VALUES (1, ?)', (json.dumps(state),))
            db.commit()
        finally:
            db.close()

    def load(self):
        response = self.client.get('/load_draft')
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_import_matches_every_supplied_report_and_restores_full_pool(self):
        state = self.load()
        self.assertEqual(len(state['pool']), 71)
        self.assertEqual(len(self.reports), 36)
        self.assertEqual(len(state['playerNotes']), 36)
        for player in state['pool']:
            if player['name'] in self.reports:
                self.assertEqual(state['playerNotes'][player['id']], self.reports[player['name']])
        self.assertEqual(state['pool_version'], app.DRAFT_POOL_VERSION)
        self.assertEqual(state['state_revision'], app.DRAFT_STATE_REVISION)

    def test_reset_preserves_notes_other_fields_and_database_backup(self):
        original = {'pool': [], 'order': [{'name': 'Old team'}], 'results': [{'playerName': 'Old player'}],
                    'currentPick': 5, 'playerNotes': {'1': 'Existing note', '71': 'Keep this too'},
                    'custom': {'keep': True}, 'ts': 9999999999999}
        self.store(original)
        state = self.load()
        self.assertEqual(state['order'], [])
        self.assertEqual(state['results'], [])
        self.assertEqual(state['currentPick'], 0)
        self.assertEqual(state['custom'], original['custom'])
        self.assertGreater(state['ts'], original['ts'])
        self.assertTrue(state['playerNotes']['1'].startswith('Existing note\n\n'))
        self.assertEqual(state['playerNotes']['71'], 'Keep this too')
        db = app.get_db()
        try:
            backup = db.execute('SELECT value FROM settings WHERE key=?',
                                ('draft_backup_' + app.DRAFT_STATE_REVISION,)).fetchone()
            self.assertEqual(json.loads(backup[0]), original)
        finally:
            db.close()
        self.assertEqual(self.load(), state)

    def test_later_picks_and_edited_notes_survive_reload(self):
        state = self.load()
        state.update(order=[{'name': 'New team', 'draftedPlayer': state['pool'][0]['name']}], results=[{'playerName': state['pool'][0]['name']}],
                     currentPick=1, playerNotes={'1': 'Edited note'})
        response = self.client.post('/save_draft', json=state)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.load(), state)

    def test_stale_browser_cannot_restore_old_draft_or_clear_new_notes(self):
        state = self.load()
        stale = copy.deepcopy(state)
        stale.pop('state_revision')
        stale['order'] = [{'name': 'Old cached order'}]
        self.assertEqual(self.client.post('/save_draft', json=stale).status_code, 409)
        self.assertEqual(self.client.post('/clear_draft_notes').status_code, 409)
        self.assertEqual(self.load(), state)

    def test_explicit_note_clear_is_not_reseeded(self):
        self.load()
        response = self.client.post('/clear_draft_notes', json={'state_revision': app.DRAFT_STATE_REVISION})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.load()['playerNotes'], {})
        self.assertEqual(self.load()['playerNotes'], {})

    def test_failed_import_rolls_back_marker_and_retains_original(self):
        original = {'playerNotes': [], 'order': [{'name': 'Keep until import succeeds'}]}
        self.store(original)
        with self.assertRaises(AttributeError):
            app.get_draft_db()
        db = app.get_db()
        try:
            self.assertIsNone(db.execute('SELECT value FROM settings WHERE key=?',
                                        ('draft_migration_' + app.DRAFT_STATE_REVISION,)).fetchone())
            state = db.execute('SELECT data FROM draft_state WHERE id=1').fetchone()
            self.assertEqual(json.loads(state[0]), original)
        finally:
            db.close()

    def test_page_renders_current_version_and_revision(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        version = Path(app.__file__).with_name('VERSION').read_text().strip()
        self.assertIn(('v' + version).encode(), response.data)
        self.assertIn(('const DC_STATE_REVISION = "' + app.DRAFT_STATE_REVISION + '"').encode(), response.data)


if __name__ == '__main__':
    unittest.main()
