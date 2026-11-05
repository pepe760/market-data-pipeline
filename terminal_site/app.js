"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const svgNS = "http://www.w3.org/2000/svg";
  const finite = (x) => typeof x === "number" && Number.isFinite(x);
  const pct = (x) =>
    finite(x) ? `${x > 0 ? "+" : ""}${(x * 100).toFixed(2)}%` : "—";
  const num = (x) =>
    finite(x)
      ? x.toLocaleString("en-US", { maximumFractionDigits: x < 1 ? 5 : 2 })
      : "—";
  const tone = (x) =>
    !finite(x) ? "neutral" : x >= 0 ? "positive" : "negative";
  const hkTime = (x) => {
    const d = new Date(x);
    return isNaN(d)
      ? "未取得"
      : new Intl.DateTimeFormat("zh-HK", {
          timeZone: "Asia/Hong_Kong",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        }).format(d) + " HKT";
  };
  const el = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  const sEl = (tag, attrs = {}) => {
    const n = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };
  const clear = (n) => n.replaceChildren();
  const button = (text, fn, cls = "button") => {
    const n = el("button", text, cls);
    n.type = "button";
    n.addEventListener("click", fn);
    return n;
  };
  const link = (text, url) => {
    const n = el("a", text);
    try {
      const u = new URL(url);
      if (u.protocol === "https:") {
        n.href = u.href;
        n.target = "_blank";
        n.rel = "noopener";
      }
    } catch {}
    return n;
  };
  const loadLocal = (key, fallback) => {
    try {
      return JSON.parse(localStorage.getItem(key)) ?? fallback;
    } catch {
      return fallback;
    }
  };
  const saveLocal = (key, value) => {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {}
  };
  const colors = [
    "#167b66",
    "#3f76d1",
    "#e28c32",
    "#a05bba",
    "#db5568",
    "#4aa0ae",
    "#8b8c3d",
    "#6875ae",
    "#97664a",
    "#329c94",
  ];
  const views = {
    overview: ["市場總覽", "MARKET OVERVIEW", "跨市場一齊睇，先知強弱喺邊。"],
    us: ["美股強弱", "US EQUITIES", "按已成功收集嘅標的排名，唔代表全美股。"],
    hk: ["港股強弱", "HONG KONG", "港元價格獨立記錄，留意各自交易日。"],
    crypto: ["加密貨幣", "CRYPTO MARKETS", "全天候市場，日線按來源 UTC 分界。"],
    fx: ["外匯", "FOREIGN EXCHANGE", "留意報價方向；幣對升幅唔等於美元升幅。"],
    commodities: [
      "大宗商品",
      "COMMODITIES",
      "期貨及 ETF 代理分開標示，未計轉倉成本。",
    ],
    etf: [
      "全球／板塊 ETF",
      "SECTORS / GLOBAL",
      "同一張表睇國家、板塊同主題走勢。",
    ],
    macro: [
      "宏觀與流動性",
      "MACRO / LIQUIDITY",
      "官方觀測日期、單位及修訂限制一齊睇。",
    ],
    compare: [
      "走勢比較",
      "COMPARE / PRICE PATH",
      "相同日期重設 100，方便比較價格路徑。",
    ],
    watchlist: ["我的觀察", "YOUR WATCHLIST", "觀察名單只存喺呢個瀏覽器。"],
    coverage: [
      "收集與方法",
      "DATA OPERATIONS",
      "收集範圍、來源缺口同計算規則公開可查。",
    ],
  };
  let data = null,
    context = null,
    rows = [],
    active = "overview",
    page = 0,
    loading = false;
  let watches = new Set(
    loadLocal("wml-watch-v1", []).filter((x) => typeof x === "string"),
  );
  let selected = loadLocal("wml-compare-v1", ["SPY", "QQQ", "SOXX"])
    .filter((x) => typeof x === "string")
    .slice(0, 10);
  const priceCache = new Map(),
    relativeCache = new Map();
  function market(a) {
    if (a.sector === "商品") return "commodities";
    const key = String(a.market || "").toLowerCase();
    return (
      {
        stocks: "us",
        stock: "us",
        equities: "us",
        hong_kong: "hk",
        commodity: "commodities",
        forex: "fx",
        etfs: "etf",
        sectors: "etf",
      }[key] || key
    );
  }
  function findAsset(symbol) {
    const aliases = {
      "BTC-USD": ["BTCUSDT", "BTC"],
      "ETH-USD": ["ETHUSDT", "ETH"],
      BTC: ["BTC-USD", "BTCUSDT"],
    };
    return (
      rows.find((a) => a.symbol === symbol) ||
      rows.find((a) => (aliases[symbol] || []).includes(a.symbol))
    );
  }
  function series(a) {
    if (!a) return [];
    if (!priceCache.has(a.symbol)) {
      const points = new Map();
      for (const p of a.series || [])
        if (typeof p.date === "string" && finite(p.close) && p.close > 0)
          points.set(p.date, { ...p });
      priceCache.set(
        a.symbol,
        [...points.values()].sort((x, y) => x.date.localeCompare(y.date)),
      );
    }
    return priceCache.get(a.symbol);
  }
  function benchmark(a, mode = "auto") {
    if (mode === "absolute") return null;
    if (mode === "spy") return findAsset("SPY");
    if (mode === "btc") return findAsset("BTC-USD");
    const m = market(a);
    return m === "crypto"
      ? findAsset("BTC-USD")
      : m === "hk"
        ? findAsset("2800.HK") || findAsset("^HSI")
        : ["us", "etf"].includes(m)
          ? findAsset("SPY")
          : null;
  }
  function paired(a, b, n = 20) {
    if (!b)
      return {
        value: a.returns?.["d" + n] ?? null,
        dates: null,
        base: "絕對升跌",
      };
    const key = a.symbol + ":" + b.symbol + ":" + n;
    if (relativeCache.has(key)) return relativeCache.get(key);
    const bm = new Map(series(b).map((p) => [p.date, p.close]));
    const points = series(a).filter((p) => bm.has(p.date));
    let result = { value: null, dates: null, base: b.symbol };
    if (points.length > n) {
      const end = points.at(-1),
        start = points.at(-1 - n);
      result = {
        value:
          end.close / start.close -
          1 -
          (bm.get(end.date) / bm.get(start.date) - 1),
        dates: [start.date, end.date],
        base: b.symbol,
      };
    }
    relativeCache.set(key, result);
    return result;
  }
  function score(a) {
    return paired(a, benchmark(a, $("benchmark").value)).value;
  }
  function cell(text, cls) {
    const n = el("td", text, cls);
    return n;
  }
  function table(headers, bodyRows, target) {
    const sc = el("div", null, "scroller"),
      t = el("table"),
      h = el("thead"),
      tr = el("tr");
    headers.forEach((x) => tr.append(el("th", x)));
    h.append(tr);
    const body = el("tbody");
    bodyRows.forEach((r) => body.append(r));
    t.append(h, body);
    sc.append(t);
    target.replaceChildren(sc);
    return t;
  }
  function spark(points, width = 84, height = 26, color) {
    const svg = sEl("svg", {
      viewBox: `0 0 ${width} ${height}`,
      class: "sparkline",
      "aria-label": "價格走勢",
    });
    const vals = points.filter(finite);
    if (vals.length < 2) return svg;
    const min = Math.min(...vals),
      range = Math.max(...vals) - min || 1;
    const path = vals
      .map(
        (v, i) =>
          (i ? "L" : "M") +
          ((i * (width - 4)) / (vals.length - 1) + 2).toFixed(2) +
          "," +
          (height - 2 - ((v - min) * (height - 4)) / range).toFixed(2),
      )
      .join(" ");
    svg.append(
      sEl("path", {
        d: path,
        stroke:
          color ||
          (vals.at(-1) >= vals[0] ? "var(--positive)" : "var(--negative)"),
      }),
    );
    return svg;
  }
  const trendPrice = a => finite(a.analysis_price) ? a.analysis_price : a.price;
  function ageText(a) {
    return a.price_asof?.slice(0, 10) || series(a).at(-1)?.date || "無報價";
  }
  function stateText(a) {
    const q = typeof a.quality === "object" ? a.quality.status : a.quality;
    const labels = {
      OBSERVED: "已觀測", SOURCE_ERROR_PREVIOUS_RETAINED: "來源失敗，保留原值",
      SOURCE_ERROR: "來源失敗", AWAITING_FIRST_OBSERVATION: "待首次收集"
    };
    return (labels[q] || (q && !["OK", "FRESH"].includes(q) ? q : "")) +
      (a.fresh === false && a.price != null ? " · 較舊" : "");
  }
  function toggleWatch(a) {
    if (watches.has(a.symbol)) watches.delete(a.symbol);
    else watches.add(a.symbol);
    saveLocal("wml-watch-v1", [...watches]);
    render();
  }
  function assetCell(a) {
    const td = cell();
    const pin = button(
      watches.has(a.symbol) ? "★" : "☆",
      () => toggleWatch(a),
      "pin",
    );
    pin.setAttribute("aria-label", "觀察 " + a.symbol);
    pin.setAttribute("aria-pressed", watches.has(a.symbol));
    td.append(
      pin,
      button(a.symbol, () => detail(a), "symbol"),
      el("small", a.name || a.symbol),
      el("small", a.sector || a.currency || ""),
      ...(a.identity_status === "UNVERIFIED_SYMBOL_MATCH" ? [el("small", "名稱映射待核實")] : []),
    );
    return td;
  }
  function trendCell(a) {
    const td = cell(),
      div = el("div", null, "lights");
    for (const k of ["d20", "d50", "d200"]) {
      const v = a.sma?.[k],
        up = finite(v) && finite(trendPrice(a));
      const light = el(
        "span",
        null,
        "light" + (up ? (trendPrice(a) >= v ? " up" : " down") : ""),
      );
      light.title = `${k.slice(1)} 日均線 ${num(v)}`;
      div.append(light);
    }
    td.append(div);
    return td;
  }
  function assetRow(a, rank, full = true) {
    const tr = el("tr");
    tr.append(cell(rank), assetCell(a));
    if (full) {
      const td = cell(num(a.price));
      td.append(el("small", a.currency || ""));
      tr.append(td);
    }
    const value = paired(a, benchmark(a, full ? $("benchmark").value : "auto"));
    const rs = cell(pct(value.value), tone(value.value));
    rs.title =
      value.base + (value.dates ? " · " + value.dates.join(" → ") : "");
    rs.append(
      el("small", value.base + (value.dates ? " · " + value.dates[1] : "")),
    );
    tr.append(rs);
    for (const k of full
      ? ["d1", "d5", "d20", "d60", "d120"]
      : ["d1", "d5", "d20", "d60"])
      tr.append(cell(pct(a.returns?.[k]), tone(a.returns?.[k])));
    if (full) tr.append(trendCell(a));
    const sp = cell();
    sp.append(
      spark(
        series(a)
          .slice(-21)
          .map((p) => p.close),
      ),
    );
    tr.append(sp);
    const dt = cell(a.price_asof ? hkTime(a.price_asof) : "未取得");
    dt.append(el("small", "分析 " + (a.analytics_asof || "未取得") + (a.analytics_partial ? " · 未收市" : "")), el("small", stateText(a)));
    tr.append(dt);
    const cp = cell();
    cp.append(button("＋", () => addCompare(a.symbol), "pin"));
    tr.append(cp);
    return tr;
  }
  function headers(full = true) {
    return full
      ? [
          "排名",
          "標的",
          "價格",
          "20 期超額¹",
          "1 日",
          "5 日",
          "20 日",
          "60 日",
          "120 日",
          "20/50/200 MA",
          "21 日走勢",
          "報價時間／分析日",
          "比較",
        ]
      : [
          "序號",
          "標的",
          "20 期超額¹",
          "1 日",
          "5 日",
          "20 日",
          "60 日",
          "21 日走勢",
          "報價日",
          "比較",
        ];
  }
  function renderQuotes() {
    clear($("quote-strip"));
    for (const s of ["SPY", "QQQ", "BTC-USD", "GLD", "TLT", "UUP"]) {
      const a = findAsset(s),
        q = el("div", null, "quote");
      q.append(
        el("small", a?.name?.slice(0, 22) || s),
        el("strong", a ? num(a.price) : "—"),
      );
      const r = el(
        "span",
        a ? pct(a.returns?.d1) : "待首次收集",
        tone(a?.returns?.d1),
      );
      q.append(r, el("small", a ? `${a.symbol} · ${ageText(a)}` : s));
      if (a) q.addEventListener("click", () => detail(a));
      $("quote-strip").append(q);
    }
  }
  function renderOverview() {
    const usable = rows.filter((a) => finite(a.price));
    const us = usable.filter((a) => market(a) === "us");
    clear($("market-summary"));
    for (const [title, text] of [
      ["目前覆蓋", `${usable.length} 個有價格標的；目標及失敗數見收集頁。`],
      [
        "美股走勢",
        (() => {
          const valid = us.filter((a) => finite(a.sma?.d50));
          const up = valid.filter((a) => trendPrice(a) > a.sma.d50).length;
          return valid.length
            ? `${up}／${valid.length} 隻高於 50 日線。只代表本名單。`
            : "等待足夠日線。";
        })(),
      ],
      ["資料時鐘", "每次公開快照保留原報價日。收集時間唔等於即時報價。"],
    ]) {
      const r = el("div", null, "summary-row");
      r.append(el("strong", title), el("p", text));
      $("market-summary").append(r);
    }
    clear($("breadth"));
    for (const [name, m] of [
      ["美股", "us"],
      ["港股", "hk"],
      ["Crypto", "crypto"],
      ["ETF", "etf"],
    ]) {
      const group = usable.filter((a) => market(a) === m && finite(a.sma?.d50));
      const count = group.filter((a) => trendPrice(a) >= a.sma.d50).length;
      const title = el("div", null, "bar-label");
      title.append(
        el("span", name + " 高於 50 日線"),
        el(
          "span",
          group.length
            ? `${count} / ${group.length} · ${Math.round((count / group.length) * 100)}%`
            : "待收集",
        ),
      );
      const meter = el("div", null, "meter"),
        fill = el("span");
      fill.style.width = group.length
        ? `${(count / group.length) * 100}%`
        : "0%";
      meter.append(fill);
      $("breadth").append(title, meter);
    }
    const keys = [
      "SPY",
      "QQQ",
      "IWM",
      "GLD",
      "SLV",
      "USO",
      "DBA",
      "COPX",
      "TLT",
      "HYG",
      "UUP",
      "FXI",
      "VGK",
      "INDA",
      "EWJ",
      "EWY",
      "EEM",
      "2800.HK",
      "3033.HK",
      "BTC-USD",
      "ETH-USD",
    ];
    const cross = keys.map(findAsset).filter(Boolean);
    table(
      headers(false),
      cross.map((a, i) => assetRow(a, i + 1, false)),
      $("cross-table"),
    );
    const leaders = us
      .filter((a) => finite(paired(a, findAsset("SPY"), 5).value))
      .sort(
        (a, b) =>
          paired(b, findAsset("SPY"), 5).value -
          paired(a, findAsset("SPY"), 5).value,
      );
    for (const [id, group] of [
      ["leaders", leaders.slice(0, 5)],
      ["laggards", leaders.slice(-5).reverse()],
    ]) {
      clear($(id));
      for (const a of group) {
        const r = el("div", null, "leader-row"),
          txt = el("div");
        txt.append(
          button(a.symbol, () => detail(a), "symbol"),
          el("small", a.name),
        );
        r.append(
          txt,
          el(
            "span",
            pct(paired(a, findAsset("SPY"), 5).value),
            "return " + tone(paired(a, findAsset("SPY"), 5).value),
          ),
        );
        $(id).append(r);
      }
      if (!group.length)
        $(id).append(el("p", "等待共同日期嘅 SPY 比較資料。", "empty"));
    }
  }
  function renderMarket(resetSectors = false) {
    let group =
      active === "watchlist"
        ? rows.filter((a) => watches.has(a.symbol))
        : rows.filter((a) => market(a) === active);
    if (resetSectors) {
      const prior = $("sector-filter").value;
      clear($("sector-filter"));
      $("sector-filter").append(new Option("全部", ""));
      [...new Set(group.map((a) => a.sector).filter(Boolean))]
        .sort()
        .forEach((x) => $("sector-filter").append(new Option(x, x)));
      $("sector-filter").value = [...$("sector-filter").options].some(
        (x) => x.value === prior,
      )
        ? prior
        : "";
    }
    const query = $("market-search").value.trim().toLowerCase(),
      sector = $("sector-filter").value,
      trend = $("trend-filter").value;
    let filtered = group.filter(
      (a) =>
        (!query ||
          [a.symbol, a.name, a.sector]
            .join(" ")
            .toLowerCase()
            .includes(query)) &&
        (!sector || a.sector === sector),
    );
    filtered = filtered.filter(
      (a) =>
        trend === "all" ||
        (trend === "above20" && finite(a.sma?.d20) && trendPrice(a) >= a.sma.d20) ||
        (["green", "red"].includes(trend) &&
          ["d20", "d50", "d200"].every(
            (k) =>
              finite(a.sma?.[k]) &&
              (trend === "green" ? trendPrice(a) >= a.sma[k] : trendPrice(a) < a.sma[k]),
          )),
    );
    const sort = $("sort").value;
    filtered.sort((a, b) =>
      sort === "symbol"
        ? a.symbol.localeCompare(b.symbol)
        : ((sort === "rs" ? score(b) : b.returns?.[sort]) ?? -Infinity) -
            ((sort === "rs" ? score(a) : a.returns?.[sort]) ?? -Infinity) ||
          a.symbol.localeCompare(b.symbol),
    );
    const max = Math.max(0, Math.ceil(filtered.length / 40) - 1);
    page = Math.min(page, max);
    table(
      headers(),
      filtered
        .slice(page * 40, page * 40 + 40)
        .map((a, i) => assetRow(a, page * 40 + i + 1)),
      $("market-table"),
    );
    $("market-page").textContent =
      `${filtered.length} 個標的 · ${page + 1} / ${max + 1} 頁`;
    $("prev-page").disabled = page === 0;
    $("next-page").disabled = page === max;
    $("market-coverage").textContent =
      `已存 ${group.filter(a => finite(a.price)).length}／${group.length} 個名單標的；篩選後 ${filtered.length} 個。¹ 超額＝同窗價格升幅減基準升幅，使用共同日期；FX／商品預設用絕對升幅。留意各行報價日，舊資料唔代表今日排名。`;
    $("heatmap").hidden = active !== "etf";
    if (active === "etf") {
      clear($("heatmap"));
      for (const a of group
        .filter((x) => finite(x.returns?.d20))
        .slice(0, 24)) {
        const b = button(
          "",
          () => detail(a),
          "heat-tile " + tone(a.returns.d20),
        );
        b.append(
          el("strong", a.symbol),
          el("small", (a.name || "").slice(0, 25)),
          el("b", pct(a.returns.d20)),
          el("small", "20 日 · " + ageText(a)),
        );
        $("heatmap").append(b);
      }
    }
  }
  function contextCard(label, value, unit, date, status, url, points) {
    const c = el("div", null, "context-card");
    c.append(
      el("span", label),
      el("strong", num(value)),
      el("small", unit || ""),
      el("p", `觀測 ${date || "未取得"} · ${status || "待接駁"}`),
    );
    if (points?.length) c.append(spark(points, 160, 30));
    if (url) c.append(link("官方來源 ↗", url));
    return c;
  }
  function renderMacro() {
    clear($("macro-cards"));
    clear($("context-sources"));
    if (!context) {
      $("macro-cards").append(el("p", "宏觀來源等待首次收集。", "empty"));
      return;
    }
    const units = {
      million_usd: "百萬美元",
      billion_usd: "十億美元",
      percent: "%",
      percent_sa: "%（季調）",
      percentage_points: "百分點",
      basis_points: "基點（bp）",
      persons_sa: "人（季調）",
      thousand_persons_sa: "千人（季調）",
      index_1982_84_100_sa: "指數（1982–84＝100，季調）",
    };
    for (const a of context.macro?.derived || [])
      $("macro-cards").append(
        contextCard(
          a.label,
          a.value,
          units[a.units] || a.units,
          a.date,
          a.status,
          null,
        ),
      );
    for (const a of context.macro?.series || []) {
      if (a.status === "LINK_ONLY_SOURCE_RESTRICTION") {
        const r = el("div", null, "context-row");
        const t = el("div");
        t.append(el("strong", a.label), el("p", a.reason));
        r.append(t, link("官方頁面 ↗", a.source_url));
        $("context-sources").append(r);
      } else
        $("macro-cards").append(
          contextCard(
            a.label,
            a.latest?.value,
            units[a.units] || a.units,
            a.latest?.date,
            a.status,
            a.source_url,
            a.observations?.map((p) => p.value),
          ),
        );
    }
    const cr = context.crypto_derivatives;
    if (cr) {
      $("macro-cards").append(
        contextCard(
          "BTC 永續合約上次 funding",
          cr.funding?.rate_percent,
          "%，按來源結算週期",
          cr.funding?.observed_at_utc,
          cr.status,
          cr.source_url,
        ),
      );
      $("macro-cards").append(
        contextCard(
          "BTC 永續未平倉量",
          cr.open_interest?.quantity,
          cr.open_interest?.units,
          cr.open_interest?.observed_at_utc,
          cr.status,
          cr.source_url,
        ),
      );
    }
    for (const note of context.limitations || [])
      $("context-sources").append(el("p", note, "caption"));
    const sec = context.sec_13f;
    if (sec) {
      const row = el("div", null, "context-row");
      const left = el("div");
      left.append(
        el("strong", "機構投資人申報"),
        el("p", `狀態：${sec.status}。完整持倉及季度變動未啟用。`),
      );
      const right = el("div");
      for (const a of sec.watchlist || [])
        right.append(link(a.label + " ↗ ", a.source_url), el("br"));
      row.append(left, right);
      $("context-sources").append(row);
      for (const f of sec.filings || [])
        $("context-sources").append(
          link(
            `${f.manager} · ${f.form} · 申報 ${f.filed_date} · 期末 ${f.report_period}`,
            f.source_url,
          ),
        );
    }
  }
  function stat(parent, label, value) {
    const n = el("div", null, "stat");
    n.append(el("span", label), el("strong", value));
    parent.append(n);
  }
  function renderCoverage() {
    clear($("coverage-stats"));
    const groups = data?.coverage?.groups || [];
    stat(
      $("coverage-stats"),
      "有價格標的",
      rows.filter((a) => finite(a.price)).length,
    );
    stat(
      $("coverage-stats"),
      "目標標的",
      groups.reduce((s, g) => s + (g.target || 0), 0) || "見名單",
    );
    stat(
      $("coverage-stats"),
      "本輪成功",
      groups.reduce((s, g) => s + (g.updated || 0), 0),
    );
    stat(
      $("coverage-stats"),
      "本輪失敗",
      groups.reduce((s, g) => s + (g.failed || 0), 0),
    );
    table(
      ["市場", "目標", "已存", "新鮮", "較舊", "本輪成功", "失敗"],
      groups.map((g) => {
        const r = el("tr");
        [
          g.market,
          g.target,
          g.stored,
          g.fresh,
          g.stale,
          g.updated,
          g.failed,
        ].forEach((v) => r.append(cell(v ?? "—")));
        return r;
      }),
      $("coverage-table"),
    );
    clear($("method"));
    for (const t of [
      "資料流程：GitHub Actions 直接取得公開行情 → 保存版本化公開快照及日線 → GitHub Pages 自動更新。開頁會自動讀取，毋須 Google Drive、Mac 或手動匯入。私人研究庫、授權原始數據及帳戶資料繼續獨立保存。",
      "相對強弱：20 個共同觀測期間嘅標的價格回報，減同日期基準價格回報。美股／ETF 預設 SPY，Crypto 用 BTC，港股用 2800.HK；FX／商品預設絕對升幅。更換基準可重新排序。呢個係自家公開公式，唔係 RSR／URSR／NRSR 或 LazyRS 嘅專有分數。",
      "1／5／20／60／120 日指日線觀測間距；美股按交易日，Crypto 包含週末。MA 係可用日線收市價簡單均線。三燈使用同口徑分析價格（唔用未調整即時價混算），由左至右對應有冇高於 20／50／200 日線。缺歷史顯示空值。",
      "比較圖只使用共同日期，每條線由共同起點重設 100。價格幣別按原資產；未做匯率對沖，未計稅或費用。Yahoo 供應商調整價可能包含拆股及股息再投資；Crypto 為現貨價，口徑各有不同。原始歷史亦可能修訂，並非嚴格 PIT 投資績效／回測。",
      "市場寬度同排名只基於已成功收集名單，名單可能有存活偏差。新鮮度按行情時鐘判斷，收集成功唔代表報價即時。來源失敗保留上次成功數據及原日期；各市場休市日不同，勿混作同步可成交價。",
      "宏觀資料係現時可見嘅修訂版本，唔係歷史發布 vintage。利率用百分點、金額標明百萬／十億，流動性代理冇經交易效果驗證。",
    ])
      $("method").append(el("p", t));
    const scope = [
      [
        "跨市場／美股／港股／ETF／FX／商品",
        "已啟用",
        "實際數目見上表；有缺口保留原日期",
      ],
      [
        "300 種 Crypto 價格",
        "輪流更新",
        "實際成功名單按來源，唔保證全數每15分鐘刷新",
      ],
      ["日線走勢／共同日期比較／觀察名單", "已啟用", "上限10條線，可下載圖表"],
      ["官方宏觀／流動性", "部分啟用", "來源觀測日期及修訂限制另列"],
      ["Crypto funding／OI", "BTC 部分啟用", "一個永續合約，唔係全市場持倉"],
      [
        "既有15m／1h及期權原始收集",
        "獨立原流程",
        "私人存檔照常；本網站唔會公開私人原始庫",
      ],
      ["RSR／URSR／NRSR 正式排行", "未接網站", "唔以簡化相對強弱取代正式策略"],
      [
        "Google 搜尋情緒",
        "來源資格待接",
        "官方 API 有限 alpha；唔以價格動能冒充搜尋量",
      ],
      [
        "投資人持倉／申報異動",
        "待接完整資訊表",
        "現階段只列官方入口及有接駁嘅申報元資料",
      ],
      ["VIX／信用利差", "官方連結", "再發布資格未核實，唔公開其數值"],
      [
        "小時級超額／歷史名次／反彈比",
        "待擴充",
        "現階段只提供日線比較及可追溯價格",
      ],
      ["策略回測／研究報告", "私人庫保留", "未獲公開授權嘅報告唔放公開 GitHub"],
    ];
    table(
      ["功能", "狀態", "覆蓋／限制"],
      scope.map((line) => {
        const r = el("tr");
        line.forEach((v) => r.append(cell(v)));
        return r;
      }),
      $("scope-table"),
    );
    const detail = el(
      "p",
      `來源名單：${data?.metadata?.directory?.source || data?.metadata?.directory?.status || "見 GitHub universe 設定"}。公開收集器版本：${data?.schema_version || "—"}。`,
      "caption",
    );
    $("method").append(
      detail,
      link("參考介面：LazyRS ↗", "https://lazyrs.trade/"),
      el("span", " · "),
      link("程式與名單 ↗", "https://github.com/pepe760/market-data-pipeline"),
    );
  }
  async function ensureSeries(a) {
    if (!a?.series_path || a._seriesLoaded) return;
    if (!/^data\/series\/[a-zA-Z0-9_.-]+\.json$/.test(a.series_path)) return;
    try {
      const r = await fetch("./" + a.series_path);
      if (!r.ok) throw Error("not found");
      const payload = await r.json();
      if (payload.symbol !== a.symbol || payload.analytics_asof !== a.analytics_asof) return;
      const points = payload.series;
      if (Array.isArray(points)) {
        a.series = points;
        priceCache.delete(a.symbol);
        relativeCache.clear();
        a._seriesLoaded = true;
      }
    } catch {}
  }
  function addCompare(symbol) {
    const a = findAsset(symbol.trim().toUpperCase());
    if (!a) {
      $("compare-note").textContent = "呢個代號未有公開價格。";
      return;
    }
    if (selected.includes(a.symbol)) {
      location.hash = "compare";
      return;
    }
    if (selected.length >= 10) {
      $("compare-note").textContent = "最多比較 10 個標的，請先移除一個。";
      return;
    }
    selected.push(a.symbol);
    saveLocal("wml-compare-v1", selected);
    location.hash = "compare";
    renderCompare();
  }
  function commonChart(assets, limit = 90) {
    const maps = assets.map(
      (a) => new Map(series(a).map((p) => [p.date, p.close])),
    );
    let days = assets.length
      ? series(assets[0])
          .map((p) => p.date)
          .filter((d) => maps.every((m) => m.has(d)))
      : [];
    days = days.slice(-limit);
    return {
      days,
      lines: assets.map((a, i) => ({
        a,
        color: colors[i % colors.length],
        values: days.map((d) => (maps[i].get(d) / maps[i].get(days[0])) * 100),
      })),
    };
  }
  function plot(target, chart) {
    clear(target);
    if (chart.days.length < 2) {
      target.append(
        el("p", "共同日期不足，等資料更新，或減少比較標的。", "empty"),
      );
      return null;
    }
    const w = 980,
      h = 360,
      p = { l: 58, r: 24, t: 22, b: 39 };
    const all = chart.lines.flatMap((x) => x.values);
    let min = Math.min(...all),
      max = Math.max(...all);
    const pad = (max - min) * 0.08 || 1;
    min -= pad;
    max += pad;
    const x = (i) => p.l + (i * (w - p.l - p.r)) / (chart.days.length - 1),
      y = (v) => p.t + ((max - v) * (h - p.t - p.b)) / (max - min);
    const svg = sEl("svg", {
      viewBox: `0 0 ${w} ${h}`,
      role: "img",
      "aria-label": "共同日期價格比較圖，起點100",
    });
    const background = sEl("rect", {
      x: 0,
      y: 0,
      width: w,
      height: h,
      fill: "var(--panel)",
    });
    svg.append(background);
    for (let i = 0; i < 5; i++) {
      const value = min + ((max - min) * i) / 4,
        yy = y(value);
      svg.append(sEl("line", { x1: p.l, y1: yy, x2: w - p.r, y2: yy }));
      const t = sEl("text", { x: p.l - 8, y: yy + 4, "text-anchor": "end" });
      t.textContent = value.toFixed(0);
      svg.append(t);
    }
    for (const i of [
      0,
      Math.floor((chart.days.length - 1) / 2),
      chart.days.length - 1,
    ]) {
      const t = sEl("text", {
        x: x(i),
        y: h - 12,
        "text-anchor":
          i === 0 ? "start" : i === chart.days.length - 1 ? "end" : "middle",
      });
      t.textContent = chart.days[i];
      svg.append(t);
    }
    for (const line of chart.lines) {
      svg.append(
        sEl("path", {
          d: line.values
            .map(
              (v, i) =>
                (i ? "L" : "M") + x(i).toFixed(2) + "," + y(v).toFixed(2),
            )
            .join(" "),
          stroke: line.color,
        }),
      );
    }
    const cursor = sEl("line", {
      x1: p.l,
      y1: p.t,
      x2: p.l,
      y2: h - p.b,
      visibility: "hidden",
      "stroke-dasharray": "3 4",
    });
    svg.append(cursor);
    svg.addEventListener("pointermove", (event) => {
      const box = svg.getBoundingClientRect();
      const px = ((event.clientX - box.left) / box.width) * w;
      const i = Math.max(
        0,
        Math.min(
          chart.days.length - 1,
          Math.round(((px - p.l) / (w - p.l - p.r)) * (chart.days.length - 1)),
        ),
      );
      cursor.setAttribute("x1", x(i));
      cursor.setAttribute("x2", x(i));
      cursor.setAttribute("visibility", "visible");
      $("chart-cursor").textContent =
        chart.days[i] +
        " · " +
        chart.lines
          .map((l) => `${l.a.symbol} ${l.values[i].toFixed(2)}`)
          .join("  /  ");
    });
    svg.addEventListener("pointerleave", () =>
      cursor.setAttribute("visibility", "hidden"),
    );
    target.append(svg);
    return svg;
  }
  let comparisonGeneration = 0;
  async function renderCompare() {
    const generation = ++comparisonGeneration;
    const assets = selected.map(findAsset).filter(Boolean);
    clear($("compare-tags"));
    assets.forEach((a, i) => {
      const b = button(
        "",
        () => {
          selected = selected.filter((s) => s !== a.symbol);
          saveLocal("wml-compare-v1", selected);
          renderCompare();
        },
        "compare-tag",
      );
      const dot = el("span");
      dot.style.background = colors[i % colors.length];
      b.append(dot, document.createTextNode(a.symbol + " ×"));
      b.title = "移除 " + a.symbol;
      $("compare-tags").append(b);
    });
    await Promise.all(assets.map(ensureSeries));
    if (generation !== comparisonGeneration) return;
    const chart = commonChart(assets, Number($("compare-range").value));
    plot($("compare-chart"), chart);
    $("compare-note").textContent = chart.days.length
      ? `${chart.days[0]} → ${chart.days.at(-1)} · ${chart.days.length} 個共同觀測日 · 本幣價格重設100，Yahoo 使用供應商調整價（可能含股息），Crypto 用現貨價；未換匯或扣費，唔係策略回測。`
      : "先加入已收集嘅標的。";
    $("export-chart").disabled = chart.days.length < 2;
  }
  async function detail(a) {
    $("asset-title").textContent = `${a.symbol} · ${a.name || ""}`;
    clear($("asset-body"));
    const metrics = el("div", null, "detail-metrics");
    for (const [label, v] of [
      ["價格", `${num(a.price)} ${a.currency || ""}`],
      ["20日", pct(a.returns?.d20)],
      ["60日", pct(a.returns?.d60)],
      ["20日年化波幅", pct(a.vol20)],
    ]) {
      const box = el("div");
      box.append(el("small", label), el("strong", v));
      metrics.append(box);
    }
    $("asset-body").append(
      metrics,
      el(
        "p",
        `報價 ${a.price_asof || ageText(a)} · 接收 ${a.received_at || "未提供"} · ${stateText(a) || "來源正常"}。分析日 ${a.analytics_asof || "未取得"}${a.analytics_partial ? "（未收市日線）" : ""}。`,
        "caption",
      ),
    );
    if (a.identity_status === "UNVERIFIED_SYMBOL_MATCH") $("asset-body").append(el("p", "Crypto 價格以 Binance 幣對為準；顯示名稱只經同代號配對，項目身份未完成獨立核實。", "caption"));
    const chart = el("div", null, "chart");
    $("asset-body").append(chart);
    $("asset-body").append(
      button(
        "加入走勢比較",
        () => {
          addCompare(a.symbol);
          $("asset-detail").close();
        },
        "button primary",
      ),
    );
    if (a.source_url)
      $("asset-body").append(
        el("span", " · "),
        link("行情來源 ↗", a.source_url),
      );
    if (!$("asset-detail").open) $("asset-detail").showModal();
    await ensureSeries(a);
    if ($("asset-title").textContent.startsWith(a.symbol + " ·"))
      plot(chart, commonChart([a], 180));
  }
  function render() {
    if (!data) return;
    const info = views[active] || views.overview;
    $("page-title").textContent = info[0];
    $("eyebrow").textContent = info[1];
    $("subtitle").textContent = info[2];
    document.title = info[0] + " · 市場研究台";
    document.querySelectorAll("[data-tab]").forEach((n) => {
      if (n.dataset.tab === active) n.setAttribute("aria-current", "page");
      else n.removeAttribute("aria-current");
    });
    $("watch-count").textContent = watches.size || "";
    [
      "overview",
      "market-view",
      "macro-view",
      "compare-view",
      "coverage-view",
    ].forEach((id) => ($(id).hidden = true));
    renderQuotes();
    if (active === "overview") {
      $("overview").hidden = false;
      renderOverview();
    } else if (active === "macro") {
      $("macro-view").hidden = false;
      renderMacro();
    } else if (active === "compare") {
      $("compare-view").hidden = false;
      renderCompare();
    } else if (active === "coverage") {
      $("coverage-view").hidden = false;
      renderCoverage();
    } else {
      $("market-view").hidden = false;
      renderMarket(true);
    }
  }
  function route() {
    active = location.hash.slice(1);
    if (!views[active]) active = "overview";
    page = 0;
    render();
  }
  async function load() {
    if (loading) return;
    loading = true;
    $("refresh").disabled = true;
    $("connection").textContent = "更新中";
    try {
      const response = await fetch("./data/latest.json", { cache: "no-store" });
      if (!response.ok) throw Error("HTTP " + response.status);
      const next = await response.json();
      if (
        next.schema_version !== 1 ||
        !Array.isArray(next.markets) ||
        !next.generated_at
      )
        throw Error("INVALID_SCHEMA");
      data = next;
      rows = next.markets.filter((a) => typeof a.symbol === "string");
      priceCache.clear();
      relativeCache.clear();
      context = next.context?.macro ? next.context : null;
      if (!context) {
        try {
          const c = await fetch("./data/context.json", { cache: "no-store" });
          if (c.ok) context = await c.json();
        } catch {}
      }
      const lag = Date.now() - Date.parse(data.generated_at);
      $("connection").textContent =
        lag > 5 * 3600000
          ? "快照較舊"
          : ["OK", "COMPLETE"].includes(data.status)
            ? "自動資料已載入"
            : "部分覆蓋";
      $("connection").className =
        "status " +
        (lag > 5 * 3600000 || !["OK", "COMPLETE"].includes(data.status) ? "warn" : "good");
      $("collected-at").textContent = hkTime(data.generated_at);
      $("footer-time").textContent = "公開快照 · " + hkTime(data.generated_at);
      $("update-note").textContent =
        data.cadence || "定時收集，價格時點逐行顯示";
      $("notice").hidden = lag <= 5 * 3600000;
      $("notice").textContent =
        "最近公開快照已超過5小時，請查看 GitHub 收集記錄；以下仍顯示原日期。";
      clear($("asset-options"));
      rows.forEach((a) => {
        const o = el("option");
        o.value = a.symbol;
        o.label = a.name || a.symbol;
        $("asset-options").append(o);
      });
      render();
    } catch (error) {
      $("connection").textContent = "暫未更新";
      $("connection").className = "status warn";
      $("notice").hidden = false;
      $("notice").textContent = data
        ? "新快照暫未取得，保留上次已載入資料。"
        : "公開快照尚未載入，請稍後刷新或查看 GitHub Actions。";
    } finally {
      loading = false;
      $("refresh").disabled = false;
    }
  }
  // Exercise the exact browser calculation functions in Node without booting a DOM.
  if (typeof module === "object" && module.exports) {
    module.exports = {
      paired, commonChart,
      setAssets(assets) { rows = assets; priceCache.clear(); relativeCache.clear(); },
    };
    return;
  }
  $("theme").addEventListener("click", () => {
    const theme =
      document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    saveLocal("wml-theme-v1", theme);
  });
  document.documentElement.dataset.theme = loadLocal("wml-theme-v1", "light");
  $("refresh").addEventListener("click", load);
  window.addEventListener("hashchange", route);
  for (const id of [
    "market-search",
    "sort",
    "benchmark",
    "trend-filter",
    "sector-filter",
  ])
    $(id).addEventListener(id === "market-search" ? "input" : "change", () => {
      page = 0;
      renderMarket();
    });
  $("prev-page").addEventListener("click", () => {
    page = Math.max(0, page - 1);
    renderMarket();
  });
  $("next-page").addEventListener("click", () => {
    page++;
    renderMarket();
  });
  $("add-compare").addEventListener("click", () => {
    addCompare($("compare-input").value);
    $("compare-input").value = "";
  });
  $("compare-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("add-compare").click();
  });
  $("compare-range").addEventListener("change", renderCompare);
  $("asset-detail").addEventListener("click", (e) => {
    if (e.target === $("asset-detail")) $("asset-detail").close();
  });
  $("export-chart").addEventListener("click", async () => {
    const svg = $("compare-chart").querySelector("svg");
    if (!svg) return;
    const clone = svg.cloneNode(true),
      styles = getComputedStyle(document.documentElement);
    clone.querySelectorAll("*").forEach((n) => {
      for (const attr of ["stroke", "fill"]) {
        const v = n.getAttribute(attr);
        const m = v?.match(/^var\((--[^)]+)\)$/);
        if (m) n.setAttribute(attr, styles.getPropertyValue(m[1]).trim());
      }
      if (n.tagName === "text") {
        n.setAttribute("fill", styles.getPropertyValue("--muted").trim());
        n.setAttribute("font-family", "sans-serif");
        n.setAttribute("font-size", "10");
      }
      if (n.tagName === "path") {
        n.setAttribute("fill", "none");
        n.setAttribute("stroke-width", "2");
      }
      if (n.tagName === "line")
        n.setAttribute("stroke", styles.getPropertyValue("--line").trim());
    });
    clone.setAttribute("xmlns", svgNS);
    const blob = new Blob([new XMLSerializer().serializeToString(clone)], {
      type: "image/svg+xml",
    });
    const url = URL.createObjectURL(blob),
      img = new Image();
    img.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = 1960;
      canvas.height = 900;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = styles.getPropertyValue("--panel").trim();
      ctx.fillRect(0, 0, 1960, 900);
      ctx.drawImage(img, 0, 0, 1960, 720);
      let legendX = 35, legendY = 755;
      ctx.font = "22px sans-serif";
      selected.map(findAsset).filter(Boolean).forEach((a, i) => {
        const width = ctx.measureText(a.symbol).width + 62;
        if (legendX + width > 1900) { legendX = 35; legendY += 35; }
        ctx.fillStyle = colors[i % colors.length];
        ctx.fillRect(legendX, legendY - 14, 21, 5);
        ctx.fillStyle = styles.getPropertyValue("--ink").trim();
        ctx.fillText(a.symbol, legendX + 30, legendY);
        legendX += width;
      });
      ctx.font = "17px sans-serif";
      let line = "", lineY = legendY + 38;
      for (const char of $("compare-note").textContent) {
        if (ctx.measureText(line + char).width > 1890) {
          ctx.fillText(line, 35, lineY); line = ""; lineY += 25;
        }
        line += char;
      }
      ctx.fillText(line, 35, lineY);
      canvas.toBlob((out) => {
        if (!out) return;
        const download = URL.createObjectURL(out),
          a = el("a");
        a.href = download;
        a.download = "william-market-comparison.png";
        a.click();
        setTimeout(() => URL.revokeObjectURL(download), 1000);
      });
      URL.revokeObjectURL(url);
    };
    img.src = url;
  });
  route();
  load();
  setInterval(() => {
    if (!document.hidden) load();
  }, 60000);
})();
