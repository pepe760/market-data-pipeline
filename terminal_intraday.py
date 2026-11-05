"""Bounded completed-bar supplement for the public market terminal."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import quote, urlencode

UTC = timezone.utc
INTERVAL = 900
HORIZONS = {"h1": 4, "h4": 16}
ROOT = Path(__file__).resolve().parent


class IntradayError(ValueError):
    """A bounded provider or quote-clock qualification failure."""


def finite(value):
    if type(value) not in (int, float):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def stamp(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def epoch(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


def normalize_yahoo(payload, alias, received):
    try:
        result, = payload["chart"]["result"]
        meta = result["meta"]
        if meta.get("symbol") != alias:
            raise IntradayError("INTRADAY_SYMBOL_MISMATCH")
        sessions = []
        for day in meta.get("tradingPeriods", []):
            if not isinstance(day, list):
                raise IntradayError("INTRADAY_SESSION_METADATA_INVALID")
            for part in day:
                first, last = finite(part.get("start")), finite(part.get("end"))
                if first is None or last is None or first >= last:
                    raise IntradayError("INTRADAY_SESSION_METADATA_INVALID")
                sessions.append((first, last))
        if not sessions:
            raise IntradayError("INTRADAY_SESSION_METADATA_UNAVAILABLE")
        indicators = result["indicators"]["quote"][0]
        rows, seen = [], set()
        rejected = {"unaligned": 0, "incomplete_or_future": 0, "invalid": 0, "outside_session": 0}
        for index, start in enumerate(result.get("timestamp", [])):
            if finite(start) is None or start <= 0 or start % INTERVAL:
                rejected["unaligned"] += 1
                continue
            end = start + INTERVAL
            if end > received.timestamp():
                rejected["incomplete_or_future"] += 1
                continue
            if not any(first <= start and end <= last for first, last in sessions):
                rejected["outside_session"] += 1
                continue
            values = {key: finite(indicators[key][index]) for key in ("open", "high", "low", "close")}
            if (any(value is None or value <= 0 for value in values.values())
                    or values["low"] > min(values["open"], values["close"])
                    or values["high"] < max(values["open"], values["close"])):
                rejected["invalid"] += 1
                continue
            if start in seen:
                raise IntradayError("INTRADAY_DUPLICATE_BAR")
            seen.add(start)
            volume = finite(indicators.get("volume", [None] * len(result["timestamp"]))[index])
            rows.append(dict(values, start=stamp(start), end=stamp(end), volume=volume))
        return sorted(rows, key=lambda row: row["end"])[-64:], rejected
    except (KeyError, IndexError, TypeError, OverflowError) as exc:
        raise IntradayError("INTRADAY_CHART_SCHEMA_INVALID") from None


def normalize_binance(payload, received):
    if not isinstance(payload, list) or len(payload) > 100:
        raise IntradayError("INTRADAY_KLINE_SCHEMA_INVALID")
    rows, seen = [], set()
    rejected = {"unaligned": 0, "incomplete_or_future": 0, "invalid": 0, "outside_session": 0}
    for raw in payload:
        if not isinstance(raw, list) or len(raw) < 7:
            rejected["invalid"] += 1
            continue
        start_ms, close_ms = finite(raw[0]), finite(raw[6])
        if start_ms is None or close_ms is None or start_ms <= 0 or start_ms % (INTERVAL * 1000):
            rejected["unaligned"] += 1
            continue
        start, end = start_ms / 1000, (close_ms + 1) / 1000
        if end != start + INTERVAL:
            rejected["invalid"] += 1
            continue
        if end > received.timestamp():
            rejected["incomplete_or_future"] += 1
            continue
        try:
            values = dict(zip(("open", "high", "low", "close"), [float(x) for x in raw[1:5]]))
            volume = float(raw[5])
        except (TypeError, ValueError, OverflowError):
            rejected["invalid"] += 1
            continue
        if (any(finite(x) is None or x <= 0 for x in values.values()) or finite(volume) is None or volume < 0
                or values["low"] > min(values["open"], values["close"])
                or values["high"] < max(values["open"], values["close"])):
            rejected["invalid"] += 1
            continue
        if start in seen:
            raise IntradayError("INTRADAY_DUPLICATE_BAR")
        seen.add(start)
        rows.append(dict(values, start=stamp(start), end=stamp(end), volume=volume))
    return sorted(rows, key=lambda row: row["end"])[-64:], rejected


def window(bars, count, end=None):
    index = {row["end"]: row for row in bars}
    selected_end = end or (bars[-1]["end"] if bars else None)
    last = epoch(selected_end) if selected_end else None
    if last is None:
        return None
    sequence = [stamp(last - offset * INTERVAL) for offset in range(count, -1, -1)]
    if any(key not in index for key in sequence):
        return None
    first, final = index[sequence[0]], index[sequence[-1]]
    if first["close"] <= 0 or final["close"] <= 0:
        return None
    return final["close"] / first["close"] - 1, [sequence[0], sequence[-1]]


def calculate(bars, benchmark_bars):
    returns, relative, windows, rs_windows = {}, {}, {}, {}
    for label, count in HORIZONS.items():
        own = window(bars, count)
        returns[label], windows[label] = own if own else (None, None)
        paired = window(benchmark_bars, count, own[1][-1]) if own else None
        if own and paired and own[1] == paired[1]:
            relative[label], rs_windows[label] = own[0] - paired[0], own[1]
        else:
            relative[label], rs_windows[label] = None, None
    return returns, relative, windows, rs_windows


def file_path(root, symbol):
    return root / "intraday" / (hashlib.sha256(symbol.encode()).hexdigest()[:20] + ".json")


def read_previous(root, symbol, metadata):
    path = file_path(root, symbol)
    if not path.is_file() or path.is_symlink():
        return []
    value = json.loads(path.read_text())
    bars = value.get("bars")
    if (value.get("symbol") != symbol or value.get("asof") != metadata.get("asof")
            or not isinstance(bars, list) or len(bars) > 64):
        return []
    return bars


def default_metadata(benchmark):
    return {
        "returns": {"h1": None, "h4": None}, "rs": {"h1": None, "h4": None},
        "asof": None, "last_attempt": None, "received_at": None, "quality": "NOT_OBSERVED",
        "benchmark": benchmark, "windows": {"h1": None, "h4": None},
        "rs_windows": {"h1": None, "h4": None}, "source": None, "bar_count": 0,
        "gap_count": None, "series_path": None, "fresh": False,
    }


def enrich(rows, previous, instruments, output_dir, *, budget, workers, force,
           clock, fetch, write_json, active_market, limited):
    config = json.loads((ROOT / "terminal_intraday_core.json").read_text())
    core = [symbol for group in config["groups"].values() for symbol in group]
    if len(core) != 60 or len(set(core)) != 60:
        raise IntradayError("INVALID_INTRADAY_CORE_UNIVERSE")
    by_symbol = {item["symbol"]: item for item in instruments}
    requested = [symbol for symbol in core if symbol in by_symbol and
                 (force or symbol in config["benchmarks"] or active_market(by_symbol[symbol]["market"], clock()))]
    requested.sort(key=lambda symbol: (
        config["benchmarks"].index(symbol) if symbol in config["benchmarks"] else 9,
        previous.get(symbol, {}).get("intraday", {}).get("last_attempt") or "", symbol
    ))
    chosen = requested[:budget]
    all_bars = {}
    for symbol in core:
        if symbol not in rows:
            continue
        benchmark = "BTCUSDT" if by_symbol[symbol]["market"] == "Crypto" else "2800.HK" if by_symbol[symbol]["market"] == "HK" else "SPY"
        rows[symbol]["intraday"] = dict(previous.get(symbol, {}).get("intraday") or default_metadata(benchmark))
        all_bars[symbol] = read_previous(output_dir, symbol, rows[symbol]["intraday"])
        if rows[symbol]["intraday"].get("asof") and not all_bars[symbol]:
            rows[symbol]["intraday"]["quality"] = "HISTORY_FILE_UNAVAILABLE"

    def one(symbol):
        if limited.is_set():
            return symbol, None, None, None, "NOT_ATTEMPTED_RATE_LIMIT"
        try:
            instrument = by_symbol[symbol]
            if instrument["market"] == "Crypto":
                url = "https://data-api.binance.vision/api/v3/klines?" + urlencode({
                    "symbol": symbol, "interval": "15m", "limit": 65
                })
                payload = fetch(url)
                received = clock()
                bars, rejected = normalize_binance(payload, received)
            else:
                alias = {"BRK.B": "BRK-B", "BF.B": "BF-B"}.get(symbol, symbol)
                url = "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(alias, safe="") + "?range=5d&interval=15m&includePrePost=false"
                payload = fetch(url)
                received = clock()
                bars, rejected = normalize_yahoo(payload, alias, received)
            if not bars:
                raise IntradayError("NO_COMPLETE_INTRADAY_BARS")
            return symbol, bars, rejected, received, None
        except (ValueError, KeyError, IndexError, TypeError, OverflowError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            if reason == "HTTP_429":
                limited.set()
            return symbol, None, None, clock(), reason

    failures, updated, unattempted = [], 0, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for symbol, bars, rejected, received, error in pool.map(one, chosen):
            metadata = rows[symbol]["intraday"]
            if error:
                if error == "NOT_ATTEMPTED_RATE_LIMIT":
                    unattempted += 1
                else:
                    metadata["last_attempt"] = clock().isoformat()
                metadata["quality"] = "SOURCE_ERROR_PREVIOUS_RETAINED" if metadata.get("asof") else "SOURCE_ERROR"
                metadata["error"] = error
                failures.append({"symbol": symbol, "error": error})
                continue
            all_bars[symbol] = bars
            metadata.update(
                asof=bars[-1]["end"], last_attempt=received.isoformat(), received_at=received.isoformat(),
                quality="OBSERVED_COMPLETE_BARS", source="Binance spot" if by_symbol[symbol]["market"] == "Crypto" else "Yahoo Finance",
                bar_count=len(bars), gap_count=sum(epoch(right["end"]) - epoch(left["end"]) != INTERVAL for left, right in zip(bars, bars[1:])),
                rejected=rejected, error=None,
            )
            path = file_path(output_dir, symbol)
            write_json(path, {"schema_version": 1, "symbol": symbol, "asof": metadata["asof"], "bars": bars})
            metadata["series_path"] = "data/intraday/" + path.name
            updated += 1

    for symbol in core:
        if symbol not in rows:
            continue
        metadata, bars = rows[symbol]["intraday"], all_bars[symbol]
        ret, rs, windows, rs_windows = calculate(bars, all_bars.get(metadata["benchmark"], []))
        metadata.update(returns=ret, rs=rs, windows=windows, rs_windows=rs_windows)
        asof = epoch(metadata.get("asof"))
        metadata["fresh"] = bool(bars) and asof is not None and 0 <= clock().timestamp() - asof <= 1800
    return {
        "target": len(core), "eligible": len(requested), "attempted": len(chosen),
        "stored": sum(bool(rows[s].get("intraday", {}).get("asof")) for s in core if s in rows),
        "fresh": sum(bool(rows[s].get("intraday", {}).get("fresh")) for s in core if s in rows),
        "failed": len(failures) - unattempted, "updated": updated,
        "not_observed": sum(s not in rows or not rows[s].get("intraday", {}).get("asof") for s in core),
        "unattempted": len(requested) - len(chosen) + unattempted, "errors": failures,
        "budget": budget, "core_symbols": core,
        "cadence": "24 targets per ordinary run across60 configured names; actual clocks and coverage show rotation",
        "gap_policy": "Exact closed15m endpoints and contiguous windows only; overnight/lunch/missing gaps yieldnull",
    }
