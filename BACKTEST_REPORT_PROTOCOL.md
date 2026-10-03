# Research handoff contract

At completion or material revision of a backtest, create/update BACKTEST_REPORT.md
in that study's existing private directory. No new backtest is required merely
to satisfy reporting. Include:

- Study ID, version, task ID, dated question and preregistration.
- Inputs, vendor, coverage, hashes, universe and available_at/PIT limitations.
- Signal/entry timing, adjustment basis, costs, benchmark and matched control.
- CAGR, daily/monthly MDD distinction, Sharpe, sample dates, CI including zero.
- In/out-of-sample definition, all trials, null/harmful results, audit status.
- Reproduction command, code revision, NAV/trades/output paths and hashes.
- Verdict, known blockers, explicit next step or stop rule.
- Drive folder/file ID, upload version/hash readback; map study/node ID and date.

Use PENDING / VERIFIED / NOT_APPLICABLE for each delivery destination. A report
is not synced just because it exists locally. Do not claim VERIFIED without
readback. Do not upload secrets, broker identities, balances, or licensed datasets
to public GitHub. Private Drive data still requires appropriate usage rights.

Each task stages its report and index entry. One coordinator merges map changes
serially, preserving existing evidence qualifications, audits and frozen forward
records. A newer report does not automatically override a stronger audit.
