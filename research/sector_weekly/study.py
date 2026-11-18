"""Weekly historical risk and monetary-path analogues with next-close entry."""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

ASSETS = "SPY XLK XLY XLI XLP XLV XLU XLRE".split()
DEF = ["XLP", "XLV", "XLU"]
CYC = ["XLK", "XLY", "XLI"]
HORIZONS = [5, 10, 20]
START = "2001-07-01"
HOLDOUT = "2018-01-01"
METHODS = {
    "Risk": ["Risk"],
    "Fed": ["Fed"],
    "Both": ["Risk", "Fed"],
    "Risk+2Y": ["Risk", "TwoYear"],
    "Risk+2Y10bp": ["Risk", "TwoYear10bp"],
    "Risk+5d2Y": ["Risk", "TwoYear5"],
    "Risk5+Fed": ["Risk5", "Fed"],
}
TARGETS = ["SPY", "DefenseCyc", "XLP", "XLV", "XLU", "XLK", "XLRE"]


def yahoo(raw, ticker, adjusted):
    data = json.loads((raw / f"{ticker}.json").read_text())["chart"]["result"][0]
    quotes = data["indicators"]
    key = quotes["adjclose"][0]["adjclose"] if adjusted else quotes["quote"][0]["close"]
    times = pd.to_datetime(data["timestamp"], utc=True, unit="s").tz_convert("America/New_York")
    index = times.tz_localize(None).normalize()
    return pd.Series(key, index=index, name=ticker).dropna().sort_index()


def fred(raw, ticker):
    return pd.read_csv(raw / f"{ticker}.csv", index_col=0, parse_dates=True)[ticker].dropna()


def append_snapshot(values, updates):
    series = pd.Series(updates, dtype=float)
    series.index = pd.to_datetime(series.index)
    return series.combine_first(values).sort_index()


def risk_state(vix, spread, days):
    changes = pd.concat([vix.diff(days), spread.diff(days)], axis=1)
    risk = np.select([changes.gt(0).all(axis=1), changes.lt(0).all(axis=1)],
                     ["Off", "On"], default="Mixed")
    return pd.Series(risk, index=vix.index).where(changes.notna().all(axis=1))


def labelled(values, cut):
    label = np.select([values.ge(cut), values.le(-cut)], ["Up", "Down"], default="Flat")
    return pd.Series(label, index=values.index).where(values.notna())


def model_inputs(raw, snapshot, prices, events):
    live_sessions = pd.to_datetime(list(snapshot["vix"]))
    days = prices.index.union(live_sessions).sort_values()

    def rates(ticker):
        series = append_snapshot(fred(raw, ticker), snapshot.get(ticker, {}))
        # Same lag in current forecast and historical test; avoid FRED after-close updates.
        return series.reindex(days, method="ffill").shift(2)

    vix = append_snapshot(yahoo(raw, "%5EVIX", False), snapshot["vix"]).reindex(days)
    baa = rates("BAA10Y")
    dgs2 = rates("DGS2")
    real = rates("DFII10")
    p = events.ED2.add(events.ED3).add(events.ED4).div(3)
    p = pd.Series(p.to_numpy(), index=days[days.searchsorted(events.Date)])
    cumulative = p.groupby(level=0).sum().reindex(days, fill_value=0).cumsum()
    path = cumulative.shift(1).diff(63)

    panel = pd.DataFrame(index=days)
    panel["Vix"] = vix
    panel["Vix21"] = vix.diff(21)
    panel["Baa"] = baa
    panel["Baa21"] = baa.diff(21)
    panel["Baa5"] = baa.diff(5)
    panel["FedPathPP"] = path
    panel["TwoYear21PP"] = dgs2.diff(21)
    panel["TwoYear5PP"] = dgs2.diff(5)
    panel["RealYield21PP"] = real.diff(21)
    panel["Risk"] = risk_state(vix, baa, 21)
    panel["Risk5"] = risk_state(vix, baa, 5)
    panel["Fed"] = labelled(path, .05)
    panel["TwoYear"] = labelled(dgs2.diff(21), .000001)
    panel["TwoYear10bp"] = labelled(dgs2.diff(21).round(6), .10)
    panel["TwoYear5"] = labelled(dgs2.diff(5), .000001)
    rate = rates("DTB3") / 100
    cash = rate * days.to_series().diff().dt.days / 365
    return panel, cash


