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

test('a bundled class change reloads the full pool during polling', async () => {
  let reloads = 0;
  const c = load('dcPollServer', {
    dcSkipBusy: false, dcSavedPoolVersion: 'old', dcServerReached: false,
    fetch: async () => ({ ok: true, json: async () => ({ ts: 123, pool_version: 'new' }) }),
    dcFetchPlayers: async () => { reloads++; }
  });
  await c.dcPollServer();
  assert.equal(reloads, 1);
});

test('loading the bundled class preserves notes and excludes prior selections', async () => {
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
      return { json: async () => ({ pool_version: 'new', players: [
        { id: 'stable-player', name: 'Current Player' }, { id: 'drafted', name: 'Already drafted' }
      ] }) };
    },
    document: { getElementById() { return status; } },
    dcRenderPool() {}, dcLoadQueue() {}, dcRenderQueue() {}, dcAutoSelectFromQueue() {}, dcRenderOrder() {}, dcRenderLog() {},
    Date
  });
  await c.dcFetchPlayers();
  assert.equal(loads, 1);
  assert.equal(clears, 0);
  assert.equal(c.dcPlayerNotes['stable-player'], 'Keep this scouting note');
  assert.equal(c.dcPool.length, 1);
  assert.equal(c.dcPool[0].id, 'stable-player');
  assert.equal(c.dcSavedPoolVersion, 'new');
  assert.equal(status.textContent, 'Draft resumed from saved state.');
});

test('another user drafting a player removes them from the pool and local queue', async () => {
  const players = [{ id: '1', name: 'Player A' }, { id: '2', name: 'Player B' }];
  const c = load('dcPollServer', {
    dcSkipBusy: false, dcSavedPoolVersion: 'bundled', dcLastSyncStamp: 10,
    dcCurrentPick: 0, dcAllPlayers: players, dcPool: [...players], dcQueue: ['1', '2'],
    fetch: async () => ({ ok: true, json: async () => ({ ts: 11, pool_version: 'bundled',
      order: [{ draftedPlayer: 'Player A' }], currentPick: 1,
      results: [{ playerName: 'Player A' }], playerNotes: { '1': 'Keep note' },
      pool: players // Even a stale saved pool cannot restore a selected player.
    }) }),
    localStorage: { setItem() {} }, DC_LS_KEY: 'test',
    dcRenderPool() {}, dcSaveQueue() {}, dcRenderOrder() {}, dcRenderLog() {},
    dcRenderQueue() {}, dcAutoSelectFromQueue() {}, console
  });
  await c.dcPollServer();
  assert.deepEqual(Array.from(c.dcPool, p => p.name), ['Player B']);
  assert.deepEqual(Array.from(c.dcQueue), ['2']);
  assert.equal(c.dcPlayerNotes['1'], 'Keep note');
});
