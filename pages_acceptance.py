"""Bounded exact-commit Pages acceptance; receipts contain public evidence only."""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request


PUBLIC_FILES = ("index.html", "app.js", "style.css", "data/latest.json",
                "data/context.json", "data/xly_ab.json")
FAILED_BUILD_STATUSES = {"errored", "cancelled", "canceled", "failure", "failed",
                         "timed_out", "action_required", "stale", "skipped", "neutral"}
PAGES_WORKFLOW_PATH = "dynamic/pages/pages-build-deployment"


class AcceptanceError(RuntimeError):
    pass


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save_receipt(path, receipt):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def prepare_receipt(site_dir, source_commit, workflow_run):
    site = Path(site_dir)
    latest = json.loads((site / "data/latest.json").read_text())
    hashes = {name: hashlib.sha256((site / name).read_bytes()).hexdigest()
              for name in PUBLIC_FILES}
    return {
        "schema_version": 2, "source_commit": source_commit,
        "workflow_run": workflow_run, "pages_commit": None,
        "source_status": "SOURCE_TESTED", "push_status": "NOT_ATTEMPTED",
        "build_status": "NOT_CHECKED", "live_status": "NOT_CHECKED",
        "data_sha256": hashes["data/latest.json"], "public_file_sha256": hashes,
        "generated_at": latest["generated_at"], "coverage": latest["coverage"],
        "prepared_at_utc": utc_now(),
    }


def github_api(path, method="GET", timeout=35):
    # Never copy gh stdout/stderr into public logs or the acceptance artifact.
    result = subprocess.run(["gh", "api", "--method", method, path],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise AcceptanceError("GitHub Pages API request failed")
    return json.loads(result.stdout)


def wait_for_build(receipt, repository, save, api=github_api, *,
                   timeout=600, appearance_grace=45, poll_interval=10,
                   clock=time.monotonic, sleep=time.sleep):
    """Accept any successful exact-commit build, never just the latest build.

    A workflow-token push need not trigger Pages. Observe first, then issue at
    most one fallback POST if a successful list response still has no candidate.
    An uncertain POST is not retried. Failed peers get the full window to allow
    an already queued successor to become visible and finish.
    """
    if timeout <= 0 or poll_interval <= 0 or appearance_grace < 0:
        raise ValueError("Polling bounds must be positive")
    desired = receipt.get("pages_commit")
    if not desired:
        raise ValueError("An exact Pages commit is required")
    previous_request = receipt.get("fallback_request_status", "NOT_REQUESTED")
    if previous_request == "INTENT_RECORDED":
        previous_request = "RESPONSE_UNCONFIRMED"
    started = clock()
    deadline = started + timeout
    candidates = {}
    successful_polls = 0
    receipt.update(build_status="BUILD_PENDING", build_candidates=[],
                   fallback_request_status=previous_request, build_poll_errors=0)
    save()

    def observe(builds, source="PAGES_BUILD"):
        for build in builds:
            if not isinstance(build, dict) or build.get("commit") != desired:
                continue
            # The API URL identifies a build; omit unrelated API fields.
            key = source + ":" + str(build.get("url") or build.get("id") or "unidentified")
            candidates[key] = dict({field: build.get(field) for field in
                                  ("url", "commit", "status", "created_at", "updated_at")},
                                  source=source)
        receipt["build_candidates"] = list(candidates.values())
        for build in candidates.values():
            if build["status"] == "built" and clock() < deadline:
                receipt.update(build_status="BUILD_PASSED", build_api_url=build["url"],
                               build_verified_at_utc=utc_now())
                save()
                return True
        return False

    while clock() < deadline:
        observed = 0
        try:
            builds = api(f"repos/{repository}/pages/builds?per_page=100",
                         timeout=min(35, max(0.1, deadline - clock())))
            if not isinstance(builds, list):
                raise AcceptanceError("Unexpected Pages build list")
            observed += 1
            if observe(builds):
                return
        except Exception as exc:
            receipt["build_poll_errors"] += 1
            receipt["last_build_poll_error"] = type(exc).__name__
        if clock() < deadline:
            try:
                runs = api(f"repos/{repository}/actions/runs?branch=gh-pages&head_sha={desired}&per_page=100",
                           timeout=min(35, max(0.1, deadline - clock())))
                if not isinstance(runs, dict) or not isinstance(runs.get("workflow_runs"), list):
                    raise AcceptanceError("Unexpected Pages workflow list")
                observed += 1
                builds = []
                for run in runs["workflow_runs"]:
                    if (not isinstance(run, dict) or run.get("head_sha") != desired or
                            run.get("head_branch") != "gh-pages" or
                            run.get("path") != PAGES_WORKFLOW_PATH or run.get("event") != "dynamic"):
                        continue
                    head_repository = run.get("head_repository")
                    if (not isinstance(head_repository, dict) or
                            head_repository.get("full_name") != repository):
                        continue
                    status = run.get("status")
                    if status == "completed":
                        status = "built" if run.get("conclusion") == "success" else run.get("conclusion")
                    builds.append(dict(url=run.get("html_url"), id=run.get("id"), commit=run["head_sha"],
                                       status=status, created_at=run.get("created_at"), updated_at=run.get("updated_at")))
                if observe(builds, "PAGES_WORKFLOW"):
                    return
            except Exception as exc:
                receipt["build_poll_errors"] += 1
                receipt["last_build_poll_error"] = type(exc).__name__
        successful_polls += observed > 0
        save()
        if (observed == 2 and not candidates and
                receipt["fallback_request_status"] == "NOT_REQUESTED" and
                clock() - started >= appearance_grace and clock() < deadline):
            # Persist intent before the mutation. A network error can mean that
            # GitHub accepted the request even though its response was lost.
            receipt["fallback_request_status"] = "INTENT_RECORDED"
            receipt["fallback_requested_at_utc"] = utc_now()
            save()
            try:
                build = api(f"repos/{repository}/pages/builds", method="POST",
                            timeout=min(35, max(0.1, deadline - clock())))
            except Exception as exc:
                receipt.update(fallback_request_status="RESPONSE_UNCONFIRMED",
                               fallback_response_error=type(exc).__name__)
                save()
                # It may have been accepted. Continue GET-only observation;
                # exact-commit success can resolve this without a second POST.
            else:
                receipt["fallback_request_status"] = "REQUESTED_ONCE"
                if isinstance(build, dict) and observe([build]):
                    return
            save()
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(poll_interval, remaining))

    if candidates and all(b["status"] in FAILED_BUILD_STATUSES for b in candidates.values()):
        receipt["build_status"] = "ALL_MATCHING_BUILDS_FAILED"
    elif not successful_polls:
        receipt["build_status"] = "BUILD_OBSERVATION_FAILED"
    elif not candidates and receipt["fallback_request_status"] == "RESPONSE_UNCONFIRMED":
        receipt["build_status"] = "BUILD_REQUEST_UNCONFIRMED"
    elif not candidates:
        receipt["build_status"] = "BUILD_NOT_OBSERVED"
    else:
        receipt["build_status"] = "BUILD_TIMEOUT"
    receipt["failure"] = receipt["build_status"]
    save()
    raise AcceptanceError("Exact-commit Pages success not confirmed within polling window")


