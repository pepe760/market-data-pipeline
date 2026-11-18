"""Sector rotation ablation. Run with Python, NumPy, pandas and openpyxl."""

import argparse
import hashlib
import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd

SECTORS = "XLB XLE XLF XLI XLK XLP XLU XLV XLY".split()
EXTRA = ["XLRE", "XLC"]
DEFENSIVE = ["XLP", "XLV", "XLU"]
CYCLICAL = ["XLK", "XLY", "XLI"]
GROWTH = ["XLY", "XLI", "XLB"]
RATE_SENSITIVE = ["XLK", "XLY", "XLU", "XLRE", "XLC"]
FACTORS = ["Risk", "Fed", "Macro"]
SPECS = [s for n in range(4) for s in itertools.combinations(FACTORS, n)]
SEED = 20261118


def prices_from_yahoo(path, ticker):
    data = json.loads((path / f"{ticker}.json").read_text())["chart"]["result"][0]
    key = data["indicators"]
    values = key["quote"][0]["close"] if ticker == "%5EVIX" else key["adjclose"][0]["adjclose"]
    dates = pd.to_datetime(data["timestamp"], utc=True, unit="s").tz_convert("America/New_York")
    return pd.Series(values, index=dates.tz_localize(None).normalize(), name=ticker).dropna().sort_index()


def series_from_fred(path, ticker):
    return pd.read_csv(path / f"{ticker}.csv", index_col=0, parse_dates=True)[ticker].dropna()


def as_known(series, days, lag):
    return series.reindex(days, method="ffill").shift(lag)


def market_panel(raw, days, price, event_path, pit_oil, pit_claims):
    vix = prices_from_yahoo(raw, "%5EVIX").reindex(days)
    credit = as_known(series_from_fred(raw, "BAA10Y"), days, 2)
    wti = as_known(series_from_fred(raw, "DCOILWTICO"), days, 3)
    rates2 = as_known(series_from_fred(raw, "DGS2"), days, 2)
    real10 = as_known(series_from_fred(raw, "DFII10"), days, 2)
    events = pd.read_excel(event_path, sheet_name="Monetary Events", parse_dates=["Date"])
    path_move = events[["ED2", "ED3", "ED4"]].mean(axis=1, skipna=False)
    events = events.assign(Path=path_move).dropna(subset=["Path"]).sort_values("Date")
    days_for_events = days.searchsorted(events.Date)
    in_calendar = days_for_events < len(days)
    published = pd.Series(events.loc[in_calendar, "Path"].to_numpy(), index=days[days_for_events[in_calendar]])
    cumulative = published.groupby(level=0).sum().reindex(days, fill_value=0).cumsum()
    if not price.loc[days, "SPY"].notna().all():
        raise ValueError("SPY calendar has missing closes")

    # Drop the last observed month, which may end on an incomplete snapshot.
    signals = days.to_series().groupby(days.to_period("M")).last().iloc[:-1].to_numpy()
    monthly = pd.DataFrame(index=pd.DatetimeIndex(signals, name="Signal"))
    monthly["VixChange"] = vix.diff(21).reindex(signals)
    monthly["CreditChange"] = credit.diff(21).reindex(signals)
    monthly["Risk"] = np.select(
        [monthly[["VixChange", "CreditChange"]].gt(0).all(axis=1),
         monthly[["VixChange", "CreditChange"]].lt(0).all(axis=1)], [1, -1], default=0
    )
    monthly.loc[monthly[["VixChange", "CreditChange"]].isna().any(axis=1), "Risk"] = np.nan
    monthly["FedPathPP"] = cumulative.shift(1).reindex(signals).diff(3)
    monthly["Fed"] = np.sign(monthly.FedPathPP).where(monthly.FedPathPP.abs() >= .05, 0)
    monthly.loc[monthly.FedPathPP.isna(), "Fed"] = np.nan
    monthly["WTIChange"] = (wti.reindex(signals).diff(3) / wti.reindex(signals).shift(3).abs()).to_numpy()
    monthly["Macro"] = np.sign(monthly.WTIChange)
    monthly["TwoYearChange"] = rates2.reindex(signals).diff(3).to_numpy()
    monthly["RealYieldChange"] = real10.reindex(signals).diff(3).to_numpy()
    oil = pd.read_csv(pit_oil, index_col="Signal", parse_dates=["Signal", "AsOf", "PriorAsOf"])
    claims = pd.read_csv(pit_claims, index_col="Signal", parse_dates=["Signal", "Cutoff", "PriorCutoff"])
    cutoff = pd.Series(days[days.get_indexer(monthly.index) - 1], index=monthly.index)
    for panel, date, value in [(oil, "AsOf", "WTIChangePct"), (claims, "Cutoff", "Claims")]:
        known = panel.join(cutoff.rename("MaximumDate"), how="inner").dropna(subset=[value])
        if not known[date].le(known.MaximumDate).all():
            raise ValueError(f"Look-ahead in {value}")
    monthly["VintageWTI"] = oil.WTIChangePct / 100
    monthly["Growth"] = claims.Claims
    return monthly, events