def trailing_beta(prices, cash):
    excess = prices.pct_change(fill_method=None).sub(cash.reindex(prices.index), axis=0)
    return excess.rolling(252).cov(excess.SPY).div(excess.SPY.rolling(252).var(), axis=0)


def returns_for_signals(prices, cash, beta, panel, signals):
    index = prices.index
    positions = index.get_indexer(signals)
    if (positions < 0).any():
        raise ValueError("A signal is outside the market calendar")
    rows = []
    for h in HORIZONS:
        i = positions[positions + h + 1 < len(index)]
        signals = index[i]
        entry, exit_ = index[i + 1], index[i + 1 + h]
        raw = prices.loc[exit_].set_axis(signals) / prices.loc[entry].set_axis(signals) - 1
        rf = (1 + cash.reindex(index)).rolling(h).apply(np.prod, raw=True).loc[exit_].set_axis(signals) - 1
        residual = raw.sub(rf, axis=0) - beta.loc[signals].mul(raw.SPY - rf, axis=0)
        outcomes = residual[TARGETS[2:]].copy()
        outcomes["SPY"] = raw.SPY
        outcomes["DefenseCyc"] = residual[DEF].mean(axis=1, skipna=False) - residual[CYC].mean(axis=1, skipna=False)
        outcomes["RawDefenseCyc"] = raw[DEF].mean(axis=1, skipna=False) - raw[CYC].mean(axis=1, skipna=False)
        outcomes["Drawdown"] = [
            (prices.SPY.iloc[a + 1:b + 2] / prices.SPY.iloc[a + 1:b + 2].cummax() - 1).min()
            for a, b in zip(i, i + h, strict=True)
        ]
        outcomes = outcomes.join(panel.loc[signals])
        outcomes["VixExit"] = panel.loc[exit_, "Vix"].to_numpy()
        outcomes["Entry"], outcomes["Exit"], outcomes["Horizon"] = entry, exit_, h
        rows.append(outcomes.rename_axis("Signal").reset_index())
    return pd.concat(rows, ignore_index=True)


def bootstrap(data, flags, target):
    # Resample whole years within each weekday stream to keep correlated weeks together.
    available = data[target].notna()
    groups = pd.DataFrame({
        "hits": (data[target] > 0).astype(float), "value": data[target], "state": flags,
        "year": data.Signal.dt.year,
    }).loc[available]
    by_year = groups.groupby("year")
    n = groups.year.nunique()
    on_years = groups.loc[groups.state, "year"].nunique()
    if n < 8 or on_years < 5 or groups.state.sum() < 15:
        return [np.nan] * 6
    counts = by_year.size()
    full = by_year[["value", "hits"]].sum().reindex(counts.index).to_numpy()
    matched_year = groups.loc[groups.state].groupby("year")
    matched = matched_year[["value", "hits"]].sum().reindex(counts.index, fill_value=0).to_numpy()
    selected = matched_year.size().reindex(counts.index, fill_value=0).to_numpy()
    rng = np.random.default_rng(20261118)
    draws = rng.integers(0, n, size=(5000, n))
    matched_n = selected[draws].sum(axis=1)
    good = matched_n > 0
    draws = draws[good]
    means = matched[draws].sum(axis=1) / matched_n[good, None]
    all_means = full[draws].sum(axis=1) / counts.to_numpy()[draws].sum(axis=1)[:, None]
    return np.quantile(means[:, 0], [.025, .975]).tolist() + np.quantile(means[:, 1], [.025, .975]).tolist() + np.quantile(
        means[:, 0] - all_means[:, 0], [.025, .975]).tolist()


