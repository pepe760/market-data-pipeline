# Collection source repairs — 2026-10-08

BRK.B / BF.B remain canonical stored IDs; Yahoo requests use BRK-B / BF-B.
SATS requests on windows starting June 24, 2026 or later use ECHO; the provider symbol is recorded with each observation. The issuer states the CUSIP is unchanged:
https://ir.echostar.com/news-releases/news-release-details/echostar-changing-stocker-ticker-sats-echo-marking-companys-next

AVB/EQR windows wholly after August 17 and EA windows wholly after August 4 have no expected observations. Preserve old IDs and batches; do not map merger successors onto their historical prices. Status NOT_EXPECTED_RETIRED is explicit in private quality checks:
https://www.sec.gov/Archives/edgar/data/915912/000110465926097833/tm2623381d1_ex99-1.htm
https://careers.ea.com/ea-play/news/ea-announces-completion-of-acquisition

News empty responses get one bounded retry. They still remain EMPTY_UNVERIFIED if no usable response returns. Missing/invalid rows still make the batch PARTIAL; no success threshold is weakened. Private quality reports now contain rejection reasons and exception types without raw exception responses. HUBB/PSKY/WBD failures require this new evidence; no fabricated values, forward filling or presumed delistings.
