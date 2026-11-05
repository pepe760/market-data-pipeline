const test = require("node:test");
const assert = require("node:assert/strict");
const math = require("../terminal_site/app.js");
const asset = (symbol, pairs) => ({
  symbol,
  series: pairs.map(([date, close]) => ({ date, close })),
});
test("RS uses exact shared endpoints, never newer benchmark bars", () => {
  const a = asset("A", [
    ["2026-01-02", 100],
    ["2026-01-05", 110],
    ["2026-01-06", 121],
  ]);
  const b = asset("B", [
    ["2026-01-02", 100],
    ["2026-01-05", 105],
    ["2026-01-06", 110],
    ["2026-01-07", 180],
  ]);
  math.setAssets([a, b]);
  const result = math.paired(a, b, 2);
  assert.deepEqual(result.dates, ["2026-01-02", "2026-01-06"]);
  assert.ok(Math.abs(result.value - 0.11) < 1e-10);
});
test("missing intermediate sessions use shared observations and keep decimal units", () => {
  const a = asset("A", [
    ["2026-01-02", 100],
    ["2026-01-03", 110],
    ["2026-01-04", 120],
    ["2026-01-05", 130],
  ]);
  const b = asset("B", [
    ["2026-01-02", 100],
    ["2026-01-04", 100],
    ["2026-01-05", 110],
  ]);
  math.setAssets([a, b]);
  assert.ok(Math.abs(math.paired(a, b, 2).value - 0.2) < 1e-10);
  assert.equal(math.paired(a, b, 3).value, null);
});
test("comparison base100 starts on the first common day; no forward filling", () => {
  const a = asset("A", [
    ["2026-01-01", 50],
    ["2026-01-02", 100],
    ["2026-01-03", 120],
    ["2026-01-04", 150],
  ]);
  const b = asset("B", [
    ["2026-01-02", 200],
    ["2026-01-04", 210],
  ]);
  math.setAssets([a, b]);
  const c = math.commonChart([a, b], 90);
  assert.deepEqual(c.days, ["2026-01-02", "2026-01-04"]);
  assert.deepEqual(c.lines[0].values, [100, 150]);
  assert.deepEqual(c.lines[1].values, [100, 105]);
});
test("nonoverlapping series and insufficient history do not fabricate a return", () => {
  const a = asset("A", [["2026-01-01", 100]]),
    b = asset("B", [["2026-01-02", 100]]);
  math.setAssets([a, b]);
  assert.equal(math.paired(a, b).value, null);
  assert.deepEqual(math.commonChart([a, b]).days, []);
});
test("15m comparison normalizes exact UTC endpoints and keeps daily history separate", () => {
  const a = {
    symbol: "A",
    series: [{ date: "2026-01-02", close: 500 }],
    intraday: {
      bars: [
        { end: "2026-01-02T14:15:00Z", close: 100 },
        { end: "2026-01-02T14:30:00Z", close: 120 },
        { end: "2026-01-02T14:45:00Z", close: 130 },
      ],
    },
  };
  const b = {
    symbol: "B",
    intraday: {
      bars: [
        { end: "2026-01-02T22:15:00+08:00", close: 200 },
        { end: "2026-01-02T22:45:00+08:00", close: 210 },
      ],
    },
  };
  math.setAssets([a, b]);
  const chart = math.commonChart([a, b], 90, "15m");
  assert.deepEqual(chart.days, [
    "2026-01-02T14:15:00.000Z",
    "2026-01-02T14:45:00.000Z",
  ]);
  assert.deepEqual(chart.lines[0].values, [100, 130]);
  assert.deepEqual(chart.lines[1].values, [100, 105]);
  assert.deepEqual(math.commonChart([a, b], 90, "daily").days, []);
});
test("invalid intraday observations do not create synthetic points", () => {
  const a = {
    symbol: "A",
    intraday: {
      bars: [
        { end: "bad", close: 100 },
        { end: "2026-01-02T14:15:00Z", close: 0 },
      ],
    },
  };
  math.setAssets([a]);
  assert.deepEqual(math.commonChart([a], 90, "15m").days, []);
});
