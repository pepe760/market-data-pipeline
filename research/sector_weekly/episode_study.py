"""Compare new and repeated weekly Risk-off plus hawkish Fed signals."""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import study

OUTCOMES = ["SPY", "Offensive", "Defensive", "DefenseCyc", "RawDefenseCyc"]


def joint_episodes(panel, gap=0):
    states = panel.Risk.eq("Off") & panel.Fed.eq("Up")
    active = np.flatnonzero(states.to_numpy())
    if not len(active):
        return pd.Series(pd.NaT, index=states.index, dtype="datetime64[ns]")
    dates = states.index[active].to_series()
    onset = np.r_[True, np.diff(active) > gap + 1]
    return dates.where(onset).ffill().reindex(states.index)


def labels_for_tuesdays(panel):
    flags = panel.loc[panel.index.weekday == 1]
    labels = pd.DataFrame({"WeeklyEpisode": joint_episodes(flags),
                           "DailyEpisode": joint_episodes(panel).reindex(flags.index),
                           "DailyGap3Episode": joint_episodes(panel, gap=3).reindex(flags.index),
                           "Risk": flags.Risk, "Fed": flags.Fed})
    labels = labels.dropna(subset=["WeeklyEpisode"]).loc[study.START:].copy()
    for label in ["Weekly", "Daily", "DailyGap3"]:
        labels[f"{label}Order"] = labels.groupby(f"{label}Episode").cumcount() + 1
    return labels


def price_between(prices, first, last, index):
    return prices.loc[last].set_axis(index) / prices.loc[first].set_axis(index) - 1


def pre_signal_returns(prices, cash, beta, dates):
    calendar = prices.index
    signal = dates.intersection(calendar)
    start = calendar[calendar.get_indexer(signal) - 5]
    returns = price_between(prices, start, signal, signal)
    bills = (1 + cash.reindex(calendar)).rolling(5).apply(np.prod, raw=True).loc[signal] - 1
    hedge = beta.loc[start].set_axis(signal)
    residual = returns.sub(bills, axis=0) - hedge.mul(returns.SPY - bills, axis=0)
    defensive = returns[study.DEF].mean(axis=1, skipna=False)
    offensive = returns[study.CYC].mean(axis=1, skipna=False)
    adjusted = residual[study.DEF].mean(axis=1, skipna=False) - residual[study.CYC].mean(axis=1, skipna=False)
    return pd.DataFrame({"PreSPY": returns.SPY, "PreOffensive": offensive, "PreDefensive": defensive,
                         "PreRawSpread": defensive - offensive, "PreHedgedSpread": adjusted})


def matched_windows(windows, labels, prices, cash, beta):
    sample = windows.merge(labels.drop(columns=["Risk", "Fed"]), left_on="Signal",
                           right_index=True, how="inner", validate="many_to_one")
    raw = price_between(prices, sample.Entry, sample.Exit, sample.index)
    sample["Offensive"] = raw[study.CYC].mean(axis=1, skipna=False)
    sample["Defensive"] = raw[study.DEF].mean(axis=1, skipna=False)
    np.testing.assert_allclose(sample.Defensive - sample.Offensive, sample.RawDefenseCyc, atol=1e-12)
    np.testing.assert_allclose(raw.SPY, sample.SPY, atol=1e-12)
    pre = pre_signal_returns(prices, cash, beta, labels.index)
    return sample.merge(pre, left_on="Signal", right_index=True, validate="many_to_one").sort_values(["Signal", "Horizon"])


def cluster_interval(data, values, state, episode):
    # Bootstrap the first-vs-repeat contrast by calendar year of episode onset.
    parts = pd.DataFrame({"Year": data[episode].dt.year, "First": state, "Value": data[values]}).dropna()
    p = parts.groupby(["Year", "First"]).Value.agg(["size", "sum"]).unstack(fill_value=0)
    if len(p) < 5 or state.sum() < 5 or (~state).sum() < 5:
        return np.nan, np.nan
    rng = np.random.default_rng(20261118)
    draws = rng.integers(0, len(p), size=(10_000, len(p)))
    n = p["size"].to_numpy()[draws].sum(axis=1)
    totals = p["sum"].to_numpy()[draws].sum(axis=1)
    good = (n > 0).all(axis=1)
    means = totals[good] / n[good]
    return np.quantile(means[:, 1] - means[:, 0], [.025, .975])


def summary(sample):
    rows = []
    pre = ["PreSPY", "PreOffensive", "PreDefensive", "PreRawSpread", "PreHedgedSpread"]
    for mode in ["Weekly", "Daily", "DailyGap3"]:
        order, episode = f"{mode}Order", f"{mode}Episode"
        for group, data in [("All", sample), ("Since2018", sample.loc[sample.Signal.ge(study.HOLDOUT)])]:
            for h, frame in data.groupby("Horizon"):
                first = frame[order].eq(1)
                for target in [*OUTCOMES, *pre]:
                    start = frame.loc[first, target].dropna()
                    repeat = frame.loc[~first, target].dropna()
                    low, high = cluster_interval(frame, target, first, episode)
                    rows.append(dict(Mode=mode, Sample=group, Horizon=h, Target=target,
                                     FirstN=len(start), RepeatN=len(repeat), FirstMean=start.mean(),
                                     RepeatMean=repeat.mean(), FirstMedian=start.median(), RepeatMedian=repeat.median(),
                                     FirstPositive=start.gt(0).mean(), RepeatPositive=repeat.gt(0).mean(),
                                     FirstMinusRepeat=start.mean() - repeat.mean(), Low95=low, High95=high))
    return pd.DataFrame(rows)


