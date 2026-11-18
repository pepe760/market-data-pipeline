"""Replay a 5-percentage-point sector rotation inside the archived SectorETF02.

Run from the replication root with Python, NumPy and pandas:
  python research/sector_rotation/incumbent.py
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import study


def split_cell(value):
    return value.split(", ") if pd.notna(value) and value else []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, default=Path("research"))
    parser.add_argument("--archive", type=Path, default=Path("outputs/mechanism"))
    parser.add_argument("--out", type=Path, default=Path("outputs/sector_rotation"))
    args = parser.parse_args()

    sectors = study.SECTORS
    methods = {"RS": (), **{"+".join(parts): parts for parts in study.SPECS if parts}}
    variants = ["Original", *methods]
    unit = .05
    schedule = pd.read_csv(args.archive / "monthly_decisions.csv", parse_dates=["Signal", "Execution"]).set_index("Execution")
    archive = pd.read_csv(args.research / "monthly_allocations.csv", parse_dates=["signal", "execution"])
    archive = archive.loc[archive.model.eq("Core + regime")].set_index("execution")
    signal_audit = pd.read_csv(args.out / "signal_audit.csv", parse_dates=["Signal"]).set_index("Signal")
    archived = pd.read_csv(args.archive / "portfolio_daily.csv", parse_dates=["Date"], index_col="Date")["Monthly 20D, cap 25%"]
    assets = ["SPY", *sectors, "IJR", "XLC", "XLRE", "GLD", "PAVE", "AIRR", "Cash"]
    raw = args.research / "raw"
    prices = pd.concat([study.prices_from_yahoo(raw, ticker) for ticker in assets[:-1]], axis=1)
    prices = prices.loc[prices.SPY.notna()].sort_index()
    rf = study.series_from_fred(raw, "DTB3").reindex(prices.index).ffill().shift() / 100
    rf *= prices.index.to_series().diff().dt.days / 365
    market = prices.pct_change(fill_method=None).assign(Cash=rf)
    excess = market.sub(rf, axis=0)
    beta = excess.rolling(252).cov(excess.SPY).div(excess.SPY.rolling(252).var(), axis=0)
    beta["Cash"] = 0
    momentum = sum(weight * prices.pct_change(days, fill_method=None)
                   for days, weight in [(63, .4), (126, .2), (189, .2), (252, .2)])
    ranks = momentum[sectors].rank(axis=1, pct=True)
    holdings, picks = {}, []
    for day, row in schedule.iterrows():
        if prices.index[prices.index.get_loc(row.Signal) + 1] != day or archive.at[day, "signal"] != row.Signal:
            raise ValueError(f"Unexpected execution date {day.date()}")
        target = archive.loc[day].reindex(assets).astype(float).fillna(0)
        for order in split_cell(row["25% max holdings"]):
            ticker, weight = order.split()
            amount = float(weight.removesuffix("%")) / 100
            target[ticker] += amount
            target["SPY"] -= amount
        if not np.allclose(target.SPY, row.SPY) or not np.allclose(target.Cash, row.Cash):
            raise ValueError(f"Invalid incumbent ledger on {day.date()}")
        weights = pd.DataFrame([target] * len(variants), index=variants)
        flags = signal_audit.loc[row.Signal, study.FACTORS]
        held = target[sectors].loc[lambda w: w.ge(unit - 1e-12)].index
        donor, choices = "", {}
        if row.Regime == "Risk-On" and len(held) and flags.notna().all():
            rank = ranks.loc[row.Signal]
            donor = rank.loc[held].idxmin()
            rank = rank.drop(donor)
            tilt = pd.DataFrame(0., index=sectors, columns=study.FACTORS)
            tilt.loc[study.DEFENSIVE, "Risk"] = flags.Risk
            tilt.loc[study.CYCLICAL, "Risk"] = -flags.Risk
            tilt.loc[tilt.index.intersection(study.RATE_SENSITIVE), "Fed"] = -flags.Fed
            tilt.loc["XLE", "Macro"] = flags.Macro
            tilt.loc["XLP", "Macro"] = -flags.Macro
            for variant, parts in methods.items():
                recipient = (rank + .25 * tilt.loc[rank.index, list(parts)].sum(axis=1)).idxmax()
                weights.loc[variant, donor] -= unit
                weights.loc[variant, recipient] += unit
                choices[variant] = recipient
        if not np.allclose(weights.sum(axis=1), 1) or weights.min().min() < -1e-12:
            raise ValueError(f"Invalid target weight on {day.date()}")
        if not np.allclose(weights.drop(columns=sectors), target.drop(labels=sectors)):
            raise ValueError(f"Changed nonsector exposure on {day.date()}")
        holdings[day] = weights
        picks.append({"Signal": row.Signal, "Entry": day, "Donor": donor, **choices})
    decisions = pd.DataFrame(picks).set_index("Entry")

    previous = archive.index[archive.index.get_loc(schedule.index[0]) - 1]
    initial = archive.loc[previous].reindex(assets).astype(float).fillna(0)
    warmup = (1 + market.loc[previous:schedule.index[0]].iloc[1:-1].fillna(0)).prod()
    initial_mark = initial.mul(warmup)
    start_weights = pd.DataFrame([initial_mark / initial_mark.sum()] * len(variants), index=variants)
    portfolio, windows = [], []
    for cost in (.0005, .0015):
        out, turnover = [], {}
        weights = start_weights.to_numpy().copy()
        for date, returns in market.loc[archived.index].iterrows():
            missing = returns.isna().to_numpy()
            if (weights[:, missing] > 1e-12).any():
                raise ValueError(f"Missing price while held on {date.date()}")
            marked = weights * (1 + returns.fillna(0).to_numpy())
            gross = marked.sum(axis=1)
            weights = marked / gross[:, None]
            if date in holdings:
                targets = holdings[date].to_numpy()
                traded = np.abs(targets[:, :-1] - weights[:, :-1]).sum(axis=1)
                turnover[date] = traded
                gross *= 1 - cost * traded
                weights = targets
            out.append(gross - 1)
        daily = pd.DataFrame(out, index=archived.index, columns=variants)
        if cost == .0005:
            np.testing.assert_allclose(daily.Original, archived, atol=1e-12, rtol=0)
        for entry, exit_ in zip(schedule.index[:-1], schedule.index[1:], strict=True):
            signal = schedule.at[entry, "Signal"]
            asset_return = (1 + market.loc[entry:exit_].iloc[1:].fillna(0)).prod() - 1
            gross = 1 + holdings[entry] @ asset_return
            realized = gross.mul(1 - cost * turnover[entry]) - 1
            difference = holdings[entry][sectors].sub(holdings[entry].loc["RS", sectors], axis=1)
            beta_gap = difference @ beta.loc[signal, sectors]
            neutral = realized - realized.RS - beta_gap * (asset_return.SPY - asset_return.Cash)
            for variant in variants:
                windows.append({
                    "CostBps": cost * 10000, "Entry": entry, "Exit": exit_, "Signal": signal,
                    "Variant": variant, "Donor": decisions.at[entry, "Donor"],
                    "Recipient": decisions.at[entry, variant] if variant in methods else "",
                    "RSRecipient": decisions.at[entry, "RS"], "Return": realized[variant],
                    "ActiveVsRS": realized[variant] - realized.RS, "HedgedVsRS": neutral[variant],
                })
        for name, begin in [("Full", archived.index[0]), ("From2018", "2018-01-01"), ("From2022", "2022-01-01")]:
            returns = daily.loc[begin:]
            years = (returns.index[-1] - market.index[market.index.get_loc(returns.index[0]) - 1]).days / 365.2425
            nav = (1 + returns).cumprod()
            cagr = nav.iloc[-1].pow(1 / years) - 1
            mdd = (nav / nav.cummax().clip(lower=1) - 1).min()
            vol = returns.std() * np.sqrt(252)
            sharpe = returns.sub(rf, axis=0).mean() * 252 / vol
            for variant in variants:
                portfolio.append({
                    "CostBps": cost * 10000, "Sample": name, "Variant": variant,
                    "FirstDate": returns.index[0], "LastDate": returns.index[-1], "CAGR": cagr[variant],
                    "CAGRVsRS": cagr[variant] - cagr.RS, "CAGRVsOriginal": cagr[variant] - cagr.Original,
                    "MDD": mdd[variant], "Sharpe": sharpe[variant],
                })
    windows = pd.DataFrame(windows)
    original = windows.loc[windows.Variant.eq("Original")].set_index(["CostBps", "Entry"]).HedgedVsRS
    windows = windows.join(original.rename("OriginalHedged"), on=["CostBps", "Entry"])
    windows["HedgedVsOriginal"] = windows.HedgedVsRS - windows.pop("OriginalHedged")
    portfolio = pd.DataFrame(portfolio)
    original = windows.loc[windows.Variant.eq("Original")].set_index(["CostBps", "Entry"]).HedgedVsRS
    windows["HedgedVsOriginal"] = windows.HedgedVsRS - original.reindex(
        pd.MultiIndex.from_frame(windows[["CostBps", "Entry"]])
    ).to_numpy()
    for i, row in portfolio.iterrows():
        matched = windows.loc[
            windows.CostBps.eq(row.CostBps) & windows.Variant.eq(row.Variant) & windows.Entry.ge(row.FirstDate)
        ]
        low, high = study.boot(matched.HedgedVsRS, 1)
        portfolio.loc[i, ["CompletedMonths", "EligibleMonths", "HoldingsChangedVsRS", "AnnHedgedVsRS", "CI95Low", "CI95High"]] = [
            len(matched), matched.Donor.ne("").sum(),
            (matched.Recipient.fillna("") != matched.RSRecipient.fillna("")).sum(),
            matched.HedgedVsRS.mean() * 12, low, high,
        ]
        original_low, original_high = study.boot(matched.HedgedVsOriginal, 1)
        portfolio.loc[i, ["AnnHedgedVsOriginal", "OriginalCI95Low", "OriginalCI95High"]] = [
            matched.HedgedVsOriginal.mean() * 12, original_low, original_high,
        ]
        low, high = study.boot(matched.HedgedVsOriginal, 1)
        portfolio.loc[i, ["AnnHedgedVsOriginal", "OriginalCI95Low", "OriginalCI95High"]] = [
            matched.HedgedVsOriginal.mean() * 12, low, high
        ]
    portfolio.to_csv(args.out / "incumbent_metrics.csv", index=False)
    windows.to_csv(args.out / "incumbent_windows.csv", index=False)


if __name__ == "__main__":
    main()
