from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
import requests

import app


PICKS_HTML = (Path(__file__).parent / 'fixtures' / 'roster_draft_picks.html').read_text()
EXPECTED = [
    {'year': 2043, 'round': 2, 'original_team': 'Spurs'},
    {'year': 2044, 'round': 2, 'original_team': 'Spurs'},
    {'year': 2045, 'round': 1, 'original_team': 'Spurs'},
    {'year': 2045, 'round': 2, 'original_team': 'Spurs'},
]
URL = 'https://www.simleaguenirvana.com/rosters/roster20.htm'
SALARY_HTML = '<title>Spurs Roster</title><table><tr><td>Name</td><td>Year 1</td></tr><tr><td><a>Test Player</a></td><td>$1,000,000</td></tr></table>'


class RosterDraftPicksTests(unittest.TestCase):
    def parse(self, html):
        return app._parse_draft_picks_from_soup(BeautifulSoup(html, 'html.parser'))

    def test_live_three_column_layout_and_blank_cells(self):
        self.assertEqual(self.parse(PICKS_HTML), EXPECTED)

    def test_nested_tables_navigation_and_unrelated_sections_do_not_add_picks(self):
        html = ('<table><tr><td><a href="#draft">Draft Picks</a></td></tr><tr><td>' + PICKS_HTML
                + '</td></tr><tr><td><table><tr><td>Injuries</td></tr>'
                  '<tr><td>2</td><td>Injured Player</td></tr></table></td></tr></table>')
        self.assertEqual(self.parse(html), EXPECTED)

    def test_single_year_and_sequential_years_remain_supported(self):
        html = ('<table><tbody><tr><th colspan="2">Draft Picks</th></tr>'
                '<tr><th colspan="2">2043</th></tr><tr><td>1</td><td>Spurs</td></tr>'
                '<tr><th colspan="2">2044</th></tr><tr><td>2</td><td>Nets</td></tr>'
                '</tbody></table>')
        self.assertEqual(self.parse(html), [
            {'year': 2043, 'round': 1, 'original_team': 'Spurs'},
            {'year': 2044, 'round': 2, 'original_team': 'Nets'},
        ])

    def test_second_year_received_pick_is_not_assigned_to_first_year(self):
        html = ('<table><tr><td colspan="4">Draft Picks</td></tr>'
                '<tr><td colspan="2">2043</td><td colspan="2">2044</td></tr>'
                '<tr><td></td><td></td><td>1</td><td><a>Nets</a></td></tr></table>')
        self.assertEqual(self.parse(html), [{'year': 2044, 'round': 1, 'original_team': 'Nets'}])

    def test_repeated_pick_rows_are_deduplicated(self):
        self.assertEqual(self.parse(PICKS_HTML + PICKS_HTML), EXPECTED)

    def test_both_salary_routes_return_all_displayed_years_and_refresh_old_cache(self):
        with tempfile.TemporaryDirectory() as temp, patch.multiple(
                app, DB_PATH=os.path.join(temp, 'roster.db'), USE_POSTGRES=False):
            client = app.app.test_client()
            cached = {'players': [{'name': 'Test Player', 'in_rat': 'B'}], 'draft_picks': EXPECTED[:1]}
            db = app.get_db()
            try:
                db.execute('INSERT INTO roster_cache (team_url, data, fetched_at) VALUES (?, ?, ?)',
                           ('salary:' + URL, json.dumps(cached), datetime.utcnow().isoformat()))
                db.commit()
            finally:
                db.close()
            response = requests.Response()
            response.status_code = 200
            response._content = (SALARY_HTML + PICKS_HTML).encode()
            abilities = {'players': [{'name': 'Test Player', 'pos': 'PG', 'in_rating': 'B'}]}
            with patch.object(app, '_fetch_url', return_value=response) as fetch, \
                    patch.object(app, '_parse_roster_from_soup', return_value=abilities):
                result = client.post('/fetch_salary_roster', json={'url': URL})
                self.assertEqual(result.status_code, 200)
                data = result.get_json()
                self.assertEqual(data['draft_picks'], EXPECTED)
                self.assertEqual(data['draft_pick_years'], [2043, 2044, 2045])
                self.assertEqual(data['draft_picks_version'], app.DRAFT_PICKS_PARSER_VERSION)
                self.assertEqual(client.post('/fetch_salary_roster', json={'url': URL}).get_json(), data)
                self.assertEqual(fetch.call_count, 1)
                parsed = client.post('/parse_salary_roster_html', json={'html': SALARY_HTML + PICKS_HTML})
                self.assertEqual(parsed.status_code, 200)
                self.assertEqual(parsed.get_json()['draft_picks'], EXPECTED)
                self.assertEqual(parsed.get_json()['draft_pick_years'], [2043, 2044, 2045])

    def test_empty_year_column_is_covered_even_when_no_picks_are_owned(self):
        html = ('<table><tr><td colspan="4">Draft Picks</td></tr>'
                '<tr><td colspan="2">2043</td><td colspan="2">2044</td></tr>'
                '<tr><td></td><td></td><td>2</td><td>Spurs</td></tr></table>')
        data = app._parse_draft_pick_data(BeautifulSoup(html, 'html.parser'))
        self.assertEqual(data['years'], [2043, 2044])
        self.assertEqual(data['picks'], [{'year': 2044, 'round': 2, 'original_team': 'Spurs'}])


if __name__ == '__main__':
    unittest.main()
