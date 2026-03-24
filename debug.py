import requests
from bs4 import BeautifulSoup

url = "https://www.simleaguenirvana.com/rosters/roster20.htm"
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

try:
    resp = requests.get(url, headers=headers, timeout=15)
    print(f"Status code: {resp.status_code}")
    print(f"Page length: {len(resp.text)} characters")
    print(f"First 500 chars of HTML:\n{resp.text[:500]}")
    print("\n=== ALL TABLES FOUND ===\n")
    soup = BeautifulSoup(resp.text, 'html.parser')
    tables = soup.find_all('table')
    print(f"Number of tables: {len(tables)}\n")
    for i, table in enumerate(tables):
        header_row = table.find('tr')
        if header_row:
            cols = [c.get_text(strip=True) for c in header_row.find_all(['th', 'td'])]
            print(f"Table {i+1}: {cols}")
            rows = table.find_all('tr')
            if len(rows) > 1:
                first_data = [c.get_text(strip=True) for c in rows[1].find_all('td')]
                print(f"  First row: {first_data}")
        print()
except Exception as e:
    print(f"ERROR: {e}")
