# RSR operational pilot / sole maintenance entrance

Pilot: 2026-10-09 through 2026-10-22 Hong Kong; final review October 23. William authorizes monitoring, receipts and alerts. This is operational maintenance, not a new strategy study. No changes to trades, research verdicts, other app strategies or production deployments are authorized by this monitor. Evidence-based diagnosis is allowed; consequential repairs are proposed with their exact scope.

## Completion vocabulary (all future RSR handoffs)

- SOURCE_TESTED: source changed and relevant checks passed.
- PUSHED: remote commit read back; not yet build/deploy proof.
- BUILD_PASSED: exact GitHub run succeeded.
- DEPLOY_PASSED: matching Cloudflare Pages check succeeded.
- LIVE_CONTENT_VERIFIED: actual protected live content authenticated and checked. Never infer this from earlier stages.
- DATA_PARTIAL: transfer/import may be verified while source coverage is incomplete. Give counts and concrete missing cases.
- BLOCKED: state exact blocker, preserved progress and next step. Do not silently call partial work complete.

User decides research priorities, sufficiency of investment evidence and publication. Watchdog owns daily operational checks. Local evidence: this file plus dated private receipts. Other AI should read this entrance and CODEX.md, not whole chat histories; do not create another writer or duplicate monitoring schedule.

## Checks / notification policy

GitHub workflow rsr_watchdog.yml in market-data-pipeline runs HK12:17 on the dated October9–23 window. HK Sunday/Monday have no new US close and skip. Daily post-close build must be successful after scheduled21:13UTC; the12:17HK check provides about7h queue/runtime allowance. Check repository HTML clock and three30-row ranking groups, current commit Cloudflare Pages check, and live endpoint status. Secret RSR_READ_TOKEN is read-only in this code; Drive credentials save only private operational receipts. No paid ranking rows are printed publicly.

Private Drive path: existing market-data folder / monitoring/rsr_pilot_20261009/{latest.json,state.json,runs/}. Concurrency enforces one state writer. Readback hash verifies receipts. New/changed incidents trigger failure once; identical incidents remain recorded without repeated alerts. Recovery is recorded. Transport/auth failure is a real monitoring outage and fails loudly. Final report collects daily states and incidents; workflow disables itself after successful October23 summary.

Codex follow-up reads bounded latest status after the GitHub check, deduplicates incident signature locally, and notifies only on new/worsened incidents, recovery requiring attention, or final review. If Mac/Codex is offline, GitHub checks and incident failure notices still run; in-chat follow-up waits until host is available. No claim that a local schedule is always-on.

Protected live website returns401 without authentication. This is an expected gate response, not content verification. Watchdog explicitly records live_authenticated_content_verified=false. Authenticated browser visual QA remains outside automatic proof until a supported authenticated route is provided.

## End review

Evaluate checked days, incidents, recovery, false positives, missed failures and manual time spent. The watchdog records the first three; William supplies false-positive/missed-event/manual-time observations. No automatic expansion to other apps or continued monitoring beyond the dated pilot. Failure-free checks alone do not establish strategy performance or source accuracy.