def portfolio_weights(rs, sectors, signal, assets, expanded):
    score = rs[sectors].rank(method="average", pct=True)
    tilt = pd.DataFrame(0.0, index=sectors, columns=FACTORS)
    tilt.loc[DEFENSIVE, "Risk"] = signal.Risk
    tilt.loc[CYCLICAL, "Risk"] = -signal.Risk
    tilt.loc[tilt.index.intersection(RATE_SENSITIVE), "Fed"] = -signal.Fed
    tilt.loc["XLE", "Macro"] = signal.Macro
    tilt.loc["XLP", "Macro"] = -signal.Macro
    if expanded:
        # Split the same macro budget between input costs and economic activity.
        tilt["Macro"] *= .5
        tilt.loc[GROWTH, "Macro"] += .5 * np.sign(signal.Growth)
        tilt.loc[DEFENSIVE, "Macro"] -= .5 * np.sign(signal.Growth)
    weights = {}
    for parts in SPECS:
        ranking = score.add(.25 * tilt[list(parts)].sum(axis=1))
        pick = ranking.sort_values(ascending=False, kind="stable").head(3).index
        target = pd.Series(0., index=assets)
        target["SPY"] = .8
        target[pick] = .2 / 3
        weights["+".join(parts) or "RS"] = target
    return pd.DataFrame(weights)


