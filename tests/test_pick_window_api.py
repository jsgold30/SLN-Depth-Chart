import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app


POST = '''2043:
SAS 2nd to ORL
2044:
ORL 1st to SAS
2045:
SAS 1st to ORL
2046:
None
2047:
NJN 1st to SAS
2048:
SAS 2nd to ORL
2049:
ORL 2nd to SAS
'''


class PickWindowApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = patch.multiple(app, USE_POSTGRES=False, DB_PATH=os.path.join(temp.name, 'picks.db'))
        config.start()
        self.addCleanup(config.stop)
        self.client = app.app.test_client()

    def paste(self, text=POST):
        return self.client.post('/api/picks/from-paste', json={'text': text})

    def test_current_season_defaults_to_2043_and_four_outer_years_are_isolated(self):
        self.assertEqual(self.client.get('/api/settings/league-year').get_json()['league_year'], 2043)
        result = self.paste()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()['years_saved'], [2045, 2046, 2047, 2048])
        data = self.client.get('/api/picks').get_json()
        self.assertEqual(data['owed_years'], [2045, 2046, 2047, 2048])
        self.assertEqual([p['year'] for p in data['owed']], [2045, 2047, 2048])

    def test_changing_league_year_moves_sources_and_reveals_previously_saved_2049(self):
        self.paste()
        result = self.client.post('/api/settings/league-year', json={'year': 2044})
        self.assertEqual(result.status_code, 200)
        data = self.client.get('/api/picks').get_json()
        self.assertEqual(data['league_year'], 2044)
        self.assertEqual(data['owed_years'], [2046, 2047, 2048, 2049])
        self.assertEqual([p['year'] for p in data['owed']], [2047, 2048, 2049])
        self.assertEqual(self.client.get('/api/settings/league-year').get_json()['league_year'], 2044)
        self.client.post('/api/settings/league-year', json={'year': 2043})
        self.assertEqual(self.client.get('/api/picks').get_json()['owed_years'], [2045, 2046, 2047, 2048])

    def test_partial_paste_replaces_only_pasted_year_and_allows_zero_owed_picks(self):
        self.paste()
        self.assertEqual(self.paste('2045:\nNone').status_code, 200)
        data = self.client.get('/api/picks').get_json()
        self.assertEqual(data['owed_years'], [2045, 2046, 2047, 2048])
        self.assertEqual([p['year'] for p in data['owed']], [2047, 2048])
        self.assertEqual(self.paste('2046:\nBOS 2nd to SAS').status_code, 200)
        self.assertEqual(len(self.client.get('/api/picks').get_json()['owed']), 3)

    def test_no_valid_years_or_unreadable_transfer_does_not_overwrite_saved_data(self):
        self.paste()
        before = self.client.get('/api/picks').get_json()
        for text in ['garbage', '2043:\nSAS 1st to ORL', '2045:\nUNKNOWN 1st to ORL']:
            self.assertEqual(self.paste(text).status_code, 400)
            self.assertEqual(self.client.get('/api/picks').get_json(), before)

    def test_zero_debt_years_can_be_saved_without_any_transfers(self):
        result = self.paste('2045:\nNone\n2046:\nNone\n2047:\nNone\n2048:\nNone')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()['total'], 0)
        data = self.client.get('/api/picks').get_json()
        self.assertEqual(data['owed'], [])
        self.assertEqual(data['owed_years'], [2045, 2046, 2047, 2048])

    def test_swap_metadata_is_retained_and_legacy_list_saves_remain_readable(self):
        result = self.paste('2047:\nBetter of SAS/BOS 1st to ORL, Worse to NJN')
        self.assertEqual(result.status_code, 200)
        data = self.client.get('/api/picks').get_json()
        self.assertEqual({p['qualifier'] for p in data['owed']}, {'better', 'worse'})
        legacy = [{'year': 2045, 'round': 1, 'from_abbr': 'SAS', 'to_abbr': 'ORL'}]
        db = app.get_db()
        try:
            db.execute('UPDATE owed_picks SET data=? WHERE id=1', (json.dumps(legacy),))
            db.commit()
        finally:
            db.close()
        data = self.client.get('/api/picks').get_json()
        self.assertEqual(data['owed'], legacy)
        self.assertEqual(data['owed_years'], [2045])

    def test_default_input_and_invalid_current_seasons(self):
        for year in [True, 2019, 2061, '2044']:
            self.assertEqual(self.client.post('/api/settings/league-year', json={'year': year}).status_code, 400)
        self.assertEqual(self.client.get('/api/settings/league-year').get_json()['league_year'], 2043)
