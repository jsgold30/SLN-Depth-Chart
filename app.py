from flask import Flask, render_template, request, jsonify
import requests
from bs4 import BeautifulSoup
import re
import sqlite3
import json
import os

app = Flask(__name__)

DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'depth_charts.db'))


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS team_charts
                    (team_url TEXT PRIMARY KEY, data TEXT,
                     updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
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
            'INSERT OR REPLACE INTO team_charts (team_url, data, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)',
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
            return f"{player['name']} is not listed as PG"
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


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/fetch_roster', methods=['POST'])
def fetch_roster():
    url = request.json.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    try:
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            )
        }
        resp = requests.get(url, timeout=15, headers=headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        # Try to get team name from page title or heading
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

            # Scan through rows to find the actual column header row
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

            # Identify the abilities table by required columns
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
                }

                elig = compute_eligibility(player)
                player['eligible_starter'] = elig['starter']
                player['eligible_backup'] = elig['backup']

                players.append(player)

            if players:
                break

        if not players:
            return jsonify({
                'error': 'Could not find a player abilities table on this page. '
                         'Check the URL and make sure the page is accessible.'
            }), 400

        # Sort by position order then name
        pos_order = {'PG': 0, 'SG': 1, 'SF': 2, 'PF': 3, 'C': 4}
        players.sort(key=lambda p: (pos_order.get(p['pos'], 5), p['name']))

        return jsonify({'players': players, 'team_name': team_name})

    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out. Please check the URL.'}), 400
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to fetch page: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': f'Error parsing roster: {str(e)}'}), 500


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
    url = request.json.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    try:
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            )
        }
        resp = requests.get(url, timeout=15, headers=headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        team_name = ''
        title_tag = soup.find('title')
        if title_tag:
            team_name = title_tag.get_text(strip=True)

        players = []

        for table in soup.find_all('table'):
            all_rows = table.find_all('tr')
            if not all_rows:
                continue

            # Find header row containing 'name' and 'year 1'.
            # Prefer <th>-only matching so indices align with <td> data rows.
            header_row_index = None
            col_names = []
            for i, row in enumerate(all_rows):
                # Try th-only first (proper header cells)
                th_cells = [c.get_text(strip=True).lower() for c in row.find_all('th')]
                if 'name' in th_cells and 'year 1' in th_cells:
                    col_names = th_cells
                    header_row_index = i
                    break
                # Fall back to td-only
                td_cells = [c.get_text(strip=True).lower() for c in row.find_all('td')]
                if 'name' in td_cells and 'year 1' in td_cells:
                    col_names = td_cells
                    header_row_index = i
                    break

            if header_row_index is None:
                continue

            name_idx  = col_names.index('name')
            year1_idx = col_names.index('year 1')

            rows = all_rows[header_row_index + 1:]
            for row in rows:
                cells = [td.get_text(strip=True) for td in row.find_all('td')]
                if len(cells) <= year1_idx:
                    continue
                name = cells[name_idx].strip()
                if not name:
                    continue
                salary = parse_salary(cells[year1_idx])
                players.append({'name': name, 'salary': salary})

            if players:
                break

        if not players:
            return jsonify({'error': 'Could not find salary data (Year 1 column) on this page.'}), 400

        total_salary = sum(p['salary'] for p in players)
        return jsonify({'players': players, 'team_name': team_name, 'total_salary': total_salary})

    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out.'}), 400
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to fetch page: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': f'Error parsing salary data: {str(e)}'}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
