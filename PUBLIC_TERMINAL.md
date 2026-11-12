# William's Market Lab

Public website: https://pepe760.github.io/market-data-pipeline/

The site reads its own public GitHub snapshot automatically. Neither opening the site nor the public collection workflow requires a Mac, Google Drive, a Drive login, a private snapshot import, or a long-lived publishing token.

## Use

- **市場總覽**: reference cross-asset watch table, breadth of the collected universe and five-session US relative movers.
- **美股／港股／Crypto／外匯／大宗商品／ETF**: search, sector and trend filters, sort and change benchmark. Prices, analysis dates and incomplete sessions remain visible.
- **走勢比較**: add up to ten collected symbols, compare common dates rebased to100, and download a PNG. Different native currencies and adjusted/spot bases are explicitly qualified.
- **我的觀察**: star symbols. This preference stays only in that browser's local storage.
- **宏觀與流動性**: dated government observations and descriptive derived series, plus explicitly incomplete source connections.
- **收集與方法**: target, stored, fresh, stale and failed coverage; formula and source limitations.

## Automatic collection and hosting

`public_terminal.yml` runs at minutes7,22,37,52 every hour, seven days a week. This is a best-effort GitHub schedule rather than a guaranteed real-time service. Each ordinary run has a bounded180-history request budget, split156 daily plus24 completed15m histories across a disclosed60-name core. The three reference instruments count inside those24 requests. Older attempts rotate; this is not simultaneous15m history for every symbol. An explicit full refresh allows1800 daily plus60 intraday requests. Crypto last prices can refresh through one bulk source request independently of the slower daily-history rotation. Market-session gates limit repeat cash-market requests; actual source clocks determine freshness. Public macro series refresh daily.

The workflow checks out the existing `gh-pages` source, preserves its previous public data, collects only public allowlisted market facts, installs reviewed assets, pushes that source and explicitly requests a Pages build. It verifies the build against the pushed commit. A collector or publishing failure does not turn missing observations into prices. The job summary/artifact and each snapshot include coverage and timestamps.

Public snapshots and historical site revisions live in the `gh-pages` branch. The main branch holds code and configured universes. This new flow is independent of existing private collection workflows, whose Drive archives and licensed data remain private.

## Data contract

- `data/latest.json`: schema1 market metadata, public quotes, daily returns and trend inputs, coverage, observed and received clocks, and short recent series.
- `data/series/<symbol-sha256-prefix>.json`: longer daily series loaded on demand for comparison/details. The symbol is also bound inside the file.
- `data/intraday/<symbol-sha256-prefix>.json`: up to64 completed OHLCV bars with exact UTC boundaries, symbol/asof binding and no filled gaps.
- `data/context.json`: government economic observations with native units/frequency, current-revised-vintage limitations, derived formula inputs, available derivatives context, source links and unconfigured interfaces.
- Daily Yahoo history uses a single vendor-adjusted basis where supplied; otherwise a declared raw basis. Binance uses USDT spot prices. Current sessions can be incomplete. Quotes are distinct from analysis prices and dates.
- The US and Hong Kong exchanges have separate completed-session checks. On ACAA, a missing Yahoo close can be resolved by an exact-date Nasdaq Official Closing Price. Its timestamp denotes session close, and Yahoo analytics retain their own dates and source.
- Relative return uses identical start/end dates shared by asset and benchmark. Different venue closes on the same date are still asynchronous; this is descriptive analysis, not execution evidence.
- Price history, present-day membership and revised macro observations do not establish strict PIT data, an executable strategy, OOS performance or an investment recommendation.

## Coverage and reference

The interface is independently implemented, informed by the public category organization and visible features at https://lazyrs.trade/ . No paid pages, proprietary scores, source code or licensed private datasets are copied.

The configured price universe targets600 US names,300 crypto spot pairs, a representative HK list, G10/emerging FX and global/sector/theme ETFs. Duplicate ETF symbols appear once. The exact successful universe and clocks are in the coverage page. A live listing directory does not mean every listed stock is priced. Crypto symbol matches are explicitly unverified identity candidates; do not use those candidate IDs as an audited asset mapping.

Fifteen government economic series cover Fed assets, Wednesday-level TGA, ON RRP, reserve balances (weekly average), overnight repo, yields, SOFR, IORB, CPI/core CPI, unemployment, payrolls and claims. Derived monetary inputs require the declared dates/units. Source-restricted VIX/credit series are official links only. Google Trends access, full SEC holdings parsing, market caps, persistent historical ranks and official RSR-family scores remain explicit gaps rather than invented values.

## Validation

See `TERMINAL_INTRADAY.md` for elapsed-hour window rules and HK lunch gaps. The comparison view can switch between daily and core15m bars.

Run `python -m unittest discover -s tests -p 'test*terminal*.py' -v` and `node --test tests/test_terminal_frontend.cjs`. The full existing repository test matrix installs the requirements files before running all tests. Publishing should only be considered complete after the workflow's matching-commit Pages build and actual public JSON/assets/functional UI have been verified.