def balance(prices, cash, monthly, rs, beta, phases, horizon, expanded, start, cost):
    sectors = SECTORS + (EXTRA if expanded else [])
    assets = ["SPY", *sectors]
    days = prices.index
    signals = monthly.loc[monthly.index.month.isin(phases)]
    frames, intervals, trades = [], [], []
    prior = None
    for signal, row in signals.iterrows():
        if signal < start:
            continue
        next_month = signal.to_period("M") + horizon
        exits = signals.index[signals.index.to_period("M") == next_month]
        if exits.empty:
            continue
        exit_signal = exits[0]
        entry = days[days.get_loc(signal) + 1]
        exit_ = days[days.get_loc(exit_signal) + 1]
        if expanded:
            row = row.copy()
            row.Macro = np.sign(row.VintageWTI)
        required = [*FACTORS, "Growth"] if expanded else FACTORS
        if row[required].isna().any() or rs.loc[signal, sectors].isna().any():
            continue
        trailing = beta.loc[signal, assets]
        if trailing.isna().any():
            continue
        window = prices.loc[entry:exit_, assets]
        if window.isna().any().any():
            raise ValueError(f"Missing prices in {entry.date()}–{exit_.date()}")
        weights = portfolio_weights(rs.loc[signal], sectors, row, assets, expanded)
        if prior is not None and prior[0] != entry:
            # Keep skipped vintage periods as explicit gaps and restart exposure.
            prior = None
        previous = prior[1] if prior is not None else weights * 0
        turnover = (weights - previous).abs().sum()
        fee = cost * turnover
        relatives = window.div(window.iloc[0])
        nav = relatives @ weights
        cash_return = (1 + cash.reindex(window.index).iloc[1:]).prod() - 1
        market_excess = relatives.SPY.iloc[-1] - 1 - cash_return
        hedges = trailing @ weights
        period_return = nav.iloc[-1].mul(1 - fee) - 1
        active = period_return - period_return.RS
        hedged = active - (hedges - hedges.RS) * market_excess
        # Charge entry fees before earning the next session's close-to-close move.
        daily = nav.pct_change(fill_method=None).iloc[1:]
        daily.iloc[0] = (1 + daily.iloc[0]) * (1 - fee) - 1
        prior_weights = weights.mul(relatives.iloc[-1], axis=0).div(nav.iloc[-1])
        prior = (exit_, prior_weights)
        frames.append(daily.assign(Signal=signal))
        intervals.append(pd.DataFrame({
            "Signal": signal, "Entry": entry, "Exit": exit_, "Return": period_return,
            "Active": active, "HedgedActive": hedged, "Beta": hedges, "SPYExcess": market_excess,
            "Turnover": turnover, "Risk": row.Risk, "Fed": row.Fed, "Macro": row.Macro,
            "Growth": row.Growth,
        }).rename_axis("Variant").reset_index())
        trades.append(weights.rename_axis("Ticker").reset_index().assign(Signal=signal, Entry=entry, Exit=exit_))
    if not frames:
        raise ValueError(f"No executable periods for phase {phases}, horizon {horizon}, expanded={expanded}")
    return pd.concat(frames), pd.concat(intervals, ignore_index=True), pd.concat(trades, ignore_index=True)


def samples(data):
    return [
        ("Full", data),
        ("Before2018", data.loc[data.Exit.lt("2018-01-01")]),
        ("From2018", data.loc[data.Signal.ge("2018-01-01")]),
        ("From2022", data.loc[data.Signal.ge("2022-01-01")]),
    ]


def boot(values, horizon):
    data = np.asarray(values, dtype=float)
    if len(data) < 8:
        return np.nan, np.nan
    block = 12 // horizon
    rng = np.random.default_rng(SEED)
    starts = rng.integers(0, len(data), size=(5000, int(np.ceil(len(data) / block)), 1))
    draws = ((starts + np.arange(block)) % len(data)).reshape(5000, -1)[:, :len(data)]
    return np.quantile(data[draws].mean(axis=1) * (12 / horizon), [.025, .975])


def metrics(daily, intervals, cash, config, horizon, cost):
    rows = []
    for cut, sample in samples(intervals):
        annualizer = 12 / horizon
        gain = daily.loc[daily.Signal.isin(sample.Signal)].drop(columns="Signal")
        rf = cash.loc[gain.index]
        for variant, group in sample.groupby("Variant", sort=False):
            result = gain[variant]
            # Geometric rate over invested trading sessions, including any separated sample blocks.
            cagr = np.expm1(np.log1p(result).mean() * 252)
            nav = (1 + result).cumprod()
            mdd = (nav / nav.cummax().clip(lower=1) - 1).min()
            vol = result.std() * np.sqrt(252)
            hedged = group.HedgedActive
            low, high = boot(hedged, horizon)
            realized_beta = hedged.cov(group.SPYExcess) / group.SPYExcess.var()
            rows.append(dict(Config=config, Horizon=horizon, CostBps=cost * 10000, Sample=cut,
                             Variant=variant, N=len(group), FirstEntry=group.Entry.min(), LastExit=group.Exit.max(),
                             CAGR=cagr, MDD=mdd, Sharpe=(result - rf).mean() * 252 / vol,
                             Volatility=vol, AnnHedged=hedged.mean() * annualizer,
                             HedgeCI95Low=low, HedgeCI95High=high, RemainingBeta=realized_beta,
                             AnnualTurnover=group.Turnover.mean() * annualizer))
    return pd.DataFrame(rows)


