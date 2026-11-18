import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

import exchange_calendars as xc

import xsp15m as xsp


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


class Source:
    def __init__(self, observed, replies):
        self.observed = observed
        self.replies = replies

    def get(self, endpoint, **params):
        reply = self.replies[endpoint, params.get("expiration")]
        if isinstance(reply, Exception):
            raise reply
        return reply, {"request": endpoint, "received_at_utc": self.observed.isoformat()}


def contract(expiry, right, quote_time, **overrides):
    return {
        "symbol": f"XSP{expiry[2:].replace('-', '')}{right}00600000",
        "expiration_date": expiry, "root_symbol": "XSP", "underlying": "XSP",
        "strike": 600, "option_type": "call" if right == "C" else "put",
        "contract_size": 100, "bid": 1.23, "ask": 1.35,
        "bid_date": int(quote_time.timestamp() * 1000), "ask_date": int(quote_time.timestamp()),
        "bidsize": 2, "asksize": 3, "trade_date": int(quote_time.timestamp()) - 120,
        "greeks": {"delta": 0.15 if right == "C" else -0.15, "updated_at": "2026-11-05 10:00:00"},
        **overrides,
    }


class XspTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = xc.get_calendar("XNYS")

    def test_market_slot_handles_dst_early_close_holidays_and_friday(self):
        slot, expiries, _, _ = xsp.market_slot(utc("2026-11-06T14:47:00"), self.calendar)
        self.assertEqual(slot, utc("2026-11-06T14:30:00"))
        self.assertEqual(expiries, ("2026-11-06", "2026-11-09"))
        slot, _, _, _ = xsp.market_slot(utc("2026-07-08T13:47:00"), self.calendar)
        self.assertEqual(slot, utc("2026-07-08T13:30:00"))
        self.assertIsNone(xsp.market_slot(utc("2026-11-26T15:02:00"), self.calendar))
        self.assertIsNotNone(xsp.market_slot(utc("2026-11-27T18:02:00"), self.calendar))
        self.assertIsNone(xsp.market_slot(utc("2026-11-27T18:17:00"), self.calendar))
        self.assertIsNone(xsp.market_slot(utc("2026-11-06T14:32:00"), self.calendar))

    def test_clocks_and_identity_are_not_inferred(self):
        received = utc("2026-11-05T15:17:00")
        quoted = received - timedelta(minutes=15)
        row = contract("2026-11-05", "P", quoted, ask_date=int(quoted.timestamp()) + 1)
        item = xsp.option_row(row, "2026-11-05", received)
        self.assertEqual(item["bid_ts"], quoted.isoformat())
        self.assertEqual(item["ask_ts"], (quoted + timedelta(seconds=1)).isoformat())
        self.assertIsNone(item["quote_ts"])
        self.assertNotEqual(item["trade_ts"], item["bid_ts"])
        self.assertIsNone(item["greeks_ts"])
        self.assertIsNone(item["standard_contract"])
        with self.assertRaises(xsp.CaptureError):
            xsp.option_row(dict(row, underlying="SPX"), "2026-11-05", received)
        with self.assertRaises(xsp.CaptureError):
            xsp.option_row(dict(row, contract_size=10), "2026-11-05", received)
        future = contract("2026-11-05", "P", received + timedelta(minutes=1))
        self.assertIsNone(xsp.option_row(future, "2026-11-05", received)["bid_ts"])

    def test_delayed_batch_and_missing_next_expiry(self):
        observed = utc("2026-11-05T15:17:00")
        session = xsp.market_slot(observed, self.calendar)
        first_expiry, next_expiry = session[1]
        replies = {
            ("expirations", None): {"expirations": {"date": [first_expiry, next_expiry]}},
            ("underlying", None): {"quotes": {"quote": {
                "symbol": "XSP", "type": "index", "last": 605, "trade_date": int(session[0].timestamp())
            }}},
            **{("chain", expiry): {"options": {"option": [
                contract(expiry, right, session[0]) for right in ("C", "P")
            ]}} for expiry in session[1]}
        }
        source = Source(observed, replies)
        with patch.object(xsp.pipeline, "now", return_value=observed):
            packet, quality = xsp.collect(source, *session)
            self.assertEqual(quality["status"], "CAPTURED_DELAYED")
            self.assertEqual(quality["rows"], 4)
            self.assertFalse(quality["exact_asof_verified"])
            self.assertEqual(packet["source_kind"], "DELAYED_BBO")
            self.assertEqual(packet["captured_at_utc"], observed.isoformat())
            self.assertEqual(packet["target_slot_utc"], session[0].isoformat())
            replies["expirations", None] = {"expirations": {"date": [first_expiry]}}
            _, quality = xsp.collect(source, *session)
            self.assertEqual(quality["status"], "DATA_PARTIAL")
            self.assertEqual(quality["checks"][1]["status"], "EXPIRY_NOT_LISTED")

    def test_stale_option_clock_is_not_coverage(self):
        received = utc("2026-11-05T20:02:00")
        slot, _, first, close = xsp.market_slot(received, self.calendar)
        row = xsp.option_row(contract("2026-11-05", "P", first), "2026-11-05", received)
        self.assertEqual(xsp.bbo_status(row, slot, first, close), "STALE_SIDE_CLOCK")

    def test_published_marker_follows_hash_verified_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = root / "20261105T150200Z-ae9b99aa0123"
            archive = root / "drive"
            xsp.stage_batch(batch, {"quotes": [{"symbol": "XSP"}]}, {"rows": 1})
            actions = []
            destination = Mock(batches="VerifiedFolderId")

            def transfer(command, source, dest, *flags):
                destination.folder.assert_called_once_with(destination.batches)
                self.assertEqual(flags[-2:], ("--drive-root-folder-id", destination.batches))
                self.assertNotIn("xsp15m/", str(source) + str(dest))
                actions.append((command, str(source), str(dest)))
                if command == "copy" and source == batch:
                    shutil.copytree(batch, archive, ignore=shutil.ignore_patterns("COMMITTED.json"))
                elif command == "copy":
                    shutil.copytree(archive, dest)
                elif isinstance(source, Path):
                    shutil.copyfile(source, archive / "COMMITTED.json")
                else:
                    shutil.copyfile(archive / "COMMITTED.json", dest)

            with patch.object(xsp.pipeline, "rclone", side_effect=transfer):
                self.assertEqual(xsp.publish(batch, "test:", destination)["rows"], 1)
            self.assertEqual([a[0] for a in actions], ["copy", "copy", "copyto", "copyto"])
            xsp.verify_batch(batch)
            (batch / "quality.json").write_text("{}")
            with self.assertRaises(xsp.CaptureError):
                xsp.verify_batch(batch)

    def test_shared_drive_destination_rejected(self):
        private = {
            "id": "1234567890", "mimeType": xsp.DRIVE_FOLDER, "ownedByMe": True,
            "shared": False, "permissions": [{"type": "user", "role": "owner"}],
        }
        self.assertEqual(xsp.DrivePrivateDestination.check(private, True), private["id"])
        with self.assertRaises(xsp.CaptureError):
            xsp.DrivePrivateDestination.check(dict(private, shared=True), True)
        with self.assertRaises(xsp.CaptureError):
            xsp.DrivePrivateDestination.check(dict(private, permissions=[]), True)

    def test_inventory_distinguishes_verified_gap_from_future(self):
        label = "2026-11-05"
        now = utc("2026-11-05T15:20:00")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "batches").mkdir()
            batch = root / "batches" / "20261105T144700Z-ae9b99aa0123"
            xsp.stage_batch(batch, {"quotes": []}, {
                "rows": 0, "status": "NO_QUOTES", "session_et": label,
                "target_slot_utc": "2026-11-05T14:30:00+00:00",
                "observed_at_utc": "2026-11-05T14:47:00+00:00",
            })
            grid = xsp.inventory(root, label, now, self.calendar)
        self.assertEqual(len(grid["buckets"]), 26)
        self.assertEqual([item["status"] for item in grid["buckets"][:4]],
                         ["NO_QUOTES", "MISSING_CAPTURE", "AWAITING_CAPTURE", "NOT_DUE"])


if __name__ == "__main__":
    unittest.main()
