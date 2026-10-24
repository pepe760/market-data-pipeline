# Independent 15-minute archive — v1

Built 2026-10-25 for William. This is ingestion engineering, not a strategy study.
No existing daily collector, PIT database, option snapshots, RSR monitor or paper
account is replaced. No subscription, purchase, trade, push or deployment is made.

## What exists, and what does not

| Lane | Implementation | Qualification |
|---|---|---|
| US listed securities | Nasdaq/other-listed current-directory discovery; bounded Yahoo/chart recent 15m OHLCV downloads | Current symbol snapshot, **not historical PIT or all-US/OTC coverage**; vendor adjustment/corporate-action semantics and prices not independently certified |
| Crypto spot / USD-M | Binance official daily 15m ZIP archives, original CSV bytes + SHA256 checksum; explicit symbol lists and backfill window | Venue/pair-specific, not all crypto markets; current USDT spot discovery excludes delisted pairs, old archives retained |
| Options / Greeks | Explicit `BLOCKED_NO_AUTHORIZED_15M_QUOTE_GREEKS_PROVIDER` gate | **0 option quote/Greek rows**. Does not substitute EOD/Yahoo last trades, BS prices, SPXW/10 or QC cloud data |
| Independent random checks | Quality records remain `PENDING` | Structural validation / exchange checksum / same-source replay are NOT independent cross-vendor checks |
| Cloud / local sync | Separate `intraday15m/` private lane; hash readback and final marker; separate SQLite batch catalog + Parquet bars | Workflow source only, disabled unless `INTRADAY15M_ENABLED=true`; not running until pushed/configured/verified |

The bar interval is 15m; collection can run once a day. This is **not** a promise of
live 15-minute delivery, all-US option coverage, long-history stock backfill, or
exchange-certified executable quotes. Yahoo's endpoint is unofficial and may
reject requests or limit recent intraday history. Never bypass a block/captcha.
Paid provider credentials, commercial/Patreon publishing rights and exact
historical quote/Greek entitlements require separate approval and verification.

## Private data layout

```
<private-root>/
  checkpoint.json       # complete tasks + attempted-task rotation; not a PIT clock
  catalog.sqlite        # immutable batch IDs/hashes/paths/quality, not all bar rows
  batches/<run-id>/
    config.json
    <task-hash>/task.json
    <task-hash>/raw.zip + raw.CHECKSUM  # crypto
    <task-hash>/raw.json               # US vendor response
    <task-hash>/bars.parquet           # valid observations only
    <task-hash>/quality.json           # gaps/rejections/actions retained
    quality.json
    manifest.json
    COMMITTED.json
```

US bars retain vendor symbol, venue qualification, OHLCV, bar-start/end UTC,
retrieval `available_at` and adjustment caveat. NYSE RTH expected grids use actual
sessions, holidays, DST and early closes. Only fully closed sessions plus90min
are requested. Missing/crossed OHLC/nonfinite/duplicate observations are flagged,
not filled. Halt/IPO/inactive-day gaps remain unclassified rather than fabricated.
Corporate-action response metadata are retained but not certified or used to
retroactively rewrite bars. No ticker-successor price splice.

Crypto CSV clocks normalize milliseconds or microseconds explicitly. Each full
UTC day expects96 bars; a partial listing day remains PARTIAL. ZIP has one expected
member, bounded compressed/download/decompressed sizes and checksum. Unsupported
market/invalid symbol/schema fails closed. No funding/OI/mark-price history is
claimed by this OHLCV-only version; these require separate native-frequency lanes.

## Bounded operation and resume

Use `intraday_config.example.json` as a *pilot*, not an assertion of total coverage.
All prices/lists/results stay under a private root, never public source/artifacts.
No secrets are accepted as config keys. The scope permits20,000 explicit symbols;
window/jobs/requests/bytes are bounded and oversized combined backfills are rejected.
Failures rotate behind never-attempted work and stock/crypto tasks interleave, so
one unavailable name does not repeatedly block the whole broad universe.
Completed task IDs suppress repeat downloads. A process crash before the final
checkpoint can leave an additional immutable observed-vintage batch on retry;
no historical bytes are overwritten. Gaps beyond Yahoo retention cannot be recovered
by current quotes, and a small request cap cannot magically cover all US names.
Measure throughput/retention coverage before broad activation.

```
python -m pip install -r intraday-requirements.txt
python intraday15m.py plan --root /absolute/private/root --config /private/config.json
python intraday15m.py collect --root /absolute/private/root --config /private/config.json
python intraday15m.py discover-us --root /absolute/private/listings
python intraday15m.py discover-crypto --root /absolute/private/listings
python intraday15m.py plan --root /absolute/private/root --config /private/config.json --universe /private/listings/<snapshot>/universe.json
python intraday15m.py verify --root /absolute/private/root/batches/<run-id>
```