def paired_repeats(sample):
    rows = []
    for h, frame in sample.groupby("Horizon"):
        first_two = frame.loc[frame.WeeklyOrder.le(2)]
        for target in OUTCOMES:
            pairs = first_two.pivot(index="WeeklyEpisode", columns="WeeklyOrder", values=target).dropna()
            values = pairs[1] - pairs[2]
            years = values.index.year
            grouped = values.groupby(years).agg(["size", "sum"])
            rng = np.random.default_rng(20261119)
            draws = rng.integers(0, len(grouped), size=(10_000, len(grouped)))
            total = grouped.to_numpy()[draws].sum(axis=1)
            low, high = np.quantile(total[:, 1] / total[:, 0], [.025, .975])
            rows.append(dict(Horizon=h, Target=target, N=len(pairs), FirstMean=pairs[1].mean(),
                             SecondMean=pairs[2].mean(), FirstMinusSecond=values.mean(),
                             Low95=low, High95=high))
    return pd.DataFrame(rows)


def historical_examples(labels, windows):
    # Include the longest past run and the four most recent runs with mature pairs.
    eligible = windows.loc[windows.Horizon.eq(20) & windows.WeeklyOrder.le(2)]
    mature = eligible.groupby("WeeklyEpisode").size()
    recent = mature.index[mature.eq(2)][-4:]
    counts = labels.groupby("WeeklyEpisode").size()
    longest = counts.loc[eligible.WeeklyEpisode.unique()].idxmax()
    chosen = (windows.WeeklyEpisode.isin(recent) & windows.WeeklyOrder.le(2)) | windows.WeeklyEpisode.eq(longest)
    examples = windows.loc[chosen].copy()
    examples["TuesdaysInEpisode"] = examples.WeeklyEpisode.map(counts)
    cols = ["WeeklyEpisode", "TuesdaysInEpisode", "WeeklyOrder", "Signal", "Entry", "Exit", "Horizon",
            "PreHedgedSpread", *OUTCOMES]
    return examples[cols]


def nonoverlap(sample):
    rows = []
    for h, group in sample.groupby("Horizon"):
        for method, candidates in [("All", group),
                                   ("First", group.loc[group.WeeklyOrder.eq(1)]),
                                   ("Repeat", group.loc[group.WeeklyOrder.gt(1)])]:
            last_exit = pd.Timestamp.min
            accepted = []
            for row in candidates.itertuples():
                if row.Entry >= last_exit:
                    accepted.append(row.Index)
                    last_exit = row.Exit
            selected = candidates.loc[accepted]
            rows.append({"Horizon": h, "Method": method, "N": len(selected),
                         **selected[OUTCOMES].mean().to_dict()})
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["raw", "events", "snapshot", "weekly-windows", "out"]:
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    snapshot = json.loads(args.snapshot.read_text())
    prices = pd.concat([study.yahoo(args.raw, ticker, True) for ticker in study.ASSETS], axis=1)
    prices = prices.loc[prices.SPY.notna()].sort_index().loc[:"2026-11-13"]
    events = pd.read_excel(args.events, sheet_name="Monetary Events", parse_dates=["Date"])
    panel, bills = study.model_inputs(args.raw, snapshot, prices, events.sort_values("Date"))
    beta = study.trailing_beta(prices, bills)
    labels = labels_for_tuesdays(panel)
    windows = pd.read_csv(args.weekly_windows, parse_dates=["Signal", "Entry", "Exit"])
    sample = matched_windows(windows, labels, prices, bills, beta)
    for h in study.HORIZONS:
        authored = windows.loc[windows.Horizon.eq(h) & windows.Risk.eq("Off") & windows.Fed.eq("Up")]
        assert set(sample.loc[sample.Horizon.eq(h), "Signal"]) == set(authored.Signal)
    labels.rename_axis("Signal").to_csv(args.out / "joint_weekly_states.csv")
    summary(sample).round(8).to_csv(args.out / "first_repeat_summary.csv", index=False)
    paired_repeats(sample).round(8).to_csv(args.out / "first_second_pairs.csv", index=False)
    nonoverlap(sample).round(8).to_csv(args.out / "first_repeat_nonoverlap.csv", index=False)
    historical_examples(labels, sample).round(8).to_csv(args.out / "historical_examples.csv", index=False)
    sample.to_csv(args.out / "joined_windows.csv", index=False)


if __name__ == "__main__":
    main()
