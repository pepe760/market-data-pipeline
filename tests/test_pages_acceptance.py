import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pages_acceptance as p


COMMIT = "a" * 40
OTHER = "b" * 40


def build(number, status, commit=COMMIT):
    return {"url": f"https://api.github.com/repos/owner/repo/pages/builds/{number}",
            "commit": commit, "status": status,
            "created_at": "2026-11-28T08:00:00Z"}


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class PagesAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.receipt = {"pages_commit": COMMIT, "source_status": "SOURCE_TESTED",
                        "push_status": "PUSHED", "live_status": "NOT_CHECKED"}
        self.saved = []

    def save(self):
        self.saved.append(copy.deepcopy(self.receipt))

    def wait(self, api, runs_api=None, **kwargs):
        def combined_api(path, **options):
            if "/actions/runs?" in path:
                return runs_api(path, **options) if runs_api else {"workflow_runs": []}
            return api(path, **options)

        p.wait_for_build(self.receipt, "owner/repo", self.save, combined_api,
                         timeout=50, appearance_grace=10, poll_interval=5,
                         clock=self.clock, sleep=self.clock.sleep, **kwargs)

    def test_existing_build_means_no_post(self):
        calls = []

        def api(path, method="GET", **kwargs):
            calls.append((path, method))
            return [build(1, "building" if self.clock() < 10 else "built")]

        self.wait(api)
        self.assertEqual(self.receipt["build_status"], "BUILD_PASSED")
        self.assertEqual([method for _, method in calls], ["GET", "GET", "GET"])
        self.assertEqual(self.receipt["fallback_request_status"], "NOT_REQUESTED")

    def test_no_automatic_build_requests_fallback_once_after_saved_intent(self):
        methods = []

        def api(path, method="GET", **kwargs):
            methods.append(method)
            if method == "POST":
                self.assertEqual(self.saved[-1]["fallback_request_status"], "INTENT_RECORDED")
                self.assertEqual(self.clock(), 10)
                return build(2, "queued")
            return [build(2, "built")] if self.clock() >= 20 else []

        self.wait(api)
        self.assertEqual(methods.count("POST"), 1)
        self.assertEqual(self.receipt["build_status"], "BUILD_PASSED")
        self.assertEqual(self.receipt["fallback_request_status"], "REQUESTED_ONCE")

    def test_ambiguous_post_fails_without_retry_even_if_verify_called_again(self):
        methods = []

        def api(path, method="GET", **kwargs):
            methods.append(method)
            if method == "POST":
                raise subprocess.TimeoutExpired("gh", 10)
            return []

        with self.assertRaises(p.AcceptanceError):
            self.wait(api)
        self.assertEqual(self.receipt["fallback_request_status"], "RESPONSE_UNCONFIRMED")
        self.assertEqual(self.receipt["build_status"], "BUILD_REQUEST_UNCONFIRMED")
        with self.assertRaises(p.AcceptanceError):
            self.wait(api)
        self.assertEqual(methods.count("POST"), 1)

    def test_ambiguous_post_can_resolve_by_exact_commit_success_without_retry(self):
        methods = []

        def api(path, method="GET", **kwargs):
            methods.append(method)
            if method == "POST":
                raise subprocess.TimeoutExpired("gh", 10)
            return [build(1, "built")] if self.clock() >= 20 else []

        self.wait(api)
        self.assertEqual(self.receipt["build_status"], "BUILD_PASSED")
        self.assertEqual(self.receipt["fallback_request_status"], "RESPONSE_UNCONFIRMED")
        self.assertEqual(methods.count("POST"), 1)

    def test_exact_dynamic_pages_success_resolves_errored_build_api(self):
        def runs_api(path, **kwargs):
            self.assertIn("head_sha=" + COMMIT, path)
            return {"workflow_runs": [dict(id=2, html_url="https://github.com/owner/repo/actions/runs/2",
                head_sha=COMMIT, head_branch="gh-pages", path=p.PAGES_WORKFLOW_PATH, event="dynamic",
                head_repository={"full_name": "owner/repo"},
                status="completed" if self.clock() >= 15 else "in_progress",
                conclusion="success" if self.clock() >= 15 else None)]}

        self.wait(lambda path, **kwargs: [build(1, "errored")], runs_api)
        self.assertEqual(self.receipt["build_status"], "BUILD_PASSED")
        self.assertEqual(self.receipt["build_candidates"][-1]["source"], "PAGES_WORKFLOW")
        self.assertEqual(self.receipt["fallback_request_status"], "NOT_REQUESTED")

    def test_unrelated_successful_workflows_do_not_pass_or_prevent_fallback(self):
        run = dict(id=2, html_url="https://github.com/owner/repo/actions/runs/2", head_sha=COMMIT,
                   head_branch="gh-pages", path=p.PAGES_WORKFLOW_PATH, event="dynamic",
                   head_repository={"full_name": "owner/repo"},
                   status="completed", conclusion="success")
        invalid_runs = [dict(run, head_sha=OTHER), dict(run, head_branch="main"),
                        dict(run, path=".github/workflows/tests.yml"), dict(run, event="push"),
                        dict(run, head_repository=None),
                        dict(run, head_repository={"full_name": "another/repo"})]
        methods = []

        def api(path, method="GET", **kwargs):
            methods.append(method)
            return {} if method == "POST" else []

        with self.assertRaises(p.AcceptanceError):
            self.wait(api, lambda path, **kwargs: {"workflow_runs": invalid_runs})
        self.assertEqual(self.receipt["build_status"], "BUILD_NOT_OBSERVED")
        self.assertEqual(methods.count("POST"), 1)

    def test_cancelled_peer_does_not_preempt_active_success(self):
        def api(path, **kwargs):
            return [build(2, "building" if self.clock() < 15 else "built"),
                    build(1, "errored")]

        self.wait(api)
        self.assertEqual(self.clock(), 15)
        self.assertEqual(self.receipt["build_status"], "BUILD_PASSED")
        self.assertTrue(self.receipt["build_api_url"].endswith("/2"))
        self.assertEqual(len(self.receipt["build_candidates"]), 2)

    def test_cancelled_peer_gets_time_for_successor_not_yet_visible(self):
        def api(path, **kwargs):
            return [build(1, "cancelled")] + ([build(2, "built")] if self.clock() >= 35 else [])

        self.wait(api)
        self.assertEqual(self.receipt["build_status"], "BUILD_PASSED")
        self.assertEqual(self.clock(), 35)
        self.assertEqual(self.receipt["fallback_request_status"], "NOT_REQUESTED")

    def test_older_matching_success_can_pass_with_latest_matching_error(self):
        self.wait(lambda path, **kwargs: [build(2, "errored"), build(1, "built")])
        self.assertEqual(self.receipt["build_status"], "BUILD_PASSED")

    def test_wrong_commit_success_never_passes(self):
        self.wait_failure(lambda path, **kwargs: [build(3, "built", OTHER), build(1, "building")],
                          "BUILD_TIMEOUT")
        self.assertNotIn("build_verified_at_utc", self.receipt)
        self.assertEqual([b["commit"] for b in self.receipt["build_candidates"]], [COMMIT])

    def wait_failure(self, api, expected, runs_api=None):
        with self.assertRaises(p.AcceptanceError):
            self.wait(api, runs_api)
        self.assertEqual(self.clock(), 50)
        self.assertEqual(self.receipt["build_status"], expected)
        self.assertEqual(self.receipt["live_status"], "NOT_CHECKED")

    def test_all_failed_is_reported_after_full_successor_window(self):
        self.wait_failure(lambda path, **kwargs: [build(1, "errored"), build(2, "cancelled")],
                          "ALL_MATCHING_BUILDS_FAILED")

    def test_active_timeout_does_not_claim_failed_build(self):
        self.wait_failure(lambda path, **kwargs: [build(1, "building")], "BUILD_TIMEOUT")

    def test_response_returning_after_deadline_cannot_pass(self):
        def api(path, **kwargs):
            self.clock.now = 60
            return [build(1, "built")]

        with self.assertRaises(p.AcceptanceError):
            self.wait(api)
        self.assertEqual(self.receipt["build_status"], "BUILD_TIMEOUT")
        self.assertNotIn("build_verified_at_utc", self.receipt)

    def test_no_candidate_after_one_fallback_is_reported(self):
        methods = []

        def api(path, method="GET", **kwargs):
            methods.append(method)
            return {} if method == "POST" else []

        self.wait_failure(api, "BUILD_NOT_OBSERVED")
        self.assertEqual(methods.count("POST"), 1)

    def test_unobservable_builds_never_cause_fallback_post(self):
        calls = []

        def api(path, method="GET", **kwargs):
            calls.append(method)
            raise p.AcceptanceError("unavailable")

        self.wait_failure(api, "BUILD_OBSERVATION_FAILED", api)
        self.assertNotIn("POST", calls)
        self.assertGreater(self.receipt["build_poll_errors"], 0)

    def test_build_pass_is_separate_from_live_hash_mismatch(self):
        self.receipt.update(build_status="BUILD_PASSED", public_file_sha256={
            "index.html": hashlib.sha256(b"reviewed").hexdigest()})
        with self.assertRaises(p.AcceptanceError):
            p.verify_live(self.receipt, "https://example.test/", self.save,
                          fetch=lambda url, timeout: b"stale", timeout=15, poll_interval=5,
                          clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(self.receipt["build_status"], "BUILD_PASSED")
        self.assertEqual(self.receipt["live_status"], "LIVE_VERIFICATION_FAILED")
        self.assertEqual(self.receipt["live_file_checks"][0]["status"], "HASH_MISMATCH")

    def test_live_readback_retries_until_every_expected_hash_matches(self):
        files = {"index.html": b"reviewed", "data/latest.json": b"public"}
        self.receipt.update(build_status="BUILD_PASSED", public_file_sha256={
            key: hashlib.sha256(value).hexdigest() for key, value in files.items()})

        def fetch(url, timeout):
            self.assertIn("terminal_commit=" + COMMIT, url)
            key = url.split("https://example.test/", 1)[1].split("?", 1)[0]
            return files[key] if self.clock() >= 5 else b"stale"

        p.verify_live(self.receipt, "https://example.test/", self.save, fetch=fetch,
                      timeout=15, poll_interval=5, clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(self.receipt["live_status"], "LIVE_HASH_VERIFIED")
        self.assertEqual(self.clock(), 5)

    def test_prepare_receipt_captures_only_reviewed_public_files(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory)
            for name in p.PUBLIC_FILES:
                path = site / name
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps({"generated_at": "today", "coverage": {"fresh": 1}}))
            (site / "unrelated.txt").write_text("not part of public acceptance")
            receipt = p.prepare_receipt(site, OTHER, "123")
            self.assertEqual(set(receipt["public_file_sha256"]), set(p.PUBLIC_FILES))
            self.assertEqual(receipt["source_status"], "SOURCE_TESTED")
            self.assertEqual(receipt["build_status"], "NOT_CHECKED")
            self.assertEqual(receipt["live_status"], "NOT_CHECKED")
            output = site / "receipt.json"
            p.save_receipt(output, receipt)
            self.assertEqual(json.loads(output.read_text()), receipt)

    def test_cli_failed_push_preserves_separate_stage_receipt_and_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            for name in p.PUBLIC_FILES:
                path = site / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"generated_at": "today", "coverage": {"fresh": 1}}))
            output = Path(directory) / "receipt.json"
            summary = Path(directory) / "summary.md"
            env = dict(os.environ, GITHUB_SHA=OTHER, GITHUB_RUN_ID="123", GITHUB_STEP_SUMMARY=str(summary))
            command = [sys.executable, str(Path(p.__file__).resolve())]
            subprocess.run(command + ["prepare", "--site-dir", str(site), "--receipt", str(output)],
                           env=env, check=True, capture_output=True)
            subprocess.run(command + ["record-push", "--status", "PUSH_FAILED", "--receipt", str(output)],
                           env=env, check=True, capture_output=True)
            receipt = json.loads(output.read_text())
            self.assertEqual(receipt["source_status"], "SOURCE_TESTED")
            self.assertEqual(receipt["push_status"], "PUSH_FAILED")
            self.assertEqual(receipt["build_status"], "NOT_CHECKED")
            self.assertEqual(receipt["live_status"], "NOT_CHECKED")
            self.assertIn("PUSH: PUSH_FAILED", summary.read_text())


if __name__ == "__main__":
    unittest.main()
