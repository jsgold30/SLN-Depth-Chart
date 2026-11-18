const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../templates/index.html'), 'utf8');

function load(name, context) {
  vm.createContext(context);
  const source = html.match(new RegExp(`async function ${name}\\([^]*?^\\}`, 'm'))[0];
  vm.runInContext(source, context);
  return context;
}

test('changed pool version reloads the authoritative full pool during polling', async () => {
  let reloads = 0;
  const c = load('dcPollServer', {
    dcSkipBusy: false, dcPoolRefreshing: false, dcSavedPoolVersion: 'old', dcServerReached: false,
    fetch: async () => ({ ok: true, json: async () => ({ ts: 123, pool_version: 'new' }) }),
    dcFetchPlayers: async () => { reloads++; }
  });
  await c.dcPollServer();
  assert.equal(reloads, 1);
});

test('refreshing a draft pool preserves notes and already drafted exclusions', async () => {
  const status = { textContent: '' };
  let loads = 0;
  let clears = 0;
  const c = load('dcFetchPlayers', {
    dcPlayersFetching: false, dcPlayersLoaded: false, dcSavedPoolVersion: 'old',
    dcPlayerNotes: { 'stable-player': 'Keep this scouting note' },
    dcResults: [{ playerName: 'Already drafted' }], dcOrder: [{}],
    dcLoadFromServer: async () => { loads++; return true; },
    fetch: async url => {
      if (url === '/clear_draft_notes') clears++;
      assert.equal(url, '/fetch_draft_players');
      return { json: async () => ({ source: 'sln', fetched_at: 1790000000, pool_version: 'new', players: [
        { id: 'stable-player', name: 'Current Player' }, { id: 'drafted', name: 'Already drafted' }
      ] }) };
    },
    document: { getElementById() { return status; } },
    dcRenderPool() {}, dcLoadQueue() {}, dcRenderQueue() {}, dcAutoSelectFromQueue() {}, dcRenderOrder() {}, dcRenderLog() {},
    Date
  });
  await c.dcFetchPlayers();
  assert.equal(loads, 2);
  assert.equal(clears, 0);
  assert.equal(c.dcPlayerNotes['stable-player'], 'Keep this scouting note');
  assert.equal(c.dcPool.length, 1);
  assert.equal(c.dcPool[0].id, 'stable-player');
  assert.equal(c.dcSavedPoolVersion, 'new');
  assert.match(status.textContent, /refreshed from SLN/);
});