def describe_states(windows, targets, live):
    rows = []
    for h, periods in windows.groupby("Horizon"):
        for sample, data in [("All", periods), ("Since2018", periods.loc[periods.Signal.ge(HOLDOUT)])]:
            for name, cols in [("All", []), *METHODS.items()]:
                data = data.copy()
                if not cols:
                    choices = [("All", pd.Series(True, index=data.index))]
                else:
                    states = data[cols].astype("string").agg("/".join, axis=1)
                    choices = [(state, states.eq(state)) for state in states.unique()]
                for state, flags in choices:
                    group = data.loc[flags]
                    if group.empty:
                        continue
                    focus = state == "/".join(str(live[c]) for c in cols) or name == "All"
                    for target in targets:
                        values = group[target].dropna()
                        if values.empty:
                            continue
                        low, high, hit_low, hit_high, gap_low, gap_high = (
                            bootstrap(data, flags, target) if focus else [np.nan] * 6
                        )
                        rows.append(dict(
                            Horizon=h, Sample=sample, Method=name, State=state, Target=target,
                            N=len(values), Years=group.loc[values.index, "Signal"].dt.year.nunique(),
                            Mean=values.mean(), Median=values.median(), Positive=values.gt(0).mean(),
                            SD=values.std(), P10=values.quantile(.10), P90=values.quantile(.90),
                            MeanLow=low, MeanHigh=high, HitLow=hit_low, HitHigh=hit_high,
                            MeanGap=values.mean()-data[target].mean(), GapLow=gap_low, GapHigh=gap_high,
                        ))
    return pd.DataFrame(rows)


def forecasts(history, targets):
    rows = []
    for h, data in history.groupby("Horizon"):
        data = data.reset_index(drop=True)
        for target in targets:
            values = data[target]
            for row in data.loc[data.Signal.ge(HOLDOUT)].itertuples(index=False):
                safe = data.Exit.le(row.Signal) & values.notna()
                training = values.loc[safe]
                actual = getattr(row, target)
                if len(training) < 100 or pd.isna(actual):
                    continue
                base = training.gt(0).mean()
                for name, cols in METHODS.items():
                    match = safe.copy()
                    for c in cols:
                        match &= data[c].eq(getattr(row, c))
                    votes = values.loc[match].gt(0)
                    # Shrink small cohorts toward the historical unconditional frequency.
                    forecast = (votes.sum() + 25 * base) / (len(votes) + 25)
                    rows.append(dict(Signal=row.Signal, Exit=row.Exit, Horizon=h, Method=name,
                                     Target=target, State="/".join(getattr(row, c) for c in cols),
                                     Count=len(votes), TrainingN=len(training), Actual=float(actual > 0),
                                     Forecast=forecast, Base=base))
    return pd.DataFrame(rows)


def score_forecasts(predictions, live):
    data = predictions.copy()
    data["Loss"] = (data.Forecast - data.Actual) ** 2
    data["BaseLoss"] = (data.Base - data.Actual) ** 2
    current = pd.Series({name: "/".join(str(live[c]) for c in cols) for name, cols in METHODS.items()})
    matched = data.loc[data.Method.map(current).eq(data.State)]
    fields = ["Horizon", "Method", "Target"]
    panels = []
    for name, group in [("All weeks", data), ("Current-state weeks", matched)]:
        table = group.groupby(fields).agg(N=("Actual", "size"), Brier=("Loss", "mean"),
                                           BaseBrier=("BaseLoss", "mean"))
        table["BrierSkill"] = 1 - table.Brier / table.BaseBrier
        panels.append(table.reset_index().assign(Sample=name))
    return pd.concat(panels, ignore_index=True)


def robustness_checks(windows):
    rows, volatility = [], []
    for h, group in windows.groupby("Horizon"):
        for method, flags in {
            "Both": group.Risk.eq("Off") & group.Fed.eq("Up"),
            "Risk+2Y": group.Risk.eq("Off") & group.TwoYear.eq("Up"),
        }.items():
            last_exit = pd.Timestamp.min
            positions = []
            for row in group.loc[flags].itertuples():
                if row.Entry >= last_exit:
                    positions.append(row.Index)
                    last_exit = row.Exit
            sample = group.loc[positions]
            for ticker in ["SPY", "DefenseCyc"]:
                values = sample[ticker]
                rows.append(dict(Horizon=h, Method=method, Target=ticker, N=len(values),
                                 Mean=values.mean(), Positive=values.gt(0).mean()))
        for cut, flags in {
            "All": group.Vix.notna(),
            "VIX 25–30": group.Vix.ge(25) & group.Vix.lt(30),
            "VIX 25+": group.Vix.ge(25),
        }.items():
            sample = group.loc[flags].VixExit.dropna()
            volatility.append(dict(Horizon=h, Screen=cut, N=len(sample),
                                   ExitMedian=sample.median(), ExitAtLeast20=sample.ge(20).mean()))
    return pd.DataFrame(rows), pd.DataFrame(volatility)


