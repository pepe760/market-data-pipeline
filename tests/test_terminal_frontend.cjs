const test = require('node:test');
const assert = require('node:assert/strict');
const math = require('../terminal_site/app.js');
const asset = (symbol, pairs) => ({symbol, series:pairs.map(([date,close])=>({date,close}))});
test('RS uses exact shared endpoints, never newer benchmark bars', () => {
  const a=asset('A',[['2026-01-02',100],['2026-01-05',110],['2026-01-06',121]]);
  const b=asset('B',[['2026-01-02',100],['2026-01-05',105],['2026-01-06',110],['2026-01-07',180]]);
  math.setAssets([a,b]);
  const result=math.paired(a,b,2);
  assert.deepEqual(result.dates,['2026-01-02','2026-01-06']);
  assert.ok(Math.abs(result.value-.11)<1e-10);
});
test('missing intermediate sessions use shared observations and keep decimal units', () => {
  const a=asset('A',[['2026-01-02',100],['2026-01-03',110],['2026-01-04',120],['2026-01-05',130]]);
  const b=asset('B',[['2026-01-02',100],['2026-01-04',100],['2026-01-05',110]]);
  math.setAssets([a,b]);
  assert.ok(Math.abs(math.paired(a,b,2).value-.20)<1e-10);
  assert.equal(math.paired(a,b,3).value,null);
});
test('comparison base100 starts on the first common day; no forward filling', () => {
  const a=asset('A',[['2026-01-01',50],['2026-01-02',100],['2026-01-03',120],['2026-01-04',150]]);
  const b=asset('B',[['2026-01-02',200],['2026-01-04',210]]);
  math.setAssets([a,b]);
  const c=math.commonChart([a,b],90);
  assert.deepEqual(c.days,['2026-01-02','2026-01-04']);
  assert.deepEqual(c.lines[0].values,[100,150]);
  assert.deepEqual(c.lines[1].values,[100,105]);
});
test('nonoverlapping series and insufficient history do not fabricate a return', () => {
  const a=asset('A',[['2026-01-01',100]]), b=asset('B',[['2026-01-02',100]]);
  math.setAssets([a,b]);
  assert.equal(math.paired(a,b).value,null);
  assert.deepEqual(math.commonChart([a,b]).days,[]);
});