def read_public_file(url, timeout):
    request = urllib.request.Request(url, headers={
        "User-Agent": "market-terminal-acceptance/1", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise AcceptanceError("Public file did not return HTTP 200")
        return response.read()


def verify_live(receipt, base_url, save, fetch=read_public_file, *, timeout=120,
                poll_interval=10, clock=time.monotonic, sleep=time.sleep):
    """Verify deployed bytes separately; this does not certify browser behavior."""
    if receipt.get("build_status") != "BUILD_PASSED":
        raise ValueError("An exact-commit successful build is required before live checks")
    if timeout <= 0 or poll_interval <= 0:
        raise ValueError("Polling bounds must be positive")
    deadline = clock() + timeout
    receipt["live_status"] = "LIVE_PENDING"
    save()
    while clock() < deadline:
        checks = []
        for name, expected in receipt["public_file_sha256"].items():
            if clock() >= deadline:
                break
            row = {"path": name, "expected_sha256": expected}
            url = base_url.rstrip("/") + "/" + name + "?terminal_commit=" + receipt["pages_commit"]
            try:
                actual = hashlib.sha256(fetch(url, min(20, max(0.1, deadline - clock())))).hexdigest()
                row.update(observed_sha256=actual,
                           status="MATCH" if actual == expected else "HASH_MISMATCH")
            except Exception as exc:
                row.update(status="FETCH_FAILED", error_type=type(exc).__name__)
            checks.append(row)
        receipt["live_file_checks"] = checks
        if (clock() < deadline and len(checks) == len(receipt["public_file_sha256"]) and
                all(r["status"] == "MATCH" for r in checks)):
            receipt.update(live_status="LIVE_HASH_VERIFIED", live_verified_at_utc=utc_now(),
                           verified_at_utc=utc_now())
            save()
            return
        save()
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(poll_interval, remaining))
    receipt.update(live_status="LIVE_VERIFICATION_FAILED", failure="LIVE_VERIFICATION_FAILED")
    save()
    raise AcceptanceError("Public file hashes not confirmed within polling window")


def write_summary(receipt):
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as handle:
            handle.write("### Terminal Pages acceptance\n\n")
            for stage in ("source", "push", "build", "live"):
                handle.write(f"- {stage.upper()}: {receipt.get(stage + '_status', 'NOT_CHECKED')}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "record-push", "verify"))
    parser.add_argument("--receipt", default="TERMINAL_CLOUD_ACCEPTANCE.json")
    parser.add_argument("--site-dir", default="_site")
    parser.add_argument("--pages-commit")
    parser.add_argument("--status", choices=("PUSHED", "UNCHANGED", "PUSH_FAILED"))
    parser.add_argument("--repository", default="pepe760/market-data-pipeline")
    parser.add_argument("--base-url", default="https://pepe760.github.io/market-data-pipeline/")
    args = parser.parse_args()
    if args.command == "prepare":
        receipt = prepare_receipt(args.site_dir, os.environ["GITHUB_SHA"], os.environ["GITHUB_RUN_ID"])
        save_receipt(args.receipt, receipt)
        return
    receipt = json.loads(Path(args.receipt).read_text())
    save = lambda: save_receipt(args.receipt, receipt)
    if args.command == "record-push":
        if not args.status or (args.status != "PUSH_FAILED" and not args.pages_commit):
            parser.error("record-push needs --status and successful pushes need --pages-commit")
        receipt.update(push_status=args.status, push_recorded_at_utc=utc_now())
        if args.pages_commit:
            receipt["pages_commit"] = args.pages_commit
        save()
        if args.status == "PUSH_FAILED":
            write_summary(receipt)
        return
    if receipt.get("push_status") not in {"PUSHED", "UNCHANGED"}:
        raise AcceptanceError("Pages source push has not succeeded")
    try:
        wait_for_build(receipt, args.repository, save)
        verify_live(receipt, args.base_url, save)
    finally:
        write_summary(receipt)


if __name__ == "__main__":
    main()
