# William's Market Lab

Public website: https://pepe760.github.io/market-data-pipeline/

The site reads its own public GitHub snapshot automatically. Neither opening the site nor the public collection workflow requires a Mac, Google Drive, a Drive login, a private snapshot import, or a long-lived publishing token.

## Use

- **市場總覽**: reference cross-asset watch table, breadth of the collected universe and five-session US relative movers.
- **美股／港股／Crypto／外匯／大宗商品／ETF**: search, sector and trend filters, sort and change benchmark. Prices, analysis dates and incomplete sessions remain visible.
- **走勢比較**: add up to ten collected symbols, compare common dates rebased to100, and download a PNG. Different native currencies and adjusted/spot bases are explicitly qualified.
- **我的觀察**: 星標名單留喺目前瀏覽器。XLY 三個月 A/B 紙上研究由 GitHub 自動更新，顯示訊號、到期回報及預測誤差。
- **宏觀與流動性**: dated government observations and descriptive derived series, plus explicitly incomplete source connections.
- **收集與方法**: target, stored, fresh, stale and failed coverage; formula and source limitations.

## Automatic collection and hosting

`public_terminal.yml` runs at minutes7,22,37,52 every hour, seven days a week. This is a best-effort GitHub schedule rather than a guaranteed real-time service. Each ordinary run has a bounded180-history request budget, split156 daily plus24 completed15m histories across a disclosed60-name core. The three reference instruments count inside those24 requests. Older attempts rotate; this is not simultaneous15m history for every symbol. An explicit full refresh allows1800 daily plus60 intraday requests. Crypto last prices can refresh through one bulk source request independently of the slower daily-history rotation. Market-session gates limit repeat cash-market requests; actual source clocks determine freshness. Public macro series refresh daily.

The workflow checks out the existing `gh-pages` source, preserves its previous public data, collects only public allowlisted market facts, installs reviewed assets and pushes that source. Pages acceptance first observes builds for that exact commit. If none appears within45 seconds, it records intent and requests one fallback build; an uncertain response is never automatically retried, while read-only polling can still confirm success. It checks up to100 recent builds and100 exact-SHA workflow runs for up to10 minutes, accepting a matching `built` result or a successful GitHub-generated Pages workflow (`dynamic/pages/pages-build-deployment`, `dynamic` event, `gh-pages` branch). This requires narrowly scoped `actions: read`. Failed or cancelled peers get time for a successor. Public HTML, JavaScript, CSS and three JSON files then undergo a separate SHA256 readback for up to2 minutes. The artifact separates source validation, push, build and live-byte results; live-byte agreement does not certify browser interaction or data completeness. A collector or publishing failure does not turn missing observations into prices. Each snapshot includes coverage and timestamps. Successful same-repo daily, options, private15m or crypto collectors also trigger this same serial public writer when cron is delayed. Failed collectors and fork runs do not trigger it, and it never triggers itself. GitHub-wide delays can still affect both routes.

Public snapshots and historical site revisions live in the `gh-pages` branch. The main branch holds code and configured universes. Collector completion events can request a public refresh; the public writer fetches its own public sources. Drive archives and licensed data remain private.

## Data contract

- `data/latest.json`: schema1 market metadata, public quotes, daily returns and trend inputs, coverage, observed and received clocks, and short recent series.
- `data/series/<symbol-sha256-prefix>.json`: longer daily series loaded on demand for comparison/details. The symbol is also bound inside the file.
- `data/intraday/<symbol-sha256-prefix>.json`: up to64 completed OHLCV bars with exact UTC boundaries, symbol/asof binding and no filled gaps.
- `data/context.json`: government economic observations with native units/frequency, current-revised-vintage limitations, derived formula inputs, available derivatives context, source links and unconfigured interfaces.
- `data/xly_ab.json`: XLY 三個月 A/B 研究嘅公共狀態、已鎖定訊號、已到期結果同配對評分。舊紀錄沿用 `gh-pages` 版本。見 [XLY_AB_PROTOCOL.md](XLY_AB_PROTOCOL.md)。
- Daily Yahoo history uses a single vendor-adjusted basis where supplied; otherwise a declared raw basis. Binance uses USDT spot prices. Current sessions can be incomplete. Quotes are distinct from analysis prices and dates.
- The US and Hong Kong exchanges have separate completed-session checks. For ACAA, AACO and AACP, a missing Yahoo close can be resolved by an exact-date Nasdaq Official Closing Price. Its timestamp denotes session close, and Yahoo analytics retain their own dates and source.
- Cash daily rows are completed only after the actual XNYS close plus90 minutes, or XHKG close plus100 minutes including the closing auction allowance. The returned quote clock must also reach the observed daily row's actual exchange close; elapsed time never upgrades a cached intraday quote. Half-days and holidays use the exchange calendar, and cash bars on non-session dates are rejected. A provider's `currentTradingPeriod` can describe a later session and is not used to certify those cash closes. A rejected close retains the old price/clock and includes a bounded `close_check` explaining the required session and returned clocks.
- Relative return uses identical start/end dates shared by asset and benchmark. Different venue closes on the same date are still asynchronous; this is descriptive analysis, not execution evidence.
- Price history, present-day membership and revised macro observations do not establish strict PIT data, an executable strategy, OOS performance or an investment recommendation.

## Coverage and reference

The interface is independently implemented, informed by the public category organization and visible features at https://lazyrs.trade/ . No paid pages, proprietary scores, source code or licensed private datasets are copied.

The configured price universe targets600 US names,300 crypto spot pairs, a representative HK list, G10/emerging FX and global/sector/theme ETFs. Duplicate ETF symbols appear once. The exact successful universe and clocks are in the coverage page. A live listing directory does not mean every listed stock is priced. Crypto symbol matches are explicitly unverified identity candidates; do not use those candidate IDs as an audited asset mapping.

Fifteen government economic series cover Fed assets, Wednesday-level TGA, ON RRP, reserve balances (weekly average), overnight repo, yields, SOFR, IORB, CPI/core CPI, unemployment, payrolls and claims. Derived monetary inputs require the declared dates/units. Source-restricted VIX/credit series are official links only. Google Trends access, full SEC holdings parsing, market caps, persistent historical ranks and official RSR-family scores remain explicit gaps rather than invented values.

## Validation

See `TERMINAL_INTRADAY.md` for elapsed-hour window rules and HK lunch gaps. The comparison view can switch between daily and core15m bars.

Run `python -m unittest discover -s tests -p 'test*terminal*.py' -v` and `node --test tests/test_terminal_frontend.cjs`. The full existing repository test matrix installs the requirements files before running all tests. Publishing should only be considered complete after the workflow's matching-commit Pages build and actual public JSON/assets/functional UI have been verified.