def fed_event_summary(prices, cash, beta, panel, events, raw):
    rate = pd.concat([fred(raw, "DFEDTAR"), fred(raw, "DFEDTARU")]).sort_index().diff()
    records = events.loc[events.Date.isin(prices.index)].copy()
    announced = rate.reindex(records.Date).to_numpy()
    effective = rate.reindex(records.Date + pd.Timedelta(days=1)).to_numpy()
    records["RateMovePP"] = announced + effective
    records["EventPathPP"] = records[["ED2", "ED3", "ED4"]].mean(axis=1, skipna=False)
    windows = returns_for_signals(prices, cash, beta, panel, pd.DatetimeIndex(records.Date))
    windows = windows.merge(records[["Date", "Unscheduled", "RateMovePP", "EventPathPP"]],
                            left_on="Signal", right_on="Date", validate="many_to_one")
    regular = windows.Unscheduled.eq(0)
    states = {
        "Hike": windows.RateMovePP.gt(0),
        "Hold": regular & windows.RateMovePP.eq(0),
        "Hawk": regular & windows.EventPathPP.ge(.05),
        "Neutral": regular & windows.EventPathPP.abs().lt(.05),
        "Dove": regular & windows.EventPathPP.le(-.05),
    }
    rows = []
    for state, flags in states.items():
        data = windows.loc[flags]
        for sample, group in [("All", data), ("Since2018", data.loc[data.Signal.ge(HOLDOUT)])]:
            metrics = group.groupby("Horizon")[TARGETS].agg(["mean", "count"])
            rows.append(metrics.stack(level=0, future_stack=True).rename_axis(["Horizon", "Target"])
                        .reset_index().assign(State=state, Sample=sample))
    return pd.concat(rows, ignore_index=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["raw", "snapshot", "events", "out"]:
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--historical-end", default="2026-11-13")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    snapshot = json.loads(args.snapshot.read_text())
    prices = pd.concat([yahoo(args.raw, t, True) for t in ASSETS], axis=1)
    prices = prices.loc[prices.SPY.notna()].sort_index().loc[:args.historical_end]
    events = pd.read_excel(args.events, sheet_name="Monetary Events", parse_dates=["Date"])
    events = events.loc[events.Date.le(snapshot["signal_date"])].sort_values("Date")
    panel, cash = model_inputs(args.raw, snapshot, prices, events)
    beta = trailing_beta(prices, cash)
    live = panel.loc[pd.Timestamp(snapshot["signal_date"])]
    pd.DataFrame([live]).to_csv(args.out / "live_signal.csv", index_label="Signal")
    samples, performance, nonoverlap, volatility = [], [], [], []
    for weekday in [1, 0, 4]:
        history = prices.loc[START:].index
        history = history[history.weekday == weekday]
        windows = returns_for_signals(prices, cash, beta, panel, history)
        windows = windows.dropna(subset=["DefenseCyc", *sorted({field for cols in METHODS.values() for field in cols})])
        valid = windows.Signal.lt(windows.Entry) & windows.Entry.lt(windows.Exit)
        if not valid.all():
            raise ValueError("Signal, execution and exit are out of order")
        samples.append(describe_states(windows, [*TARGETS, "RawDefenseCyc", "Drawdown"], live).assign(Weekday=weekday))
        strict_windows, vol = robustness_checks(windows)
        nonoverlap.append(strict_windows.assign(Weekday=weekday))
        volatility.append(vol.assign(Weekday=weekday))
        if weekday == 1:
            pred = forecasts(windows, ["SPY", "DefenseCyc"])
            performance.append(score_forecasts(pred, live).assign(Weekday=weekday))
            windows.to_csv(args.out / "tuesday_windows.csv", index=False)
            pred.to_csv(args.out / "tuesday_walkforward.csv", index=False)
    pd.concat(samples).to_csv(args.out / "historical_summary.csv", index=False)
    pd.concat(performance).to_csv(args.out / "walkforward_summary.csv", index=False)
    pd.concat(nonoverlap).to_csv(args.out / "nonoverlap_summary.csv", index=False)
    pd.concat(volatility).to_csv(args.out / "volatility_summary.csv", index=False)
    fed_event_summary(prices, cash, beta, panel, events, args.raw).to_csv(args.out / "fed_event_summary.csv", index=False)


if __name__ == "__main__":
    main()
