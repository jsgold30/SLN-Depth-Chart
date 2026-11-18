const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../templates/index.html'), 'utf8');

function setup() {
  const status = { textContent: '' };
  const c = { document: { getElementById() { return status; } }, Date };
  vm.createContext(c);
  for (const name of ['tmRenderOwedSyncStatus', 'tmRefreshOwedPicks']) {
    const source = html.match(new RegExp(`(?:async )?function ${name}\\([^]*?^\\}`, 'm'))[0];
    vm.runInContext(source, c);
  }
  return { c, status };
}

test('refresh progress keeps saved data availability explicit', () => {
  const { c, status } = setup();
  c.tmRenderOwedSyncStatus({ syncing: true });
  assert.match(status.textContent, /Refreshing owed picks/);
  assert.match(status.textContent, /Saved picks remain available/);
});

test('failed login is visible and successful refresh shows its timestamp', () => {
  const { c, status } = setup();
  c.tmRenderOwedSyncStatus({ sync_error: 'Login expired. Saved picks retained.' });
  assert.match(status.textContent, /Login expired/);
  c.tmRenderOwedSyncStatus({ last_auto_sync: 1790000000 });
  assert.match(status.textContent, /automatically refreshed:/);
});

test('Refresh from SLN asks the server then reloads the saved pick data', async () => {
  const { c } = setup();
  let refreshed = false;
  c.fetch = async (url, options) => {
    assert.equal(url, '/api/picks/sync');
    assert.equal(options.method, 'POST');
    return { ok: true };
  };
  c.tmLoadOwedPicks = async () => { refreshed = true; };
  await c.tmRefreshOwedPicks();
  assert.equal(refreshed, true);
});

test('failed refresh request leaves saved picks in place and displays a message', async () => {
  const { c, status } = setup();
  c.fetch = async () => ({ ok: false });
  c.tmLoadOwedPicks = async () => { throw new Error('Must not discard cached data'); };
  await c.tmRefreshOwedPicks();
  assert.match(status.textContent, /Saved picks remain available/);
});
