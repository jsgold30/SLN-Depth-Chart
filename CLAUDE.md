# SLN Assistant GM — Project Guide

Flask web app for the Sim League Nirvana (SLN) basketball sim. Lets users build depth charts, run trade machines, manage IR, and explore the draft class. Deployed on Railway.

---

## Repository & Deployment

**Branches**
- `staging` → Railway staging environment
- `main` → Railway production environment

**Deploy command** (Railway auto-bumps `VERSION` on each deploy, which causes merge conflicts — always force push both branches):
```bash
git push origin staging --force && git push origin staging:main --force
```

**Never deploy directly to `main`** — always push to staging first, then force-promote staging to main in the same command.

**Railway environment variables** (set in Railway dashboard, never hardcode):
- `SLN_USERNAME` — SLN login username
- `SLN_PASSWORD` — SLN login password
- `SLN_COOKIE` — session cookie for SLN scraping
- `SCRAPER_API_KEY` — ScraperAPI key for bypassing bot detection
- `DATABASE_URL` — Postgres connection string (auto-set by Railway Postgres plugin)

---

## File Structure

```
app.py                    — Flask backend, all routes, scraping logic, DB
templates/index.html      — Entire frontend (HTML + CSS + JS, ~5000 lines)
static/draft_order_XXXX.csv  — Draft order for the current season
static/logos/             — Team logo images
data/depth_charts.db      — SQLite (local dev only; Railway uses Postgres)
VERSION                   — Auto-bumped by Railway on each deploy
Procfile                  — `web: gunicorn app:app`
```

---

## Key Concepts

### League Year
`TM_LEAGUE_YEAR` (set in `index.html`) drives all pick-range logic. Pick dropdowns show 6 future years: `TM_LEAGUE_YEAR + 1` through `TM_LEAGUE_YEAR + 6`. Update this every season.

### Draft Player Pool
`DRAFT_PLAYER_POOL` in `app.py` (~line 972) is a static list updated each season. Each player needs 6 site-rank fields derived from the 6 SLN sort pages:

| Field | Sort page URL |
|-------|--------------|
| `sri` | `/draft/draftplayers-in.htm` |
| `sro` | `/draft/draftplayers-out.htm` |
| `srh` | `/draft/draftplayers-hn.htm` |
| `srd` | `/draft/draftplayers-df.htm` |
| `srr` | `/draft/draftplayers-reb.htm` |
| `srp` | `/draft/draftplayers-pot.htm` |

Each player's rank = their 1-based position in that sort page's list (1 = best). The app's sort logic is `cmp = b[rf] - a[rf]` with descending default, so rank 1 appears first. **Without these fields, in-app filtering will not match the SLN site order.**

Update process each season:
1. Fetch all 6 sort pages
2. Record each player's position (1–N) on each page as their rank for that category
3. Replace `DRAFT_PLAYER_POOL` entirely — do not merge with prior year's list

### Draft Order CSV
Stored at `static/draft_order_XXXX.csv`. Format:
```
Round 1:
#,Team,Trade
1,VAN,
9,PHX,DEN to PHX
```
- `Trade` field uses `FROM to TEAM` format; split on ` to ` and rendered as arrows in the UI
- Leave `Trade` blank for untraded picks
- Each round starts with a `Round N:` header line followed by `#,Team,Trade` column headers

### IR Logic
`enforceIRLimit()` in `index.html`. Rules:
- `maxIR = Math.max(0, roster.length - 12)` — IR slots = roster size minus 12 active spots
- **Checked (selected) IR players must always be enabled** (`disabled = false`) so users can remove them, even when `maxIR = 0`. The early-return pattern `if (cb.checked) return` is a bug — always explicitly set `cb.disabled = false` for checked boxes.
- Unchecked players get disabled only when `currentIR >= maxIR`

### Trade Machine Picks
`tmAvailablePicks()` in `index.html`. Key behavior:
- Always calls `_picksFromOwed()` for ALL 6 future years as the base (own picks + received picks from DB)
- Roster page supplements **only** the next year with any received picks not yet in the DB
- Do NOT skip `_picksFromOwed` for the next year even when roster page data is loaded — that's how own picks go missing

### IR Styling (dark / light themes)
Dark theme `.ir-label.checked`: `border: 2px solid #38bdf8; box-shadow: 0 0 0 2px #38bdf840`
Light theme `.ir-label.checked`: `border: 2px solid #c17c1a !important; box-shadow: 0 0 0 2px #c17c1a40 !important`
Both themes use `font-weight: 700` on selected labels.

---

## Database

Local dev: SQLite at `data/depth_charts.db`
Production: Postgres via `DATABASE_URL` env var

`USE_POSTGRES` flag in `app.py` switches query style. Key tables:
- `roster_cache` — cached roster scrapes (keyed by team URL)
- `depth_charts` — saved depth chart data per team
- `tm_owed_picks` — trade machine owed picks

Flush salary cache route: `POST /flush_salary_cache`

---

## Scraping

SLN requires authentication. The scraper uses `SLN_COOKIE` for session auth. If scraping breaks, the cookie likely expired and needs to be refreshed in Railway env vars.

ScraperAPI (`SCRAPER_API_KEY`) is used for pages that need bot bypass.

---

## Common Maintenance Tasks

**Each new season:**
1. Update `TM_LEAGUE_YEAR` in `index.html`
2. Replace `DRAFT_PLAYER_POOL` in `app.py` with new class (fetch all 6 SLN sort pages)
3. Create `static/draft_order_XXXX.csv` for the new draft
4. Update `DRAFT_POOL_VERSION` is auto-derived — no manual update needed

**If picks are missing in trade machine:**
Check `_picksFromOwed` is called for all years, not just outer years.

**If IR players can't be removed:**
Check `enforceIRLimit` — checked boxes must always be explicitly enabled.

**If site sort order doesn't match app:**
Re-derive all 6 `sr*` rank fields from the current SLN sort pages.
