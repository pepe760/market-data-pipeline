# Broad collection: operations and data coverage

The stock workflow explicitly captures available free-source observations.
Its green result means every configured task was observed under the declared
contract and the private transfer passed full hash readback. It does not mean
all requested stock bars exist, all instruments are validated, or prices are PIT.

`quality.json.status` remains `PARTIAL` for missing/null/unusable rows. The public
warning and job summary show this separately from `capture_status`, with bars,
complete windows, missing/rejected/null/excluded-clock/empty-history counts,
zero-valid-bar symbols and unattempted tasks. Missing and rejected overlap.
Partial tasks never enter the complete checkpoint. Strict CLI remains exit2.

The explicit `observed-source` stock mode can finish capture with data gaps only
for structurally valid observations, null fields, excluded native integer
timestamps that do not fit the requested 15-minute grid, and one constrained
Yahoo native empty-history profile. The latter requires matching symbol,
EQUITY/ETF, 15m, New York timezone, valid range metadata, an observed market
clock, no timestamp array and exactly an empty quote object. Its original strict
parser failure, raw hash and observation time remain recorded. None of these
states proves no trading or delisting; no timestamp is rounded and no price is
filled. Malformed JSON/schema/types, wrong identity, corrupt/nonfinite OHLCV,
duplicates, HTTP failures, exhausted limits and unattempted tasks still fail.
A whole shard with zero accepted or previously complete data also fails.

HTTP429 retries at most twice, respecting bounded Retry-After deadlines. A
deadline over the wait bound is not shortened. HTTP451 stops without a bypass.
Specific reasons and retry delays are retained. Existing checkpoints and raw
batches remain immutable; behavior-bearing lifecycle/capture sources are sealed.

The dated EPICUSDT spot retirement is 2026-10-28 03:00 UTC. That day expects only
00/01/02 hourly starts. Later dates are `RETIRED_NOT_EXPECTED`, produce no bars
or HTTP requests, and do not count toward 300 active targets. Only the specific
terminal 02–03 bucket permits its millisecond-last-tick close label in a
microsecond field, retaining all raw clock integers and an explicit qualification.
This is not general timestamp rounding or historical trade-completeness proof.

Private dated configuration retains retired EPIC history, corrects future EPIC
identity to `epic-chain`, and separately adds verified ACHUSDT / `alchemy-pay` to
maintain 300 active targets. Other 299 public-ID mappings are unchanged and are
not newly exchange-contract-certified. The old stock universe observation date
is retained separately from the configuration revision and new identity clocks.

Old EPIC/`epic-cash` attribution is preserved as invalid source history. The
private qualification sidecar and catalog `asset_identity_policies` table record
the dated rule; those records do not rewrite old prices or intercept arbitrary
SQL/parquet readers. Use `broad_data_access.read_qualified_bars(batch)` for a
fail-closed EPIC identity guard. An explicit `identity_policy=...` plus
`apply_identity_correction=True` makes only an in-memory copy, retaining original
asset ID, price and availability clock, and adding correction availability and
`NOT_HISTORICAL_PIT_IDENTITY`. It does not retroactively certify PIT identity.

These are collection/qualification repairs, with no strategy, broker, trading,
subscription, public raw-data sharing or existing app deployment changes.
