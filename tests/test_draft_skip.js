const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../templates/index.html'), 'utf8');
function setup() {
  const elements = {};
  const element = () => ({
    style: {}, dataset: {}, classList: { add() {}, remove() {} }, children: [],
    appendChild(child) { this.children.push(child); }, addEventListener() {}, scrollIntoView() {}
  });
  const context = {
    console, Date, Math, JSON,
    document: {
      getElementById(id) { return elements[id] ||= element(); },
      createElement: element, querySelector() { return null; }, querySelectorAll() { return []; }
    },
    alert(message) { throw new Error(message); }, confirm() { return true; },
    localStorage: { setItem() {} },
    dcOrder: ['A', 'B', 'C'].map(name => ({ name, abbr: name, round: 1, draftedPlayer: null })),
    dcCurrentPick: 1, dcSkippedPick: 0, dcSkipBusy: false, dcModUnlocked: true,
    dcModPassword: 'test-admin', dcLastSyncStamp: 10, dcPlayerNotes: {}, dcSavedPoolVersion: 'pool',
    dcResults: [], dcQueue: [], dcSelectedId: null, dcServerReached: true,
    DC_LS_KEY: 'draft-test', DC_STATE_REVISION: 'test', DC_CLOCK_VERSION: 2,
    dcSave() {}, dcSaveQueue() {}, dcRenderPool() {}, dcRenderLog() {}, dcRenderQueue() {},
    dcAutoSelectFromQueue() {}, dcUpdateDraftBtn() {},
    fetch: async () => { throw new Error('Unexpected request'); }
  };
  context.dcAllPlayers = ['P1', 'P2', 'P3'].map((name, i) => ({ id: String(i), name, pos: 'PG' }));
  context.dcPool = [...context.dcAllPlayers];
  vm.createContext(context);
  for (const name of ['dcCompletionDetails', 'dcAdvanceAfterPick', 'dcAdvancePastRescinded',
    'dcDraftPlayer', 'dcUndoPick', 'dcClearSelection', 'dcRenderOrder', 'dcRescindPick',
    'dcSkipPick', 'dcLoadFromServer']) {
    const match = html.match(new RegExp(`(?:async )?function ${name}\\([^]*?^\\}`, 'm'));
    assert.ok(match, `Actual function ${name} exists`);
    vm.runInContext(match[0], context);
  }
  return context;
}

test('borrowed selection returns to original pick, then advances past completed borrowed slot', () => {
  const c = setup();
  c.dcDraftPlayer('0');
  assert.equal(c.dcCurrentPick, 0);
  assert.equal(c.dcSkippedPick, null);
  assert.equal(c.dcResults[0].pick, 2);
  c.dcDraftPlayer('1');
  assert.equal(c.dcCurrentPick, 2);
  assert.equal(c.dcResults[1].pick, 1);
  assert.equal(c.dcPool.length, 1);
});

test('undo restores both the selected player and prior skipped clock state', () => {
  const c = setup();
  c.dcDraftPlayer('0');
  c.dcDraftPlayer('1');
  c.dcUndoPick();
  assert.equal(c.dcCurrentPick, 0);
  assert.equal(c.dcResults.length, 1);
  c.dcUndoPick();
  assert.equal(c.dcCurrentPick, 1);
  assert.equal(c.dcSkippedPick, 0);
  assert.equal(c.dcResults.length, 0);
  assert.equal(c.dcPool.length, 3);
});

test('undo before borrowed selection cancels skip; non-admin undo is blocked', () => {
  const c = setup();
  c.dcModUnlocked = false;
  c.dcUndoPick();
  assert.equal(c.dcCurrentPick, 1);
  c.dcModUnlocked = true;
  c.dcUndoPick();
  assert.equal(c.dcCurrentPick, 0);
  assert.equal(c.dcSkippedPick, null);
});

test('rescinding borrowed pick also returns immediately to deferred pick', () => {
  const c = setup();
  c.dcRescindPick();
  assert.equal(c.dcCurrentPick, 0);
  assert.equal(c.dcSkippedPick, null);
  assert.equal(c.dcResults[0].pick, 2);
  c.dcDraftPlayer('0');
  assert.equal(c.dcCurrentPick, 2);
});

test('render retains skipped row and disables another skip until return', () => {
  const c = setup();
  c.dcRenderOrder();
  const list = c.document.getElementById('dcOrderList');
  const tbody = list.children[0].children[0];
  assert.ok(tbody.children.some(row => row.innerHTML.includes('Skipped — returns next')));
  assert.equal(c.document.getElementById('dcSkipBtn').disabled, true);
  assert.match(c.document.getElementById('dcClockPickNum').textContent, /Pick 2.*Pick 1 returns next/);
});

test('skip is inaccessible to non-admins and stale pending state cannot trigger it twice', async () => {
  const c = setup();
  c.dcModUnlocked = false;
  c.dcSkippedPick = null;
  await c.dcSkipPick();
  c.dcModUnlocked = true;
  c.dcSkippedPick = 0;
  await c.dcSkipPick();
});

test('admin skip sends authenticated request and reloads shared pending state', async () => {
  const c = setup();
  c.dcSkippedPick = null;
  c.dcCurrentPick = 0;
  let writes = 0;
  c.dcSave = () => { throw new Error('Skip must not overwrite draft before checking server clock'); };
  const state = { order: c.dcOrder, currentPick: 1, skippedPick: 0, pool: c.dcPool,
    results: [], playerNotes: {}, ts: 11 };
  c.fetch = async (url, options) => {
    if (url === '/skip_draft_pick') {
      writes++;
      assert.equal(options.headers['X-Draft-Admin'], 'test-admin');
      assert.equal(JSON.parse(options.body).currentPick, 0);
    } else assert.equal(url, '/load_draft');
    return { ok: true, json: async () => state };
  };
  await c.dcSkipPick();
  assert.equal(writes, 1);
  assert.equal(c.dcCurrentPick, 1);
  assert.equal(c.dcSkippedPick, 0);
  assert.equal(c.dcSkipBusy, false);
  assert.equal(c.dcLastSyncStamp, 11);
});
