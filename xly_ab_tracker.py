"""Maintain the prospective XLY three-month paper A/B record for the public terminal."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import exchange_calendars as xc
import numpy as np
import pandas as pd

from public_terminal import SourceError, fetch, number, write_json
from terminal_context import parse_fred


UTC = timezone.utc
NY = ZoneInfo("America/New_York")
FIRST_SIGNAL = date(2026, 11, 30)
PROTOCOL = "XLY-3M-2026-11-18"
SEED = Path(__file__).with_name("xly_ab_training_seed.csv")
BASE_FEATURES = ["rs3", "rs20", "risk_off"]
FACTOR = "yield_fall"
MODEL_COLUMNS = [*BASE_FEATURES, FACTOR, "exit", "actual"]
QUARTER_MONTHS = {11, 2, 5, 8}
MIN_TRAIN = 36
CLOSE_GRACE = timedelta(minutes=15)
YAHOO_FIELDS = {"XLY": "adjusted", "SPY": "adjusted", "^VIX": "raw"}
FRED_FIELDS = ("DGS2", "DTB3", "T10Y2Y")


class DataUnavailable(ValueError):
    """A missing or unverified input that should be retried on the next eligible run."""


@dataclass(frozen=True)
class Window:
    signal: pd.Timestamp
    entry: pd.Timestamp
    exit: pd.Timestamp

    @property
    def key(self):
        return self.signal.date().isoformat()


def as_utc(stamp):
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Use an explicit timezone in the run timestamp.")
    return stamp.astimezone(UTC)


def close_at(calendar, session):
    return calendar.session_close(session).to_pydatetime()


def month_window(calendar, month):
    signal = calendar.date_to_session(month.end_time.normalize(), direction="previous")
    entry = calendar.date_to_session((month + 1).start_time, direction="next")
    exit_session = calendar.date_to_session((month + 4).start_time, direction="next")
    return Window(signal, entry, exit_session)


def windows_through_next(calendar, now):
    first = pd.Period(FIRST_SIGNAL, freq="M")
    current = pd.Period(now.astimezone(NY).date(), freq="M")
    return [month_window(calendar, month) for month in pd.period_range(first, max(first, current) + 1)]


def parse_chart(payload, symbol, cutoff):
    try:
        result, = payload["chart"]["result"]
        meta = result["meta"]
        if meta["symbol"] != symbol:
            raise DataUnavailable(f"{symbol} returned a different instrument")
        market_at = datetime.fromtimestamp(meta["regularMarketTime"], UTC)
        zone = ZoneInfo(meta["exchangeTimezoneName"])
        timestamps = result["timestamp"]
        indicators = result["indicators"]
        kind = YAHOO_FIELDS[symbol]
        prices = indicators["adjclose"][0]["adjclose"] if kind == "adjusted" else indicators["quote"][0]["close"]
        if len(timestamps) != len(prices):
            raise DataUnavailable(f"{symbol} price and date counts differ")
        rows = {}
        for epoch, price in zip(timestamps, prices):
            stamp = datetime.fromtimestamp(epoch, UTC)
            price = number(price)
            day = stamp.astimezone(zone).date()
            if stamp > cutoff or price is None or price <= 0:
                continue
            if day in rows and rows[day] != price:
                raise DataUnavailable(f"{symbol} has conflicting daily closes")
            rows[day] = price
        return pd.Series(rows, dtype=float).rename_axis("date"), market_at
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, DataUnavailable):
            raise
        raise DataUnavailable(f"{symbol} adjusted prices unavailable") from None


def fetch_chart(symbol, now):
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{quote(symbol, safe='')}?range=2y&interval=1d"
        try:
            observations, market_at = parse_chart(fetch(url), symbol, now)
            return observations, market_at, url
        except (SourceError, DataUnavailable):
            continue
    raise DataUnavailable(f"{symbol} daily source unavailable")


def fetch_rate(series, now):
    day = now.astimezone(NY).date()
    args = urlencode({"id": series, "cosd": day - timedelta(days=800), "coed": day})
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?" + args
    try:
        items = parse_fred(fetch(url, json_data=False).encode("utf-8"), series, day)
        return pd.Series({date.fromisoformat(p["date"]): p["value"] for p in items}, dtype=float), url
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise DataUnavailable(f"{series} FRED source unavailable") from None


@dataclass(frozen=True)
class Inputs:
    prices: dict
    rates: dict
    market_stamps: dict
    sources: dict


def get_inputs(now, forecast_due):
    symbols = YAHOO_FIELDS if forecast_due else ("XLY", "SPY")
    series = FRED_FIELDS if forecast_due else ("DTB3",)
    with ThreadPoolExecutor(max_workers=6) as pool:
        price_jobs = {name: pool.submit(fetch_chart, name, now) for name in symbols}
        rate_jobs = {name: pool.submit(fetch_rate, name, now) for name in series}
    prices, rates, stamps, sources = {}, {}, {}, {}
    for name, job in price_jobs.items():
        prices[name], stamps[name], sources[name] = job.result()
    for name, job in rate_jobs.items():
        rates[name], sources[name] = job.result()
    return Inputs(prices, rates, stamps, sources)


def price_rows(inputs, calendar, sessions, symbols):
    days = sessions.date
    for symbol in symbols:
        if inputs.market_stamps[symbol] < close_at(calendar, sessions[-1]):
            raise DataUnavailable(f"{symbol} close has not been confirmed")
    frame = pd.DataFrame({symbol: inputs.prices[symbol].reindex(days).to_numpy() for symbol in symbols}, index=sessions)
    if not np.isfinite(frame).all().all() or (frame <= 0).any().any():
        raise DataUnavailable("The expected exchange sessions have missing prices")
    return frame


def rate_at(series, through):
    known = series.loc[:through]
    if known.empty or (through - known.index[-1]).days > 7:
        raise DataUnavailable("A Treasury series is missing or stale at the required cutoff")
    return float(known.iloc[-1]), known.index[-1].isoformat()


def cash_returns(rates, calendar, sessions):
    cutoff_sessions = calendar.sessions_in_range(sessions[0] - pd.Timedelta(days=14), sessions[-1])
    available = rates.reindex(cutoff_sessions.date)
    rates_asof = available.ffill()
    rates_prior = rates_asof.shift().reindex(sessions.date)
    observation_day = pd.Series(cutoff_sessions.date, index=cutoff_sessions.date).where(available.notna()).ffill()
    observation_day = observation_day.shift().reindex(sessions.date)
    if rates_prior.isna().any() or any((session - observed).days > 7 for session, observed in observation_day.items()):
        raise DataUnavailable("DTB3 is missing or stale within the return window")
    calendar_days = sessions.to_series().diff().dt.days
    return pd.Series(rates_prior.to_numpy(), index=sessions) / 100 * calendar_days / 365


def feature_row(inputs, calendar, window):
    # 252 daily returns need 253 complete prices. Use extra sessions to build the same monthly SMA.
    sessions = calendar.sessions_in_range(window.signal - pd.Timedelta(days=410), window.signal)
    daily = price_rows(inputs, calendar, sessions, ["XLY", "SPY"])
    if len(daily) < 253:
        raise DataUnavailable("Fewer than 253 paired trading-session closes")
    monthly = daily.groupby(daily.index.to_period("M")).tail(1)
    relative_three = monthly.pct_change(3).iloc[-1]
    relative_twenty = daily.pct_change(20).iloc[-1]
    current = calendar.previous_session(window.signal).date()
    older = calendar.previous_session(monthly.index[-4]).date()
    present_rate, present_date = rate_at(inputs.rates["DGS2"], current)
    previous_rate, previous_date = rate_at(inputs.rates["DGS2"], older)
    curve, curve_date = rate_at(inputs.rates["T10Y2Y"], current)
    vix = price_rows(inputs, calendar, sessions[-1:], ["^VIX"])["^VIX"].iloc[-1]
    cash = cash_returns(inputs.rates["DTB3"], calendar, daily.index[-253:])
    excess = daily.pct_change().reindex(cash.index).sub(cash, axis=0).iloc[-252:]
    market_variance = excess.SPY.var()
    if not np.isfinite(excess).all().all() or not market_variance > 0:
        raise DataUnavailable("Insufficient variance or incomplete paired return history")
    beta = excess.XLY.cov(excess.SPY) / market_variance
    features = {"rs3": float(relative_three.XLY - relative_three.SPY),
                "rs20": float(relative_twenty.XLY - relative_twenty.SPY),
                "risk_off": int(monthly.SPY.iloc[-1] < monthly.SPY.iloc[-12:].mean() and (vix >= 25 or curve < 0)),
                FACTOR: (previous_rate - present_rate) / 100}
    provenance = {"treasury_cutoff": current.isoformat(), "treasury_comparison_cutoff": older.isoformat(),
                  "dgs2_observation": present_date, "dgs2_comparison_observation": previous_date,
                  "dgs2_percent": present_rate, "dgs2_comparison_percent": previous_rate,
                  "curve_observation": curve_date, "beta_first_price": daily.index[-253].date().isoformat(),
                  "beta_last_price": window.key, "fred_vintage": "Observed during the recorded run; prior-session cutoff"}
    return features, float(beta), provenance


def read_seed(path):
    data = pd.read_csv(path).rename(columns={"Exit": "exit", "RS3": "rs3", "RS20": "rs20",
                                           "RiskOff": "risk_off", "2Y yield": FACTOR, "Frozen 252d": "actual"})
    return data[MODEL_COLUMNS].assign(exit=lambda frame: pd.to_datetime(frame.exit, errors="raise"))


def training_rows(seed, forecasts, scores, window):
    additions = []
    for score in scores:
        if score["exit"] > window.key:
            continue
        forecast = forecasts[score["signal"]]
        additions.append({**forecast["features"], "exit": pd.Timestamp(score["exit"]), "actual": score["actual"]})
    available = seed.loc[seed.exit <= window.signal]
    if additions:
        available = pd.concat([available, pd.DataFrame(additions)], ignore_index=True)
    if len(available) < MIN_TRAIN:
        raise DataUnavailable("Fewer than 36 matured training observations")
    return available


def prediction(data, features, columns):
    sample = data[columns]
    sd = sample.std(ddof=0)
    active = sd.index[sd > 1e-12]
    mean = sample[active].mean()
    train = (sample[active] - mean) / sd[active]
    params = np.linalg.lstsq(np.column_stack([np.ones(len(train)), train]), data.actual, rcond=None)[0]
    row = (pd.Series(features)[active] - mean) / sd[active]
    value = float(np.r_[1, row.to_numpy()] @ params)
    if not np.isfinite(value):
        raise DataUnavailable("The fitted model did not produce a finite prediction")
    return value


def score_forecast(inputs, calendar, window, forecast, now):
    sessions = calendar.sessions_in_range(window.entry, window.exit)
    prices = price_rows(inputs, calendar, sessions, ["XLY", "SPY"])
    gross = prices.iloc[-1] / prices.iloc[0] - 1
    cash = (1 + cash_returns(inputs.rates["DTB3"], calendar, sessions).iloc[1:]).prod() - 1
    actual = float(gross.XLY - cash - forecast["beta"] * (gross.SPY - cash))
    return {"signal": window.key, "exit": window.exit.date().isoformat(), "actual": actual,
            **{f"squared_error_{key}": (actual - forecast[key]) ** 2 for key in ("a", "b", "mean")},
            "observed_at": now.isoformat(), "sources": {k: inputs.sources[k] for k in ("XLY", "SPY", "DTB3")}}


def validation(scores, forecasts):
    def group(rows):
        errors = {key: sum(row[f"squared_error_{key}"] for row in rows) for key in ("a", "b", "mean")}
        return {"n": len(rows), "improvement_vs_a": 1 - errors["b"] / errors["a"] if errors["a"] > 0 else None,
                "improvement_vs_mean": 1 - errors["b"] / errors["mean"] if errors["mean"] > 0 else None}

    quarter = [row for row in scores if forecasts[row["signal"]]["quarter_phase"]]
    return {"monthly": group(scores), "quarterly": group(quarter)}


def settle(state, calendar, clock, seed_path=SEED, load_inputs=get_inputs):
    """Process due observations once, leaving previously published records intact."""
    now = as_utc(clock())
    windows = windows_through_next(calendar, now)
    forecasts = {rec["signal"]: rec for rec in state.get("forecasts", [])}
    scores = {rec["signal"]: rec for rec in state.get("scores", [])}
    by_date = {window.key: window for window in windows}
    if not set(forecasts).issubset(by_date) or not set(scores).issubset(forecasts):
        raise ValueError("Stored tracker sessions do not match the verified calendar")
    for signal, rec in forecasts.items():
        window = by_date[signal]
        if rec["entry"] != window.entry.date().isoformat() or rec["exit"] != window.exit.date().isoformat():
            raise ValueError("Stored entry or exit differs from the verified calendar")
        recorded = as_utc(datetime.fromisoformat(rec["recorded_at"]))
        if not close_at(calendar, window.signal) <= recorded < close_at(calendar, window.entry):
            raise ValueError("Stored forecast was not recorded before entry close")
    past = [window for window in windows if close_at(calendar, window.signal) <= now]
    latest = past[-1] if past else None
    next_signal = next(window for window in windows if close_at(calendar, window.signal) > now)
    mature = [by_date[key] for key in forecasts.keys() - scores.keys()
              if close_at(calendar, by_date[key].exit) + CLOSE_GRACE <= now]
    pending = (latest if latest and latest.key not in forecasts
               and close_at(calendar, latest.signal) + CLOSE_GRACE <= now < close_at(calendar, latest.entry) else None)
    retry_key = now.replace(minute=0, second=0, microsecond=0).isoformat()
    due = [window.key for window in sorted(mature, key=lambda window: window.key)]
    attempt = {"hour": retry_key, "pending_signal": pending.key if pending else None, "pending_scores": due}
    last_attempt = state.get("last_attempt")
    error = state.get("error") if pending or mature else None
    if (pending or mature) and attempt != last_attempt:
        last_attempt = attempt
        failures = []
        try:
            inputs = load_inputs(now, forecast_due=pending is not None)
        except DataUnavailable as exc:
            failures.append(str(exc))
        else:
            for window in sorted(mature, key=lambda w: w.key):
                try:
                    scores[window.key] = score_forecast(inputs, calendar, window, forecasts[window.key], as_utc(clock()))
                except DataUnavailable as exc:
                    failures.append(str(exc))
            if pending:
                try:
                    features, beta, provenance = feature_row(inputs, calendar, pending)
                    seed = read_seed(seed_path)
                    train = training_rows(seed, forecasts, list(scores.values()), pending)
                    result = {"a": prediction(train, features, BASE_FEATURES),
                              "b": prediction(train, features, [*BASE_FEATURES, FACTOR]), "mean": float(train.actual.mean())}
                    recorded_at = as_utc(clock())
                    if recorded_at < close_at(calendar, pending.entry):
                        forecasts[pending.key] = {"signal": pending.key, "entry": pending.entry.date().isoformat(),
                                                  "exit": pending.exit.date().isoformat(), "recorded_at": recorded_at.isoformat(),
                                                  "quarter_phase": pending.signal.month in QUARTER_MONTHS, "beta": beta,
                                                  "training_rows": len(train), "latest_training_exit": train.exit.max().date().isoformat(),
                                                  "features": features, "provenance": provenance, "sources": inputs.sources, **result}
                except DataUnavailable as exc:
                    failures.append(str(exc))
        error = "; ".join(dict.fromkeys(failures)) if failures else None
    finished = as_utc(clock())
    missing = [w.key for w in past if w.key not in forecasts and close_at(calendar, w.entry) <= finished]
    status = "waiting_for_signal"
    if latest:
        status = ("tracking" if latest.key in forecasts else
                  "missed_signal" if latest.key in missing else "pending_data")
    scored = sorted(scores.values(), key=lambda rec: rec["signal"])
    return {"schema_version": 1, "protocol": PROTOCOL, "status": status,
            "next_signal_date": next_signal.key, "latest_signal_date": latest.key if latest else None,
            "forecasts": sorted(forecasts.values(), key=lambda rec: rec["signal"]), "scores": scored,
            "missed_signals": missing, "validation": validation(scored, forecasts),
            "first_review_quarters": 8, "error": error, "last_attempt": last_attempt}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--now", help="Inspect the calendar at an ISO timestamp. Read-only; never records a forecast or score.")
    args = parser.parse_args()
    calendar = xc.get_calendar("XNYS")
    if args.now:
        now = as_utc(datetime.fromisoformat(args.now))
        print(json.dumps([{"signal": w.key, "entry": w.entry.date().isoformat(), "exit": w.exit.date().isoformat()}
                          for w in windows_through_next(calendar, now)], separators=(",", ":")))
        return
    state = json.loads(args.output.read_text()) if args.output.exists() else {}
    if state and (state.get("schema_version") != 1 or state.get("protocol") != PROTOCOL):
        raise ValueError("Invalid or unrecognized tracker state")
    updated = settle(state, calendar, lambda: datetime.now(UTC))
    previous = {key: value for key, value in state.items() if key != "generated_at"}
    if updated != previous:
        updated["generated_at"] = datetime.now(UTC).isoformat()
        write_json(args.output, updated)
    print(f"XLY A/B {updated['status']}: {len(updated['forecasts'])} forecasts, {len(updated['scores'])} scores")


if __name__ == "__main__":
    main()