`--universe` expands stock symbols only. Crypto discovery writes a current spot
symbol snapshot; select its symbols into the private `crypto` config explicitly.
US discovery retains raw listing files and ETF/security labels; warrants/preferreds,
OTC and special symbols need identity/provider-compatibility review, not blind
conversion into ordinary stocks. Discovery never erases prior symbol snapshots.

`collect` stdout contains aggregate counts only. Its successful exit means only
the **configured bar tasks** are complete; overall `status` stays PARTIAL because
the requested option/Greek lane is blocked. `jobs_remaining`, failed checks and
independent-crosscheck status must not be hidden by green CI. Actual I/O failures
preserve private raw bytes when received; retries obey limits and do not subscribe.

## Cloud activation — NOT performed by this build

1. Review/push source only; keep current daily workflows and private data untouched.
2. Under the existing dedicated **private** Drive root, save a reviewed
   `config/intraday15m.json`. Optional full US snapshot:
   `config/intraday15m_universe.json` + `use_us_directory_snapshot:true`.
3. Explicitly approve reuse of the established Drive Secrets for this lane,
   then enable `INTRADAY15M_ENABLED=true` and run one manual bounded pilot.
4. New workflow has only scheduled/manual trusted events, `contents:read`, its own
   concurrency group, no artifact upload, no raw data in Git and no secret output.
   Proposed schedule02:19UTC daily is best effort; it is not yet an active schedule.
5. `cloud` resumes only its checkpoint, collects bounded data, uploads non-marker
   files, fully downloads/verifies hashes, publishes marker LAST, then advances and
   reads back checkpoint. Partial tasks retry later, successful tasks skip.
6. Local importer, separately configured after authorization:

```
LOCAL_RCLONE_REMOTE=gdrive_market LOCAL_DRIVE_FOLDER_ID=<private-market-folder-id> \
  python intraday15m.py sync --root /absolute/private/intraday15m
```

Do not run the old `pipeline.py sync` on this new schema or mix it into
`observations.sqlite`/Sharadar. Read Parquet using DuckDB `read_parquet` with
explicit version selection by bar key/available_at; a glob contains overlapping
retrieval vintages and must not be treated as unique market observations.

## Verification boundaries

Run `python -m unittest discover -s tests -v` with intraday requirements installed.
Tests cover synthetic timestamps, gaps, invalid markets, archives, identity,
calendar, manifests and resume. Offline pass is not live source accuracy, legal
entitlement, PIT certification, successful cloud delivery or all-market coverage.
Pilot receipts must state actual accepted counts and every unavailable lane.

Sources reviewed: Binance official public-data README and archive format;
Nasdaq Trader symbol-directory definitions; current yfinance history documentation.
No third-party downloader was downloaded/executed.

## Local acceptance — 2026-10-25 HKT

- Full repository suite: **51 tests PASS, zero skips**, including actual XNYS
  early-close/DST fixtures and Parquet/resume tests. This is engineering validation.
- Live bounded pilots: BTCUSDT/ETHUSDT **192 bars** (2026-10-23 UTC);
  SPY/QQQ **156 bars** (2026-10-21–23 regular sessions). All four configured
  bar tasks complete; both immutable batches pass manifest readback locally.
- Nasdaq current-directory snapshot: **13,199 security symbols discovered**,
  not 13,199 stocks downloaded and not a historical PIT universe.
- Crypto current-list API was unavailable (`BudgetStop`); exact HTTP status was
  not retained in that early pilot. No current full crypto universe claimed.
- **Independent random cross-source price checks PENDING.** The stock pilot
  includes a deterministic review sample queue; structural checks/checksums
  do not establish market-price accuracy. Options/Greeks remain zero/blocked.
- Source/workflow not pushed, cloud schedule not enabled, no Drive upload or
  canonical database import performed. Existing daily collection untouched.
- Local runtime: existing `~/projects/quant_env/bin/python` plus task-local
  `private/intraday-runtime` calendars (set its absolute path in `PYTHONPATH`).
  Global/quant_env packages unchanged. Slow standalone Arrow installation was
  cancelled; the isolated venv is not a ready runtime. Clean CI should install
  `intraday-requirements.txt` before enabling this workflow.
- Receipt: `INTRADAY15M_BUILD_RECEIPT.json`. Private raw pilot paths are under
  this repository's ignored `private/` directory; never commit them.
