# Completed15-minute market supplement

The ordinary180-history request budget is split into156 daily and24
intraday requests. The three reference symbols SPY, BTCUSDT and2800.HK
are prioritized inside that24. Older intraday attempts rotate through
the remaining60-name configured core. Quotes outside that core keep
their daily analytics. Target cadence is15minutes and each symbol's
actual observation time is displayed. Rotation is roughly three runs
when all60 names are eligible.

An explicit full refresh uses1860 requests, comprising1800 daily and
at most60 intraday. Bulk crypto prices and the existing daily universe
and macro metadata requests remain separate from this history budget.

Yahoo requests five days of15-minute regular-session bars and retains
the last64 completed intervals. Binance requests65 rows to retain up
to64 completed15-minute intervals. These64 intervals represent16hours.
Yahoo's returned trading-period boundaries determine session inclusion.
Unaligned appended last-price points, open bars, future bars, and
malformed/foreign-symbol rows do not become history.

The1h and4h values require contiguous completed15-minute bars with exact
UTC endpoints. Overnight gaps, lunch gaps, missing bars, and endpoints
absent from either the asset or its benchmark yield null. Relative
return subtracts the benchmark's return over exactly the asset's same
qualified window. No newer benchmark endpoint is substituted.

Daily3-session and10-session returns use the existing daily series and
introduce no additional source request. Their peer comparisons share
the same daily start/end dates. Heterogeneous daily venue closes retain
the existing descriptive qualification.

Each qualifying core row has an `intraday` object containing actual
`asof`, retrieval time, attempt time, source, quality, bar count, gap
count, fresh status, h1/h4 returns and qualified windows. The lazy file
`data/intraday/<symbol-hash>.json` binds the symbol and asof to up to64
OHLCV bars with UTC `start` and exclusive `end` clocks. Source failures
retain the preceding successful file and its clocks while recording
the new attempt. Coverage reports the60 targets, eligible requests,
actual attempts, stored/fresh observations, failures and remaining gaps.