def diagnostics(price, cash, beta, signals):
    def endpoint(signal, h):
        return (signal.to_period("M") + h).end_time.normalize()

    days = price.index
    rows = []
    for h in [3, 12]:
        months = {2, 5, 8, 11} if h == 3 else {11}
        for signal, row in signals.loc[signals.index.month.isin(months)].iterrows():
            cutoff = endpoint(signal, h)
            after = days.searchsorted(cutoff, side="right")
            if after >= len(days):
                continue
            entry, exit_ = days[days.get_loc(signal) + 1], days[after]
            rf = (1 + cash.loc[entry:exit_].iloc[1:]).prod() - 1
            raw = price.loc[exit_] / price.loc[entry] - 1
            residual = raw - rf - beta.loc[signal] * (raw.SPY - rf)
            for basket, selection in {"Defense-Cyclical": DEFENSIVE + CYCLICAL,
                                      "Growth-Defense": GROWTH + DEFENSIVE,
                                      "XLE-XLP": ["XLE", "XLP"],
                                      "XLP-DefensePeers": ["XLP", "XLV", "XLU"]}.items():
                a = 1 if basket in {"XLE-XLP", "XLP-DefensePeers"} else 3
                for series, ret in [("BetaAdjusted", residual), ("Raw", raw)]:
                    vals = ret[selection]
                    value = vals.iloc[:a].mean() - vals.iloc[a:].mean() if vals.notna().all() else np.nan
                    rows.append(dict(Signal=signal, Entry=entry, Exit=exit_, Horizon=h, Basket=basket,
                                     Basis=series, Return=value, SPY=raw.SPY,
                                     Risk=row.Risk, Fed=row.Fed, Macro=row.Macro,
                                     VintageWTI=row.VintageWTI, Growth=row.Growth))
    return pd.DataFrame(rows)


def event_window(price, cash, beta, entry, exit_, beta_day):
    ret = price.loc[exit_].div(price.loc[entry]) - 1
    rf = (1 + cash.loc[entry:exit_].iloc[1:]).prod() - 1
    return ret, ret - rf - beta.loc[beta_day] * (ret.SPY - rf)


def risk_windows(price, cash, beta, monthly):
    days = price.index
    rows = []
    for signal, row in monthly.loc["2001-06-01":].iterrows():
        if pd.isna(row.Risk):
            continue
        pos = days.get_loc(signal)
        windows = [("Signal build-up", pos - 21, pos, pos - 21)]
        for h in [21, 63, 252]:
            if pos + 1 + h < len(days):
                windows.append((str(h), pos + 1, pos + 1 + h, pos))
        for name, a, b, hedge in windows:
            raw, adjusted = event_window(price, cash, beta, days[a], days[b], days[hedge])
            spread = raw[DEFENSIVE].mean() - raw[CYCLICAL].mean()
            neutral = adjusted[DEFENSIVE].mean() - adjusted[CYCLICAL].mean()
            rows.append(dict(Signal=signal, Entry=days[a], Exit=days[b], Window=name, Risk=int(row.Risk),
                             SPY=raw.SPY, Defense=raw[DEFENSIVE].mean(), Cyclical=raw[CYCLICAL].mean(),
                             RawSpread=spread, HedgeSpread=neutral))
    return pd.DataFrame(rows)


