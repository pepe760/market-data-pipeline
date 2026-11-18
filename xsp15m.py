"""Delayed XSP chain snapshots for private research, never executable fills."""

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import gzip
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlencode
import uuid
from zoneinfo import ZoneInfo

import pipeline

ET = ZoneInfo("America/New_York")
SYMBOL = "XSP"
SOURCE = "TRADIER_SANDBOX"
SOURCE_KIND = "DELAYED_BBO"
QUOTE_DELAY = timedelta(minutes=15)
OCC = re.compile(r"XSP *(\d{6})([CP])(\d{8})")
PAYLOADS = {"quality.json", "snapshot.ndjson.gz"}
LIMIT = 4 * 1024 * 1024
DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,160}")
DRIVE_FOLDER = "application/vnd.google-apps.folder"
GREEKS = ("delta", "gamma", "theta", "vega", "rho", "phi", "bid_iv", "mid_iv", "ask_iv", "smv_vol")


class CaptureError(ValueError):
    """A bounded collection or delivery failure with no raw provider text."""


def market_slot(observed_at, calendar):
    if observed_at.tzinfo is None:
        raise CaptureError("Expected a time zone for the observation clock")
    observed_at = observed_at.astimezone(timezone.utc)
    local = observed_at.astimezone(ET)
    label = local.date().isoformat()
    if not calendar.is_session(label):
        return None
    slot = observed_at.replace(
        minute=(observed_at.minute // 15) * 15, second=0, microsecond=0
    ) - QUOTE_DELAY
    first = calendar.session_open(label).to_pydatetime()
    close = calendar.session_close(label).to_pydatetime()
    if not first <= slot < close:
        return None
    next_session = calendar.next_session(label).date().isoformat()
    return slot, (label, next_session), first, close


def safe_json(body):
    def unique_keys(pairs):
        row = dict(pairs)
        if len(row) != len(pairs):
            raise CaptureError("Provider returned duplicate JSON fields")
        return row

    try:
        result = json.loads(body, object_pairs_hook=unique_keys)
    except (ValueError, RecursionError):
        raise CaptureError("Invalid provider JSON") from None
    if not isinstance(result, dict):
        raise CaptureError("Unexpected provider response")
    return result


def request_json(host, path, *, headers=None, form=None):
    conn = http.client.HTTPSConnection(host, timeout=10)
    method = "GET" if form is None else "POST"
    try:
        conn.request(method, path, body=form, headers=headers or {})
        response = conn.getresponse()
        if response.status != 200:
            raise CaptureError(f"Provider HTTP {response.status}")
        body = response.read(LIMIT + 1)
    except (OSError, http.client.HTTPException):
        raise CaptureError("Provider connection failed") from None
    finally:
        conn.close()
    if len(body) > LIMIT:
        raise CaptureError("Provider payload exceeded limit")
    return safe_json(body), hashlib.sha256(body).hexdigest()


class TradierSandbox:
    PATHS = {
        "expirations": "/v1/markets/options/expirations",
        "chain": "/v1/markets/options/chains",
        "underlying": "/v1/markets/quotes",
    }

    def __init__(self, token):
        if not token:
            raise CaptureError("Tradier sandbox credential missing")
        self.token = token

    def get(self, endpoint, **params):
        started = pipeline.now()
        payload, digest = request_json(
            "sandbox.tradier.com", self.PATHS[endpoint] + "?" + urlencode(params),
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        )
        return payload, {
            "request": endpoint, "expiration": params.get("expiration"), "sha256": digest,
            "started_at_utc": started.isoformat(), "received_at_utc": pipeline.now().isoformat()
        }


def numeric(value):
    if type(value) not in (int, float):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def epoch_clock(value, received):
    number = numeric(value)
    if number is None or number <= 0:
        return None
    # Tradier docs show epoch seconds. Some payloads use epoch milliseconds.
    if number >= 100_000_000_000:
        number /= 1000
    try:
        observed = datetime.fromtimestamp(number, timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None
    return observed.isoformat() if observed <= received else None


def listed_expirations(payload):
    wrapper = payload.get("expirations")
    if not isinstance(wrapper, dict):
        raise CaptureError("XSP expiration inventory unavailable")
    dates = wrapper.get("date", wrapper.get("expiration"))
    if isinstance(dates, (str, dict)):
        dates = [dates]
    if not isinstance(dates, list) or not dates or len(dates) > 500:
        raise CaptureError("XSP expiration inventory unavailable")
    try:
        parsed = {date.fromisoformat(row["date"] if isinstance(row, dict) else row).isoformat()
                  for row in dates}
    except (TypeError, ValueError, KeyError):
        raise CaptureError("Invalid XSP expiration inventory") from None
    return sorted(parsed)


def option_row(row, expiry, received):
    if not isinstance(row, dict):
        raise CaptureError("Invalid contract object")
    symbol = row.get("symbol")
    match = OCC.fullmatch(symbol) if isinstance(symbol, str) else None
    expiry_digits, side, strike_digits = match.groups() if match else (None, None, None)
    strike = numeric(row.get("strike"))
    if (not match or row.get("root_symbol") != SYMBOL or row.get("underlying") != SYMBOL
            or row.get("expiration_date") != expiry or expiry[2:].replace("-", "") != expiry_digits
            or row.get("option_type") != {"C": "call", "P": "put"}[side]
            or strike is None or strike <= 0 or Decimal(str(strike)) * 1000 != int(strike_digits)):
        raise CaptureError("XSP option identity mismatch")
    multiplier = numeric(row.get("contract_size"))
    if multiplier is not None and multiplier != 100:
        raise CaptureError("XSP multiplier mismatch")
    bid = numeric(row.get("bid"))
    ask = numeric(row.get("ask"))
    bid_time = epoch_clock(row.get("bid_date"), received)
    ask_time = epoch_clock(row.get("ask_date"), received)
    greeks = row.get("greeks")
    greeks = greeks if isinstance(greeks, dict) else {}
    updated = greeks.get("updated_at")
    # Retain the provider's original Greek clock, but never infer its time zone.
    if not isinstance(updated, str) or not re.fullmatch(r"[0-9TtZz .:+-]{1,40}", updated):
        updated = None
    return {
        "symbol": SYMBOL, "contract_symbol": symbol, "trading_class": SYMBOL,
        "expiry": expiry, "right": side, "strike": strike, "bid": bid, "ask": ask,
        "bid_size": numeric(row.get("bidsize")), "ask_size": numeric(row.get("asksize")),
        "bid_ts": bid_time, "ask_ts": ask_time, "quote_ts": bid_time if bid_time == ask_time else None,
        "trade_ts": epoch_clock(row.get("trade_date"), received),
        "multiplier": multiplier, "standard_contract": None,
        "last": numeric(row.get("last")), "volume": numeric(row.get("volume")),
        "open_interest": numeric(row.get("open_interest")),
        "delta": numeric(greeks.get("delta")), "gamma": numeric(greeks.get("gamma")),
        "iv": numeric(greeks.get("mid_iv")), "greeks_ts": None,
        "greeks_source": "VENDOR" if greeks else None,
        "vendor_greeks": {field: numeric(greeks.get(field)) for field in GREEKS},
        "vendor_greeks_updated_at": updated,
        "received_at_utc": received.isoformat(),
    }


def normalize_chain(payload, expiry, received):
    wrapper = payload.get("options")
    raw = wrapper.get("option") if isinstance(wrapper, dict) else None
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or len(raw) > 10_000:
        raise CaptureError("Empty or unsupported XSP chain")
    rows, seen = [], set()
    for item in raw:
        row = option_row(item, expiry, received)
        key = row["strike"], row["right"]
        if key in seen:
            raise CaptureError("Duplicate XSP contract")
        seen.add(key)
        rows.append(row)
    return rows


def underlying_quote(payload, received):
    wrapper = payload.get("quotes")
    row = wrapper.get("quote") if isinstance(wrapper, dict) else None
    if not isinstance(row, dict) or row.get("symbol") != SYMBOL or row.get("type") != "index":
        return None
    price = numeric(row.get("last"))
    if price is None or price <= 0:
        return None
    return {
        "symbol": SYMBOL, "price": price, "quote_ts": epoch_clock(row.get("trade_date"), received),
        "source": SOURCE, "price_kind": "INDEX_LAST"
    }


def bbo_status(row, slot, first, close):
    bid, ask = row["bid"], row["ask"]
    if bid is None or ask is None:
        return "MISSING_SIDE"
    if bid <= 0 or ask <= 0:
        return "ZERO_OR_NEGATIVE_SIDE"
    if bid > ask:
        return "CROSSED"
    if row["bid_ts"] is None or row["ask_ts"] is None:
        return "UNKNOWN_SIDE_CLOCK"
    bid_at, ask_at = (datetime.fromisoformat(row[k]) for k in ("bid_ts", "ask_ts"))
    if not all(first <= event < close for event in (bid_at, ask_at)):
        return "SIDE_CLOCK_OUTSIDE_SESSION"
    if min(bid_at, ask_at) < slot - QUOTE_DELAY:
        return "STALE_SIDE_CLOCK"
    if max(bid_at, ask_at) >= slot + QUOTE_DELAY:
        return "SIDE_CLOCK_AFTER_TARGET_WINDOW"
    return "CLOCKED_TWO_SIDED"


def collect(provider, slot, expiries, first, close):
    requests, checks, quotes = [], [], []
    listed, underlying = [], None
    errors = {}
    try:
        raw, receipt = provider.get("expirations", symbol=SYMBOL, includeAllRoots="false")
        requests.append(receipt)
        listed = listed_expirations(raw)
    except CaptureError as exc:
        errors["expirations"] = str(exc)
    for expiry in expiries:
        check = {"expiry": expiry, "status": "INVENTORY_UNAVAILABLE", "rows": 0, "bbo_counts": {}}
        if listed and expiry not in listed:
            check["status"] = "EXPIRY_NOT_LISTED"
        if expiry in listed:
            try:
                raw, receipt = provider.get("chain", symbol=SYMBOL, expiration=expiry, greeks="true")
                requests.append(receipt)
                received = datetime.fromisoformat(receipt["received_at_utc"])
                rows = normalize_chain(raw, expiry, received)
                for row in rows:
                    row["bbo_status"] = bbo_status(row, slot, first, close)
                counts = Counter(row["bbo_status"] for row in rows)
                rights = {row["right"] for row in rows if row["bbo_status"] == "CLOCKED_TWO_SIDED"}
                check.update(rows=len(rows), bbo_counts=counts,
                             status="CAPTURED" if rights == {"C", "P"} else "CHAIN_WITH_QUOTE_GAPS")
                quotes.extend(rows)
            except CaptureError as exc:
                check.update(status="CHAIN_UNAVAILABLE", error=str(exc))
        checks.append(check)
    if listed:
        try:
            raw, receipt = provider.get("underlying", symbols=SYMBOL)
            requests.append(receipt)
            underlying = underlying_quote(raw, datetime.fromisoformat(receipt["received_at_utc"]))
        except CaptureError as exc:
            errors["underlying"] = str(exc)

    spot_at = datetime.fromisoformat(underlying["quote_ts"]) if underlying and underlying["quote_ts"] else None
    spot_in_window = bool(spot_at and first <= spot_at < close
                          and slot - QUOTE_DELAY <= spot_at < slot + QUOTE_DELAY)
    observed = pipeline.now().isoformat()
    packet = {
        "schema_version": 1, "symbol": SYMBOL, "source": SOURCE, "source_kind": SOURCE_KIND,
        "captured_at_utc": observed, "received_at_utc": observed, "underlying": underlying,
        "target_slot_utc": slot.isoformat(), "expiration_dates": listed,
        "expiry_inventory_complete": bool(listed), "quotes": quotes,
        "requests": requests, "declared_delay_minutes": 15,
    }
    quality = {
        "schema_version": 1, "lane": "xsp15m", "source": SOURCE, "source_kind": SOURCE_KIND,
        "session_et": slot.astimezone(ET).date().isoformat(),
        "target_slot_et": slot.astimezone(ET).isoformat(), "target_slot_utc": slot.isoformat(),
        "observed_at_utc": observed, "declared_delay_minutes": 15, "rows": len(quotes),
        "checks": checks, "errors": errors, "underlying_clocked": spot_at is not None,
        "underlying_clock_in_window": spot_in_window,
        "status": "CAPTURED_DELAYED" if quotes and all(c["status"] == "CAPTURED" for c in checks)
                  and spot_in_window else "DATA_PARTIAL" if quotes else "NO_QUOTES",
        "research_gate": "DELAYED_OBSERVATION_ONLY", "exact_asof_verified": False,
    }
    return packet, quality


def stage_batch(folder, packet, quality):
    folder.mkdir(mode=0o700)
    if packet["quotes"]:
        content = json.dumps(packet, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(content) > 16 * 1024 * 1024:
            raise CaptureError("Normalized snapshot exceeded archive packet limit")
        (folder / "snapshot.ndjson.gz").write_bytes(gzip.compress(content, mtime=0))
    pipeline.write_json(folder / "quality.json", quality)
    pipeline.write_json(folder / "manifest.json", {
        "schema_version": 1, "lane": "xsp15m", "run_id": folder.name,
        "source_revision": os.getenv("GITHUB_SHA"),
        "files": {p.name: pipeline.digest(p) for p in folder.iterdir()},
    })
    pipeline.write_json(folder / "COMMITTED.json", {
        "manifest_sha256": pipeline.digest(folder / "manifest.json")
    })


def verify_batch(folder):
    if (not pipeline.RUN.fullmatch(folder.name) or folder.is_symlink()
            or any(p.is_symlink() or not p.is_file() for p in folder.iterdir())):
        raise CaptureError("XSP batch has unsafe path entries")
    manifest = json.loads((folder / "manifest.json").read_bytes())
    marker = json.loads((folder / "COMMITTED.json").read_bytes())
    files = manifest["files"]
    if (manifest["run_id"] != folder.name
            or manifest["lane"] != "xsp15m" or manifest["schema_version"] != 1
            or set(files) not in (PAYLOADS, {"quality.json"})
            or {p.name for p in folder.iterdir()} != set(files) | {"manifest.json", "COMMITTED.json"}
            or marker["manifest_sha256"] != pipeline.digest(folder / "manifest.json")
            or any(pipeline.digest(folder / name) != digest for name, digest in files.items())):
        raise CaptureError("XSP batch integrity check failed")
    quality = json.loads((folder / "quality.json").read_bytes())
    if ("snapshot.ndjson.gz" in files) != (quality["rows"] > 0):
        raise CaptureError("XSP batch row count conflicts with its payload")
    return quality


class DrivePrivateDestination:
    def __init__(self):
        try:
            token = json.loads(os.environ["DRIVE_TOKEN"])
            credentials = urlencode({
                "client_id": os.environ["DRIVE_CLIENT_ID"],
                "client_secret": os.environ["DRIVE_CLIENT_SECRET"],
                "refresh_token": token["refresh_token"], "grant_type": "refresh_token"
            })
            self.root = os.environ["DRIVE_FOLDER_ID"]
        except (KeyError, TypeError, ValueError):
            raise CaptureError("Private Drive credentials missing") from None
        payload, _ = request_json("oauth2.googleapis.com", "/token", form=credentials,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            raise CaptureError("Private Drive access token missing")
        self.headers = {"Authorization": f"Bearer {access}"}
        self.folder(self.root)
        xsp = self.child(self.root, "xsp15m", folder=True)
        self.batches = self.child(xsp, "batches", folder=True)

    def get(self, path):
        result, _ = request_json("www.googleapis.com", "/drive/v3/files" + path, headers=self.headers)
        return result

    @staticmethod
    def check(row, folder):
        permissions = row.get("permissions")
        mime_type = row.get("mimeType")
        if (row.get("ownedByMe") is not True or row.get("shared") is not False
                or not isinstance(permissions, list) or len(permissions) != 1
                or not isinstance(permissions[0], dict)
                or permissions[0].get("type") != "user" or permissions[0].get("role") != "owner"
                or not isinstance(mime_type, str)
                or (mime_type == DRIVE_FOLDER) != folder
                or not isinstance(row.get("id"), str) or not DRIVE_ID.fullmatch(row["id"])):
            raise CaptureError("Drive ownership or sharing not established")
        return row["id"]

    def folder(self, identifier):
        if not DRIVE_ID.fullmatch(identifier):
            raise CaptureError("Invalid Drive folder identifier")
        fields = "id,mimeType,ownedByMe,shared,permissions(type,role)"
        result = self.check(self.get("/" + identifier + "?" + urlencode({"fields": fields})), True)
        if result != identifier:
            raise CaptureError("Drive folder identity changed")
        return result

    def child(self, parent, name, *, folder=False):
        if not DRIVE_ID.fullmatch(parent) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise CaptureError("Invalid Drive child lookup")
        query = urlencode({
            "q": f"'{parent}' in parents and trashed = false and name = '{name}'",
            "fields": "nextPageToken,files(id,mimeType,ownedByMe,shared,permissions(type,role))"
        })
        result = self.get("?" + query)
        rows = result.get("files")
        if not isinstance(rows, list) or len(rows) != 1 or result.get("nextPageToken"):
            raise CaptureError("Private Drive path missing or ambiguous")
        return self.check(rows[0], folder)


def publish(folder, target, destination):
    verify_batch(folder)
    path = target + folder.name
    # Never resolve the destination by its potentially ambiguous folder name.
    # The upload and both readbacks are rooted at the verified folder ID.
    scope = ("--drive-root-folder-id", destination.batches)
    destination.folder(destination.batches)
    pipeline.rclone("copy", folder, path, "--exclude", "COMMITTED.json", "--immutable", *scope)
    batch_id = destination.child(destination.batches, folder.name, folder=True)
    for item in folder.iterdir():
        if item.name != "COMMITTED.json":
            destination.child(batch_id, item.name)
    with tempfile.TemporaryDirectory() as tmp:
        back = Path(tmp) / folder.name
        pipeline.rclone("copy", path, back, *scope)
        (back / "COMMITTED.json").write_bytes((folder / "COMMITTED.json").read_bytes())
        verify_batch(back)
        pipeline.rclone("copyto", folder / "COMMITTED.json", path + "/COMMITTED.json", "--immutable", *scope)
        pipeline.rclone("copyto", path + "/COMMITTED.json", back / "COMMITTED.json", *scope)
        destination.child(batch_id, "COMMITTED.json")
        return verify_batch(back)


def sync(root):
    import fcntl
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / ".sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        target = pipeline.remote(local=True)
        batches = root / "batches"
        pipeline.rclone("copy", target + "xsp15m/batches", batches, "--immutable")
        verified, incomplete = 0, 0
        for batch in batches.iterdir():
            if batch.is_symlink() or not batch.is_dir() or not pipeline.RUN.fullmatch(batch.name):
                raise CaptureError("Unexpected XSP archive entry")
            if not (batch / "COMMITTED.json").exists():
                incomplete += 1
                continue
            verify_batch(batch)
            verified += 1
        print(json.dumps({"verified_batches": verified, "pending_uncommitted": incomplete}))


def inventory(root, label, now, calendar):
    """Return the expected quarter-hour grid and verified local attempts."""
    market_open = calendar.is_session(label)
    attempts = {}
    date_prefix = label.replace("-", "")
    for folder in sorted((root / "batches").glob(date_prefix + "T*")):
        if not (folder / "COMMITTED.json").is_file():
            continue
        quality = verify_batch(folder)
        if quality["session_et"] == label:
            attempts.setdefault(quality["target_slot_utc"], []).append({
                "batch": folder.name, "observed_at_utc": quality["observed_at_utc"],
                "status": quality["status"], "rows": quality["rows"]
            })
    buckets = []
    if market_open:
        slot = calendar.session_open(label).to_pydatetime()
        close = calendar.session_close(label).to_pydatetime()
        while slot < close:
            expected_poll = slot + timedelta(minutes=17)
            observations = attempts.get(slot.isoformat(), [])
            if any(r["status"] == "CAPTURED_DELAYED" for r in observations):
                state = "CAPTURED_DELAYED"
            elif observations:
                state = observations[-1]["status"]
            elif now < expected_poll:
                state = "NOT_DUE"
            elif now < expected_poll + timedelta(minutes=8):
                state = "AWAITING_CAPTURE"
            else:
                state = "MISSING_CAPTURE"
            buckets.append({"target_slot_utc": slot.isoformat(), "target_slot_et": slot.astimezone(ET).isoformat(),
                            "expected_poll_utc": expected_poll.isoformat(), "status": state, "attempts": observations})
            slot += timedelta(minutes=15)
    return {
        "session_et": label, "market_open": market_open, "checked_at_utc": now.isoformat(),
        "source_kind": SOURCE_KIND, "buckets": buckets,
    }


def run(mode):
    import exchange_calendars as xc
    stamp = pipeline.now()
    session = market_slot(stamp, xc.get_calendar("XNYS"))
    if session is None:
        print("SKIP_OUTSIDE_RESEARCH_WINDOW")
        return 0
    if mode == "cloud":
        if os.getenv("XSP15M_STORAGE_AUTHORIZED") != "true":
            raise CaptureError("XSP automated retrieval and private retention rights unconfirmed")
        destination = DrivePrivateDestination()
        target = pipeline.remote()
    provider = TradierSandbox(os.getenv("XSP15M_TRADIER_SANDBOX_TOKEN"))
    packet, quality = collect(provider, *session)
    public = {k: quality[k] for k in ("status", "session_et", "target_slot_et", "rows", "research_gate")}
    public["underlying_clock_in_window"] = quality["underlying_clock_in_window"]
    public["expiries"] = quality["checks"]
    public["errors"] = quality["errors"]
    if mode == "cloud":
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / (stamp.strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12])
            stage_batch(batch, packet, quality)
            publish(batch, target, destination)
        public["private_drive_readback"] = "SHA256_VERIFIED"
    print(json.dumps(public))
    return 0 if quality["status"] == "CAPTURED_DELAYED" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("probe", "cloud", "sync", "inventory"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--session", type=date.fromisoformat)
    args = parser.parse_args()
    try:
        if args.mode in {"sync", "inventory"}:
            if not args.root:
                parser.error("--root is required for sync and inventory")
            if args.mode == "sync":
                sync(args.root)
            else:
                import exchange_calendars as xc
                stamp = pipeline.now()
                session = args.session or stamp.astimezone(ET).date()
                print(json.dumps(inventory(args.root, session.isoformat(), stamp, xc.get_calendar("XNYS"))))
            return 0
        return run(args.mode)
    except (CaptureError, OSError, RuntimeError, subprocess.SubprocessError, KeyError, ValueError) as exc:
        print(f"XSP scan failed: {exc}" if isinstance(exc, CaptureError) else f"XSP scan failed: {type(exc).__name__}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
