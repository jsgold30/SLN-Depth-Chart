# Picks Auto-Sync Implementation Plan

**Overall Progress:** `100%`

## TLDR
Fix 5 bugs so the picks system auto-syncs from SLN without manual paste: Postgres crash, wrong year range, ScraperAPI bypass, stale cookie, and no auto-trigger.

## Critical Decisions
- **Auth**: Auto-login with SLN_USERNAME/SLN_PASSWORD before each sync to always get a fresh cookie — don't rely on stored cookie
- **Year range**: Forum covers y+1 through y+6 (6 years); roster pages still used for y+1 as cross-check but forum is authoritative
- **Auto-sync trigger**: Background thread on app startup; admin "Sync Now" button replaces manual paste UI
- **ScraperAPI**: Use `_fetch_url()` for forum thread so Railway IP block is bypassed

## Tasks

- [ ] 🟩 **Step 1: Fix Postgres LIKE crash**
  - [ ] 🟩 Escape `%` in `LIKE 'salary:%%'` in `flush_salary_cache` and `clear_salary_cache`

- [ ] 🟩 **Step 2: Fix year range**
  - [ ] 🟩 Change `get_forum_pick_years()` from `range(y+2, y+7)` to `range(y+1, y+7)`

- [ ] 🟩 **Step 3: Fix forum fetch to use ScraperAPI**
  - [ ] 🟩 Replace `_scraper.get(SLN_THREAD_URL, ...)` with `_fetch_url(SLN_THREAD_URL, ...)` in `_execute_picks_sync()`

- [ ] 🟩 **Step 4: Auto-login before each sync**
  - [ ] 🟩 Call `_sln_login()` at the start of `_execute_picks_sync()` to get a fresh cookie
  - [ ] 🟩 Fall back to stored cookie if login fails

- [ ] 🟩 **Step 5: Auto-sync on app startup**
  - [ ] 🟩 Trigger `_bg_sync()` in a background thread when app starts
  - [ ] 🟩 Re-sync every 6 hours via a background scheduler loop

- [ ] 🟩 **Step 6: Replace paste UI with Sync Now button**
  - [ ] 🟩 Replace textarea + Parse & Save with a "Sync Picks Now" button
  - [ ] 🟩 Show last synced timestamp and pick count in admin panel
  - [ ] 🟩 Button calls `/api/picks/sync` and polls for result
