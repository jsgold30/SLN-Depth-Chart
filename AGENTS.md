# SLN Assistant GM — Codex project guide

Flask app for Sim League Nirvana basketball: depth charts, trades, injured reserve, and draft tools. Railway hosts staging and production.

## Development

Use the existing checkout in this isolated cloud task; do not create a worktree unless requested. Python 3.12 is validated. The committed `venv/` contains old Python 3.9 artifacts; leave it untouched and use `/workspace/.sln-venv`.

Install from the checkout:
```
python3 -m venv /workspace/.sln-venv
/workspace/.sln-venv/bin/python -m pip install -r requirements.txt
```

Run locally with an isolated SQLite database (never use production data for smoke tests):
```
mkdir -p /workspace/.sln-data
DATABASE_URL='' DB_PATH=/workspace/.sln-data/depth_charts.db PYTHONDONTWRITEBYTECODE=1 /workspace/.sln-venv/bin/gunicorn --no-control-socket --bind 127.0.0.1:5000 --workers 1 app:app
```
Validate `/`, `/api/status`, `/api/picks`, and static assets with GET requests; `/fetch_draft_players` uses POST. Verify `/save_chart` then `/load_chart` roundtrip using a disposable SQLite database. Run the tests with `/workspace/.sln-venv/bin/python -m unittest discover -s tests`. There is no frontend build step. Run pip check after dependency installation; add meaningful tests when changing behavior.

Run Draft Center frontend tests with `node --test tests/test_draft_skip.js`. Skip Pick is restricted to Draft Mod admins through server-side password verification; `DRAFT_ADMIN_PASSWORDS` can override the existing moderator passwords as a comma-separated list. The browser keeps the entered password only in memory, clearing it when locked. A pending `skippedPick` preserves original pick numbers and returns to that pick after the borrowed selection. Do not change the one-time reset migration marker for feature releases. `clock_version` rejects old browser writes that cannot preserve skipped state.

## Code map and data

- `app.py`: Flask routes, scraping, database wrapper, draft pool, league year.
- `templates/index.html`: main frontend, styles, JavaScript.
- `static/logos/`: team images. Draft-order CSVs, when present, use `static/draft_order_XXXX.csv`.
- `Procfile`: production `gunicorn app:app` entrypoint.
- `VERSION`: deployment version; avoid unrelated version edits.
- SQLite is the local default; `DATABASE_URL` selects Postgres. `DB_PATH` overrides SQLite location. Actual tables include `team_charts`, `draft_state`, `owed_picks`, `settings`, and `roster_cache`.

## Domain rules to preserve

- League year comes from database settings with `LEAGUE_YEAR_DEFAULT` fallback and is rendered into `TM_LEAGUE_YEAR`. Future picks cover the next six years.
- Draft pool ranks `sri`, `sro`, `srh`, `srd`, `srr`, `srp` are 1-based positions from SLN's six draft sort pages. Replace the pool each season; `DRAFT_POOL_VERSION` is derived from player names.
- Draft CSV rounds start with `Round N:` then `#,Team,Trade`. Trade cells use `FROM to TEAM`; blank means untraded.
- IR limit is `Math.max(0, roster.length - 12)`. Checked IR boxes must remain enabled so users can remove players; disable unchecked boxes only when capacity is reached. Preserve selected styling in both themes.
- `tmAvailablePicks()` must include `_picksFromOwed()` for all six future years. Roster data only supplements next-year received picks.

## External integrations and deployment

Increment `VERSION` once for each release pushed to staging and production (4.0, 4.1, 4.2, ...). Both branches receive the same release version. The user has requested this versioning convention; deploy when requested.

Draft Center release 4.1 applies the `2042-reset-notes-v1` migration once per database on first Draft Center access: back up the prior state in `settings`, clear order/results, restore all players, and merge the 36 supplied scouting reports from `data/draft_scout_notes_2042.json`. Do not change that marker merely to release another version; doing so would reset an active draft again. Draft writes carry `state_revision` to reject old browser sessions and caches. Run `python -m unittest discover -s tests` with the development interpreter when changing this workflow.

`SLN_COOKIE` supports authenticated SLN scraping. Camp history uses Google Sheets; stats use slnstatbook.com. `/api/faq-query` currently uses Anthropic and `ANTHROPIC_API_KEY`; changing coding assistants does not migrate this runtime feature. Credentials belong in environment settings, never code or chat. Core local development needs none of these optional credentials.

The legacy `CLAUDE.md` is historical reference and may be stale (for example table names and scraper settings). Check current code before following it. Its deployment commands do not authorize a deployment or force push. `staging` and `main` are documented Railway staging and production branches; deploy only when the user explicitly requests it, after checking current branch and remote state. Do not force-push by default.
