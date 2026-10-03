# Market data pipeline

Public code, private data. No datasets, universes, credentials, raw responses,
reports, or account information belong in this repository or Actions artifacts.

## Status and boundaries

This is a new ingestion lane, not a replacement for an established PIT database.
Keep existing collectors running until a real cloud upload and local import have
both passed verification. `PIPELINE_ENABLED` must be `true` to enable collection.
The initial deployment remains disabled pending private-storage credentials.

Scheduled collection: 22:47 UTC Monday–Friday, retry/catch-up 03:17 UTC
Tuesday–Saturday. These cover US Friday close even though it is Saturday in HK.
The exchange calendar excludes non-sessions and unfinished sessions. Scheduling
is best effort, not an SLA. A rolling 21-calendar-day fetch does not recover
arbitrarily old outages or historical news; keep the gap records.

Prices use Yahoo via yfinance, explicitly separate split-adjusted vendor Close
from dividend-adjusted Adj Close. Neither is labelled raw, and neither is silently
merged into Sharadar. Each batch records retrieval time as `available_at`:
historical values fetched today were NOT necessarily known on their bar date.
Universe is a dated private snapshot, not historical index membership.

Events are current earnings-calendar observations and recent news metadata
(title, URL, publisher, publication time), not full articles or exhaustive news.
Empty results are labelled EMPTY_UNVERIFIED, not proof of no events. A later
revision is a new observation, never a rewrite of an older batch.

The initial lane performs structural validation. Independent, stratified random
cross-vendor checks are still REQUIRED before research qualification. They are
not yet automated here; every batch is RESEARCH_ONLY_UNVERIFIED, never PIT-certified.
No option collection, trading, paper-account writer, or website deployment.

## Private Drive layout

```
Market_Data_Pipeline/
  config/universe.json
  batches/<UTC-run-id>/
    prices.jsonl
    events.jsonl
    quality.json
    manifest.json
    COMMITTED.json
```

Private configuration schema:
`{"as_of":"YYYY-MM-DD","prices":["SPY"],"events":["SPY"]}`.
Prices may cover the existing full universe. Event scope is separately explicit;
do not infer holdings or expose a proprietary selection in public source.

GitHub secrets (never send their contents in chat):

- `DRIVE_TOKEN`: rclone-compatible OAuth token JSON, with refresh_token.
- `DRIVE_FOLDER_ID`: dedicated private folder ID.
- Optional `DRIVE_CLIENT_ID` and `DRIVE_CLIENT_SECRET` for a dedicated OAuth client.

Prefer dedicated credentials. Setting a root folder in rclone is routing, NOT an
OAuth permission boundary. Anyone able to change a trusted workflow may use its
secrets. Existing broad Drive credentials require explicit approval before reuse.
No pull-request triggers may access these secrets. Only schedule/manual runs do.

After secrets and private config are ready, set repository variable
`PIPELINE_ENABLED=true`, manually run Daily collection, verify the private batch,
then verify a local import before changing an old local scheduler.

Cloud command: `python pipeline.py cloud` (requires rclone on PATH).
Local command: `python pipeline.py sync --root /absolute/private/database/path`
using the same secret environment, OR an existing local rclone remote via
`LOCAL_RCLONE_REMOTE` and `LOCAL_DRIVE_FOLDER_ID`. No credential is stored in code.
Local sync uses a lock, verifies an explicit commit marker and exact hashes, then
imports append-only observations into `observations.sqlite`. This database is a
separate landing lane, not `pit.duckdb`. Repeated imports are idempotent.

Failed and empty datasets are retained in quality.json; PARTIAL batches cannot
be reported as full-universe success. Cloud exits nonzero after preserving a
partial batch. Import records quality status; it never promotes data quality.
Quota/rate-limit errors stop a run promptly. Raw data and exception text never
go to public logs. No public upload-artifact or dataset commits are used.

## Verification

`python -m unittest discover -s tests -v`

These tests use synthetic fixtures only. Passing tests or green CI does not prove
vendor accuracy, permissions, a successful collection, or PIT certification.

## Research reports

GitHub cannot read desktop AI conversations. Each AI task must provide a local
Markdown report and explicit sync evidence; use BACKTEST_REPORT_PROTOCOL.md.
Map updates must be serialized by the coordinator, not concurrently regenerated
by individual tasks. That separate report/map pipeline is not implemented by
the daily market collector.
