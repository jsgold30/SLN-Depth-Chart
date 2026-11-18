const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../templates/index.html'), 'utf8');

function setup() {
  const elements = {};
  const context = {
    TM_LEAGUE_YEAR: 2043, TM_PICK_YEARS: [2043, 2044, 2045, 2046, 2047, 2048],
    tmOwedPicks: [], tmOwedYears: [2045, 2046, 2047, 2048],
    tmRosterPicksA: null, tmRosterPicksB: null, tmRosterPickYearsA: [], tmRosterPickYearsB: [],
    tmPicksA: [], tmPicksB: [], tmRenderPickPills() {},
    tmGetSideAbbr(side) { return side === 'A' ? 'SAS' : 'ORL'; },
    document: { getElementById(id) { return elements[id] ||= { innerHTML: '', textContent: '', value: 2043 }; } }
  };
  vm.createContext(context);
  vm.runInContext(html.match(/const TM_TEAMS = \[[\s\S]*?\n\];/)[0], context);
  for (const name of ['_teamNameToAbbr', '_picksFromOwed', 'tmAvailablePicks', 'tmPickLabel',
    'tmApplyLeagueYear', 'tmUpdatePickSourceInfo', 'tmPopulatePickSelectors', 'tmReconcilePicks', 'tmPickTradeErrors']) {
    vm.runInContext(html.match(new RegExp(`function ${name}\\([^]*?^\\}`, 'm'))[0], context);
  }
  return context;
}

function roster(c, side, picks, years = [2043, 2044, 2045]) {
  c[`tmRosterPicks${side}`] = picks;
  c[`tmRosterPickYears${side}`] = years;
}

function pick(year, round, original_team) { return { year, round, original_team }; }

test('2043–2048 includes current year and never offers 2049', () => {
  const c = setup();
  roster(c, 'A', [pick(2043, 2, 'Spurs'), pick(2044, 2, 'Spurs'), pick(2049, 1, 'Nets')]);
  const picks = c.tmAvailablePicks('SAS', 'A');
  assert.ok(picks.some(p => p.year === 2043));
  assert.ok(picks.some(p => p.year === 2048));
  assert.equal(picks.some(p => p.year === 2049), false);
  assert.equal(picks.length, 10);
});

test('first two years use roster ownership and ignore conflicting owed entries', () => {
  const c = setup();
  c.tmOwedPicks = [
    { year: 2043, round: 2, from_abbr: 'SAS', to_abbr: 'ORL' },
    { year: 2044, round: 1, from_abbr: 'BOS', to_abbr: 'SAS' }
  ];
  roster(c, 'A', [pick(2043, 2, 'Spurs'), pick(2044, 1, 'Nets')]);
  const picks = c.tmAvailablePicks('SAS', 'A');
  assert.ok(picks.some(p => p.year === 2043 && p.round === 2 && p.originalAbbr === 'SAS'));
  assert.ok(picks.some(p => p.year === 2044 && p.originalAbbr === 'NJN'));
  assert.equal(picks.some(p => p.year === 2043 && p.round === 1), false);
  assert.equal(picks.some(p => p.year === 2044 && p.originalAbbr === 'BOS'), false);
});

test('outer four years use owed sections only, retain swaps, and ignore roster columns', () => {
  const c = setup();
  c.tmOwedPicks = [
    { year: 2045, round: 1, from_abbr: 'NJN', to_abbr: 'SAS', swap_partner: 'BOS', qualifier: 'better' },
    { year: 2045, round: 2, from_abbr: 'SAS', to_abbr: 'ORL' }
  ];
  roster(c, 'A', [pick(2045, 2, 'Spurs'), pick(2045, 1, 'Magic')]);
  const picks = c.tmAvailablePicks('SAS', 'A');
  const received = picks.find(p => p.year === 2045 && p.originalAbbr === 'NJN');
  assert.equal(c.tmPickLabel(received), '2045 NJN/BOS 1st Round Pick (Better)');
  assert.equal(picks.some(p => p.year === 2045 && p.round === 2 && p.originalAbbr === 'SAS'), false);
  assert.equal(picks.some(p => p.originalAbbr === 'ORL'), false);
});

test('empty roster columns mean zero picks; missing source data never fabricates ownership', () => {
  const c = setup();
  c.tmOwedYears = [];
  assert.equal(c.tmAvailablePicks('SAS', 'A').length, 0);
  roster(c, 'A', [], [2043, 2044]);
  assert.equal(c.tmAvailablePicks('SAS', 'A').length, 0);
  c.tmOwedYears = [2045];
  const picks = c.tmAvailablePicks('SAS', 'A');
  assert.equal(picks.length, 2);
  assert.ok(picks.every(p => p.year === 2045));
});

test('both sides use their own roster and duplicate entries do not duplicate picks', () => {
  const c = setup();
  roster(c, 'A', [pick(2043, 2, 'Spurs'), pick(2043, 2, 'Spurs')]);
  roster(c, 'B', [pick(2043, 1, 'Nets')]);
  assert.equal(c.tmAvailablePicks('SAS', 'A').filter(p => p.year === 2043).length, 1);
  const b = c.tmAvailablePicks('ORL', 'B').filter(p => p.year === 2043);
  assert.equal(b.length, 1);
  assert.equal(b[0].originalAbbr, 'NJN');
});

test('league-year change moves range, source roles, instructions, and selected pills', () => {
  const c = setup();
  roster(c, 'A', [pick(2043, 2, 'Spurs'), pick(2044, 2, 'Spurs'), pick(2045, 1, 'Nets')]);
  c.tmPicksA = [{ year: 2043 }, { year: 2044, round: 2, originalAbbr: 'SAS' }];
  c.tmApplyLeagueYear(2044);
  assert.deepEqual(Array.from(c.TM_PICK_YEARS), [2044, 2045, 2046, 2047, 2048, 2049]);
  assert.equal(c.tmPicksA.length, 1);
  const picks = c.tmAvailablePicks('SAS', 'A');
  assert.ok(picks.some(p => p.year === 2045 && p.originalAbbr === 'NJN'));
  assert.equal(picks.some(p => p.year === 2045 && p.originalAbbr === 'SAS'), false);
  assert.match(c.document.getElementById('tmPickSources').textContent, /2044–2049/);
  assert.match(c.document.getElementById('tmOwedPicksHelp').textContent, /2046–2049/);
});

test('validation blocks old, too-far, and unowned picks and allows current and final year', () => {
  const c = setup();
  roster(c, 'A', [pick(2043, 2, 'Spurs')]);
  const candidates = [2042, 2043, 2048, 2049].map(year => ({ year, round: 2, originalAbbr: 'SAS', label: String(year) }));
  assert.equal(c.tmPickTradeErrors(candidates, 'SAS', 'A').length, 2);
  assert.equal(c.tmPickTradeErrors([{ year: 2043, round: 1, originalAbbr: 'SAS', label: 'Unowned' }], 'SAS', 'A').length, 1);
  c.tmPicksA = candidates;
  c.tmReconcilePicks();
  assert.deepEqual(Array.from(c.tmPicksA, p => p.year), [2043, 2048]);
});
