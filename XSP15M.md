# XSP 15-minute quick scan

This lane is prepared but disabled. It has no live capture receipt. The
candidate provider is the Tradier Sandbox market-data API. Confirm that the
account can actually return XSP, and that its terms permit unattended
collection from Actions and retention in the owner's private Google Drive.
Sandbox quotes carry Tradier's stated 15-minute delay. Exact XSP sandbox
entitlement and archive rights remain unverified.

The workflow polls every 15 minutes at 09:47 through 16:02 New York time. The
target labels are 09:30 through 15:45. The NYSE session calendar gates U.S.
cash sessions and early closes, rather than covering the entire Cboe XSP
session. Holidays and out-of-session jobs make no market request. GitHub
schedule delays can produce gaps or more than one capture for a bucket.
The bucket uses the actual pre-request observation clock less the nominal
delay. It is a coverage label, never the quote timestamp. No historical
scan is backfilled.

Each scan requests the actual listed XSP chains for the current and next
trading-session expiry, both calls and puts across returned strikes, and
the genuine XSP index quote when provided. Missing expiries stay visible.
No substitute root or maturity is used. Bid, ask, sizes, last, volume, open
interest, and vendor Greeks are retained when present. The vendor bid, ask,
last-trade and Greeks clocks stay separate. The unknown Greek model clock
and spot are null. Clock quality measures the underlying and side clocks
relative to the target within one interval, and does not establish simultaneous
multi-leg quotes or executable fills.

## Activation

1. Obtain a Tradier **sandbox** credential with XSP access and confirm the
   retrieval, private retention and research-display permissions. Do not use
   the live brokerage token or put a token in repository files, logs or chat.
2. Create a GitHub environment named `xsp-sandbox` restricted to `main`. Put
   the sandbox credential in its `XSP15M_TRADIER_SANDBOX_TOKEN` secret.
3. During the stated window, run the manual workflow on `main`. It prints
   a short coverage receipt and stores no snapshots. Check real XSP identity,
   both expected expiries, quotes, provider timestamps and underlying access.
   A no-quotes or partial probe requires review before enabling. A probe
   outside the observation window reports `SKIP_OUTSIDE_RESEARCH_WINDOW`.
4. In the existing private Market Data Pipeline Drive root, create
   `xsp15m/batches`. Each scheduled capture checks the root and both
   destination folders are owner-only. Transfers bind to the verified
   batch parent folder ID and verify the new files' permissions and
   hashes before and after publishing the commit marker.
   The existing Drive credentials are used only for scheduled delivery.
5. Set the **repository** variables `XSP15M_STORAGE_AUTHORIZED=true` and
   `XSP15M_ENABLED=true` only after rights and probe verification. Both
   must be true. Confirm a scheduled
   run's uploaded `quality.json`, `manifest.json` and `COMMITTED.json`
   against a private download. A partial scan retains its evidence and
   exits 2.

For local verification with the existing scoped rclone configuration:

```sh
LOCAL_RCLONE_REMOTE=gdrive_market LOCAL_DRIVE_FOLDER_ID=<existing-private-root-id> \
  python3 xsp15m.py sync --root ~/projects/data/xsp15m
python3 xsp15m.py inventory --root ~/projects/data/xsp15m --session YYYY-MM-DD
```

Read committed batches only. `snapshot.ndjson.gz` is a private XSP Option
Observatory v1 packet with `source_kind=DELAYED_BBO`. It can be imported
through the Observatory's existing local, entitlement-gated ingestion. A
batch with no quotes records quality and hashes only. Each private batch
carries the capture time, the target, response hashes, request clocks,
coverage and rejection counts. Raw response bytes and prices are omitted
from public logs. The manual probe and CI never write Drive.

`inventory` returns a JSON grid with actual verified attempts and distinct
`NOT_DUE`, `AWAITING_CAPTURE`, `MISSING_CAPTURE`, `NO_QUOTES`,
`DATA_PARTIAL` and `CAPTURED_DELAYED` statuses. Use that read-only output in
the local data website once the lane is activated. The inventory considers
unobserved slots missing from the local copy after an eight-minute grace
period. The slot status does not prove that GitHub did or did not fire.
To import a single verified, nonempty batch into the existing Observatory
when permitted, set `OBSERVATORY_DIR` and `XSP_VERIFIED_BATCH_DIR` to the
local checkout and the selected committed folder, respectively.

```sh
set -o pipefail
gzip -dc "$XSP_VERIFIED_BATCH_DIR/snapshot.ndjson.gz" |
  python3 "$OBSERVATORY_DIR/capture_feed.py" handoff - |
  python3 "$OBSERVATORY_DIR/observe.py" ingest \
    --db ~/projects/data/xsp-option-observatory/observations.sqlite3 \
    --confirm-storage-rights
```

## Provider references

- [Tradier chains and clock fields](https://docs.tradier.com/market-data/get-options-chains)
- [Tradier sandbox delay and personal access](https://docs.tradier.com/overview/faq)
- [Cboe XSP contract and session specifications](https://www.cboe.com/tradable-products/sp-500/xsp-options/specifications/)
- [Cboe restriction on automating its quote dashboard](https://www.cboe.com/delayed_quotes/api/quote_table)
- [GitHub timezone-aware scheduling and delays](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
