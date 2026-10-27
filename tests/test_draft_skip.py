import copy
import os
import tempfile
import unittest
from unittest.mock import patch

import app


class DraftSkipTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = patch.multiple(app, USE_POSTGRES=False, DB_PATH=os.path.join(temp.name, 'draft.db'),
                                DRAFT_ADMIN_PASSWORDS=('test-admin',))
        config.start()
        self.addCleanup(config.stop)
        self.client = app.app.test_client()
        self.admin = {'X-Draft-Admin': 'test-admin'}
        state = self.load()
        state.update(order=[{'name': n, 'abbr': n, 'draftedPlayer': None} for n in ['A', 'B', 'C']],
                     currentPick=0, skippedPick=None, results=[])
        self.assertEqual(self.client.post('/save_draft', json=state, headers=self.admin).status_code, 200)

    def load(self):
        return self.client.get('/load_draft').get_json()

    def skip(self, headers=None, state=None):
        state = state or self.load()
        return self.client.post('/skip_draft_pick', json={'currentPick': state['currentPick'], 'ts': state['ts']},
                                headers=headers or {})

    def test_only_authenticated_admin_can_unlock_and_skip(self):
        original = self.load()
        for headers in [{}, {'X-Draft-Admin': 'incorrect'}]:
            self.assertEqual(self.client.post('/draft_admin_auth', headers=headers).status_code, 403)
            self.assertEqual(self.skip(headers).status_code, 403)
            self.assertEqual(self.load(), original)
        self.assertEqual(self.client.post('/draft_admin_auth', headers=self.admin).status_code, 200)
        self.assertEqual(self.skip(self.admin).status_code, 200)

    def test_next_team_picks_then_skipped_team_then_third_team(self):
        skipped = self.skip(self.admin).get_json()
        self.assertEqual((skipped['currentPick'], skipped['skippedPick']), (1, 0))
        self.assertEqual([p['name'] for p in skipped['order']], ['A', 'B', 'C'])
        self.assertEqual(self.load(), skipped)
        skipped['order'][1]['draftedPlayer'] = 'Player B'
        skipped.update(currentPick=0, skippedPick=None)
        skipped['results'].append({'pick': 2, 'playerName': 'Player B'})
        self.assertEqual(self.client.post('/save_draft', json=skipped).status_code, 200)
        returned = self.load()
        self.assertEqual(returned['currentPick'], 0)
        returned['order'][0]['draftedPlayer'] = 'Player A'
        returned.update(currentPick=2)
        returned['results'].append({'pick': 1, 'playerName': 'Player A'})
        self.assertEqual(self.client.post('/save_draft', json=returned).status_code, 200)
        self.assertEqual(self.load()['currentPick'], 2)

    def test_cannot_forge_skip_or_avoid_return_through_save(self):
        forged = self.load()
        forged.update(currentPick=1, skippedPick=0)
        self.assertEqual(self.client.post('/save_draft', json=forged).status_code, 403)
        forged['skippedPick'] = None
        self.assertEqual(self.client.post('/save_draft', json=forged).status_code, 403)
        skipped = self.skip(self.admin).get_json()
        skipped.update(currentPick=2, skippedPick=None)
        skipped['order'][1]['draftedPlayer'] = 'Player B'
        self.assertEqual(self.client.post('/save_draft', json=skipped).status_code, 403)

    def test_stale_request_and_second_skip_are_rejected(self):
        stale = self.load()
        stale['ts'] -= 1
        self.assertEqual(self.skip(self.admin, stale).status_code, 409)
        self.assertEqual(self.skip(self.admin).status_code, 200)
        original = self.load()
        self.assertEqual(self.skip(self.admin).status_code, 409)
        self.assertEqual(self.load(), original)

    def test_final_pick_cannot_be_skipped(self):
        state = self.load()
        state['currentPick'] = 2
        self.client.post('/save_draft', json=state, headers=self.admin)
        self.assertEqual(self.skip(self.admin).status_code, 400)
        self.assertEqual(self.load(), state)

    def test_already_completed_next_pick_is_bypassed(self):
        state = self.load()
        state['order'][1]['draftedPlayer'] = '(Rescinded)'
        self.client.post('/save_draft', json=state, headers=self.admin)
        result = self.skip(self.admin).get_json()
        self.assertEqual((result['currentPick'], result['skippedPick']), (2, 0))

    def test_old_browser_cannot_overwrite_skip_state(self):
        state = self.load()
        state.pop('clock_version')
        self.assertEqual(self.client.post('/save_draft', json=state, headers=self.admin).status_code, 409)


if __name__ == '__main__':
    unittest.main()
