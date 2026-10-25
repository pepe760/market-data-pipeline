# Broad collection expansion — 2026-10-26 HKT

William authorized source push/deploy, private Drive storage, 300 different crypto
hourly price symbols and (after feasibility review) Deribit's7 currently observed
option underlyings instead of inventing300 coin option markets. No subscription,
trade or public redistribution authorized. Old collectors remain unchanged apart
from backward-compatible optional1h parsing, tested alongside their old fixtures.

## Universe comparison

Local Sharadar ticker metadata:21,859 historical rows, June20 export, NOT current
PIT membership. Filter snapshot-not-delisted/USD/common-stock/NYSE-NASDAQ-NYSEMKT,
then exact-symbol intersect with fresh official Nasdaq current directories:

- 5,816 snapshot-active common symbols;5,549 current-directory matches.
- 267 snapshot-active names not matched; NOT automatically declared delisted.
- Current old price batch has612 observed symbols;584 match the new list.
- 4,965 matched candidates additional to the old observed price list.
- 5,549 is a collection target, not proof all have usable Yahoo15m bars.
- Existing14-symbol15m lane and615-name daily config are retained. ETF/new-listing
  names outside the stale Sharadar universe are not falsely removed from old data.
- Private comparison preserves source-file hash, current directory bytes, missing
  names and chosen symbols. No alias/successor splicing, historical universe/PIT
  certification, Sharadar overwrite or new delisting settlement assumptions.

## Lanes and delivery

| Lane | Source/size | Proposed/activated by explicit flag | Storage prefix |
|---|---|---|---|
| stocks15m | Yahoo/chart;5,549 candidates /24 stable shards,max2 simultaneous | Tue-Sat16:13HKT, BROAD_COLLECTION_ENABLED | broad/stocks15m/part00..23/ |
| crypto1h | Binance official archive;300 qualified distinct USDT base-symbol pairs | daily12:29HKT inclweekends, BROAD_COLLECTION_ENABLED | broad/crypto1h/ |
| crypto_options | Deribit active metadata + per-settlement full-chain summaries | hourly atminute43, BROAD_CRYPTO_OPTIONS_ENABLED | broad/crypto_options/ |

Activation and remote hash acceptance must be checked in dated task receipts;
the existence of workflow source is NOT proof of deployment. All schedules are
best effort. Existing intraday_run_guard monitors the OLD intraday15m workflow
only; do not claim it already covers these three new workflows.

Private config `config/broad_universe.json`; no licensed symbol list or raw price
data in public Git. `broad_collect.py cloud --lane ...` downloads that config.
Stock shards are deterministic strides over the fixed private list. No new PAT;
reuse established private Drive Secrets. Each shard has independent checkpoint
and workflow concurrency; failure of one does not cancel the others.

## Crypto qualification / semantics

Complete official archive listing:3,749 symbols /696 filtered USDT candidates.
The selected300 must each have24 valid1h bars for a recent complete UTC day and
pass the vendor archive CHECKSUM. Selection is first300 qualified alphabetical
candidates, NOT marketcap/liquidity ranking or survivor-free historical universe.
Known stablecoin/leveraged-pair filters are limited heuristics; identity mapping
and independent price checks remain unverified. Preserve source observations;
do not splice renamed assets or extend prelisting history.

Native1h archives retained with original ZIP and CHECKSUM inside `raw_bundle.zip`.
Volume is base quantity, not USD notional. Crypto is24/7; no NYSE calendar.
Cloud revisits yesterday and preceding UTC day for one-day outage catch-up.
Older gaps require an explicit dated backfill, not fabricated prices. Initial
qualification of one day is NOT years of history or live hourly delivery.

## Crypto option feasibility / limits

Four public venue listings were inspected: Deribit, OKX, Binance and Bybit. They
did NOT establish300 crypto option underlyings. Bybit's legacy no-baseCoin query
defaults toBTC; its dedicated crypto option-base-coins endpoint was used instead.
Commodity/XAU/XAG/oil and tokenized-gold exposure are not counted as hundreds of
additional crypto assets. This is a bounded venue check, not an exhaustive proof
that no other option market exists anywhere.

Live Deribit pilot:5,044 contracts acrossAVAX/BTC/ETH/HYPE/SOL/TRX/XRP, three
settlement currencies BTC/ETH/USDC. This is7 assets, NOT5,044 assets.447 rows have
BBO flags; all retained, not silently promoted to executable quotes. Summary
includes bid/ask,mark,mark IV,OI,24hvolume and vendor underlying/index.

- Hourly snapshots start prospectively when deployed; NOT historical hourly BBO.
- Observed/summary clocks distinct; summary time is NOT bid/ask exchange clock.
- Premium currency is settlement currency; strike currency is instrument quote
  currency. InverseBTC premiums must NOT be mistaken for USD premiums.
- `mark_iv_vendor_percent` is percent. Mark/IV are not trades or fills.
- No native Greek/size endpoint collected: `greeks=null`, `bid_ask_size=null`.
- Same-summary underlying is NOT a synchronous executable spot quote or PIT join.
- Active instruments seeded from public endpoint and cached24h. New metadata
  mismatches remain PARTIAL and raw responses retained; no inferred contracts.
- Compressed options JSONL and raw response ZIP retain lossless bytes + original
  SHA256 mapping. Initial uncompressed pilot ~10MB/snapshot; compression tested
  separately. Actual daily storage and Google quota must still be monitored.

## Integrity and local use

Root batches contain source snapshots, config, original raw bundle, normalized
Parquet (bars) or `options.jsonl.gz`, quality and manifest. COMMITTED marker goes
LAST after full remote download SHA256 verification. Checkpoint/cache are then
uploaded and read back. Partial source errors are distinct from transfer success.

Local CLI example (exact private lane root required):

```
LOCAL_RCLONE_REMOTE=gdrive_market LOCAL_DRIVE_FOLDER_ID=<existing-private-root-id> \
  python broad_collect.py sync --lane crypto1h --root /absolute/private/broad/crypto1h
```

For stocks pass`--lane stocks15m --shard N`; keep independent root perpartN.
Never run the legacy daily importer on this schema. Catalog indexes immutable
batches; globbed bars overlap across retrieval vintages. Deduplicate intentionally
by instrument/venue/interval/bar_start and choose availability vintage explicitly.
No source is upgraded to trading/PIT/OOS certification by checksum or CI success.

Official sources:
- https://github.com/binance/binance-public-data
- https://docs.deribit.com/articles/options-data-collection-best-practices
- https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency
- https://bybit-exchange.github.io/docs/v5/market/option-base-coins