def fed_windows(price, cash, beta, events, raw):
    days = price.index
    rates = pd.concat([series_from_fred(raw, "DFEDTAR"), series_from_fred(raw, "DFEDTARU")]).sort_index()
    moves = rates.diff()
    events = events.loc[events.Date.ge(days[0])].copy()
    actual = moves.reindex(events.Date).to_numpy() + moves.reindex(events.Date + pd.Timedelta(days=1)).to_numpy()
    events["RateMovePP"] = actual
    rows = []
    for event in events.itertuples(index=False):
        pos = days.searchsorted(event.Date)
        if pos == len(days):
            continue
        for h in [63, 252]:
            if pos + 1 + h >= len(days):
                continue
            entry, exit_ = days[pos + 1], days[pos + 1 + h]
            returns, residuals = event_window(price, cash, beta, entry, exit_, days[pos])
            for ticker in [*SECTORS, *EXTRA, "DEF"]:
                values = DEFENSIVE if ticker == "DEF" else [ticker]
                if residuals[values].isna().any():
                    continue
                rows.append(dict(Signal=event.Date, Entry=entry, Exit=exit_, Days=h, Ticker=ticker,
                                 RateMovePP=event.RateMovePP, PathPP=event.Path, MP1=event.MP1,
                                 Scheduled=event.Unscheduled == 0, Relative=returns[values].mean() - returns.SPY,
                                 BetaAdjusted=residuals[values].mean()))
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ["raw", "events", "pit-oil", "pit-claims", "out"]:
        parser.add_argument(f"--{arg}", required=True, type=Path)
    parser.add_argument("--end", default="2026-11-13")
    args = parser.parse_args()
    raw, out = args.raw, args.out
    out.mkdir(parents=True, exist_ok=True)
    price = pd.concat([prices_from_yahoo(raw, t) for t in ["SPY", *SECTORS, *EXTRA]], axis=1)
    price = price.loc[price.SPY.notna()].sort_index().loc[:args.end]
    days = price.index
    cash = as_known(series_from_fred(raw, "DTB3"), days, 2).div(100)
    cash *= days.to_series().diff().dt.days / 365
    excess = price.pct_change(fill_method=None).sub(cash, axis=0)
    beta = excess.rolling(252).cov(excess.SPY).div(excess.SPY.rolling(252).var(), axis=0)
    rs = sum(w * price.pct_change(d, fill_method=None) for d, w in [(63, .4), (126, .2), (189, .2), (252, .2)])
    rs = rs.sub(rs.SPY, axis=0)
    monthly, events = market_panel(raw, days, price, args.events, args.pit_oil, args.pit_claims)

    summaries = []
    for cfg, expanded, horizon, phases in [
        ("9Sectors", False, 3, (2, 5, 8, 11)), ("9Sectors_Alt1", False, 3, (1, 4, 7, 10)),
        ("9Sectors_Alt2", False, 3, (3, 6, 9, 12)), ("9Sectors", False, 12, (11,)),
        ("11Sectors_PIT", True, 3, (2, 5, 8, 11)), ("11Sectors_PIT", True, 12, (11,)),
    ]:
        for cost in [.0005, .0015]:
            daily, intervals, holdings = balance(price, cash, monthly, rs, beta, phases, horizon,
                                                  expanded, pd.Timestamp("2001-06-01"), cost)
            summaries.append(metrics(daily, intervals, cash, cfg, horizon, cost))
            if cost == .0005:
                stem = f"{cfg}_{horizon}m"
                daily.to_csv(out / f"{stem}_daily.csv")
                intervals.to_csv(out / f"{stem}_intervals.csv", index=False)
                holdings.to_csv(out / f"{stem}_weights.csv", index=False)
    pd.concat(summaries).to_csv(out / "metrics.csv", index=False)
    diagnostics(price, cash, beta, monthly).to_csv(out / "mechanism_windows.csv", index=False)
    risk_windows(price, cash, beta, monthly).to_csv(out / "risk_windows.csv", index=False)
    fed_windows(price, cash, beta, events, raw).to_csv(out / "fed_windows.csv", index=False)
    monthly.to_csv(out / "signal_audit.csv")
    files = [args.events, args.pit_oil, args.pit_claims, *sorted(raw.glob("*.csv")),
             *(raw / f"{t}.json" for t in ["SPY", "%5EVIX", *SECTORS, *EXTRA])]
    (out / "manifest.json").write_text(json.dumps({
        "LastPriceDate": str(days[-1].date()), "MethodsVersion": 1,
        "SHA256": {str(f): hashlib.sha256(f.read_bytes()).hexdigest() for f in files},
    }, indent=2))


if __name__ == "__main__":
    main()
