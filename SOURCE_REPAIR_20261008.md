# Collection source repairs — 2026-10-08

BRK.B / BF.B remain canonical stored IDs; Yahoo requests use BRK-B / BF-B.
SATS requests on windows starting June 24, 2026 or later use ECHO; the provider symbol is recorded with each observation. The issuer states the CUSIP is unchanged:
https://ir.echostar.com/news-releases/news-release-details/echostar-changing-stocker-ticker-sats-echo-marking-companys-next

AVB/EQR windows wholly after August 17 and EA windows wholly after August 4 have no expected observations. Preserve old IDs and batches; do not map merger successors onto their historical prices. Status NOT_EXPECTED_RETIRED is explicit in private quality checks:
https://www.sec.gov/Archives/edgar/data/915912/000110465926097833/tm2623381d1_ex99-1.htm
https://careers.ea.com/ea-play/news/ea-announces-completion-of-acquisition

News empty responses get one bounded retry. They still remain EMPTY_UNVERIFIED if no usable response returns. Missing/invalid rows still make the batch PARTIAL; no success threshold is weakened. Private quality reports now contain rejection reasons and exception types without raw exception responses. HUBB/PSKY/WBD failures require this new evidence; no fabricated values, forward filling or presumed delistings.

## Live readback

Source commit 566d695; recovery run 37777562727 saved private batch 20261008T123243Z-03827a65e4f0, read back its quality.json. Prices: 610 OK, 3 NOT_EXPECTED_RETIRED, 2 PARTIAL. BF.B, BRK.B, SATS and HUBB now OK. PSKY remains missing September 17–October 2; WBD has missing sessions and a non-positive October 5 row. All seven news responses remain EMPTY_UNVERIFIED after retry. Batch transfer succeeded, but run still returns 2 for incomplete source coverage; operational failure remains explicitly unresolved for these feeds.

ETF option dependency fix ba3ad92, recovery run 37777419220 SUCCESS. Full parquet-test dependency is now installed by option workflow.
