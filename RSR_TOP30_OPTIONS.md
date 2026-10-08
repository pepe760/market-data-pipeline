# Daily RSR family option snapshots

Implemented dynamic read-only source: commit-pinned private RSR dashboard index.html, parsing POOL_RSR / POOL_NRSR / POOL_URSR (exactly 30 unique tickers each). Retain original dashboard score/order, source commit/hash/build time, capture clock and overlapping group memberships in private quality.json and per-option records. This is current dashboard observation, not historical PIT membership or certified financial eligibility.

Combine the deduplicated three lists with the existing 13 reference symbols. Current read-only test on 2026-10-08: 62 unique Top30 symbols, 75 combined; source build 2026-10-08 08:57:46 Hong Kong. Same DTE targets 7/14/30/45/90/180/365/730; all returned call/put strikes for selected expiries. Existing chain clocks, zero-bid/crossed-market flags and underlying provenance remain.

Activation requires GitHub secret RSR_READ_TOKEN with RSR contents read access, then repository variable RSR_OPTIONS_ENABLED=true. A token choice has been requested from William: reuse his existing pepe760 token (its existing permissions apply) or provide a narrower RSR-only read token. No credential has been copied or created yet; existing daily 13-symbol collection continues while disabled. Dashboard requires login and cannot be fetched anonymously.

23 unit tests pass (one local calendar test skipped for unavailable dependency); real authenticated source parsing verified. Expanded live option batch is not yet run/verified. Use options.yml workflow after activation; preserve private Drive root and immutable hash/readback transport. Never claim all contracts/executable quotes or historical coverage from this collector.
