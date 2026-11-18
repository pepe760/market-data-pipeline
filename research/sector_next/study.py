"""Evaluate one operating or financing indicator per ETF on matched beta-hedged windows."""

import json
import argparse
from math import erfc
from pathlib import Path

import numpy as np
import pandas as pd


CONTROLS = ['RS3','RS20','RiskOff']
BASE_MACRO = {'SOXX':['Chip production'], 'ITB':['Mortgage change','Permits'], 'XLF':['Credit'], 'IJR':['Credit']}
TARGET = 'Frozen 252d'
TRAINING_MINIMUM = 36
NONOVERLAPPING = {3: ('Fixed quarterly', {2, 5, 8, 11}), 12: ('Fixed annual', {11})}


def regression(train, row, features, new=None, clipped=False):
    mean = train[features].mean()
    sd = train[features].std(ddof=0)
    active = list(sd.index[sd > 1e-12])
    x = (train[active]-mean[active])/sd[active]
    xp = (row[active]-mean[active])/sd[active]
    if clipped and new in active:
        x[new] = x[new].clip(-3,3)
        xp[new] = np.clip(xp[new],-3,3)
    model = np.linalg.lstsq(np.c_[np.ones(len(x)),x],train[TARGET].to_numpy(),rcond=None)[0]
    slope = model[1+active.index(new)] if new in active else np.nan
    return float(np.r_[1,xp.to_numpy(dtype=float)]@model),float(slope)


def sales_signal(events, sessions):
    if events is None:
        return pd.DataFrame(columns=['ETF','Signal','Factor','Value','Observation','AsOf','SourceURL'])
    data = pd.read_csv(events,parse_dates=['observation_month','release_date']).sort_values(['release_date','observation_month'])
    data['observation_month'] = data.observation_month + pd.offsets.MonthEnd(0)
    data['yoy'] = pd.to_numeric(data.yoy,errors='raise')
    assert data.release_date.ge(data.observation_month).all() and data.yoy.abs().lt(4).all()
    rows=[]
    for signal,cutoff in sessions[['Signal','Cutoff']].itertuples(index=False,name=None):
        known=data.loc[data.release_date<=cutoff]
        if known.empty:
            continue
        obs=known.iloc[-1]
        # Require a timely observation, never substitute revised billings for a missing press release.
        if (cutoff-obs.observation_month).days>100:
            continue
        rows.append(dict(ETF='SOXX',Signal=signal,Factor='GlobalChipSalesYoY',Value=obs.yoy,Observation=obs.observation_month,
                         AsOf=obs.release_date,SourceURL=obs.source_url))
    return pd.DataFrame(rows, columns=['ETF','Signal','Factor','Value','Observation','AsOf','SourceURL'])


def inference(d, lags):
    delta=d.loss_b-d.loss_c
    period=d.Signal.dt.to_period('M').astype('int64').to_numpy()
    scores=np.zeros(period.max()-period.min()+1)
    scores[period-period.min()]=(delta-delta.mean()).to_numpy()
    cov=scores@scores
    for lag in range(1,lags+1):
        cov+=2*(1-lag/(lags+1))*(scores[lag:]@scores[:-lag])
    return np.sqrt(max(cov,0)*len(d)/(len(d)-1))/len(d)


def row_metrics(data, group, lags):
    fields = ['ETF','Factor','Version']
    res = {field:data.iloc[0][field] for field in fields}
    best = (data.loss_b-data.loss_c).idxmax()
    rest = data.drop(index=best)
    gain=(data.loss_b-data.loss_c).mean()
    se=inference(data,lags) if len(data)>=12 else np.nan
    p=erfc(abs(gain)/(se*np.sqrt(2))) if np.isfinite(se) and se>0 else np.nan
    return dict(**res,Sample=group,N=len(data),FirstSignal=data.Signal.min(),LastSignal=data.Signal.max(),
                VsA=1-data.loss_c.sum()/data.loss_a.sum(),VsB=1-data.loss_c.sum()/data.loss_b.sum(),
                VsMean=1-data.loss_c.sum()/data.loss_mean.sum(),
                BvsMean=1-data.loss_b.sum()/data.loss_mean.sum(),
                VsBWithoutBest=1-rest.loss_c.sum()/rest.loss_b.sum(),
                VsMeanWithoutBest=1-rest.loss_c.sum()/rest.loss_mean.sum(),
                MedianSlopeBp=data.Slope.median()*10000,PositiveSlope=(data.Slope>0).mean(),P=p)


def forecasts(joined, ticker, clipped):
    old = [*CONTROLS, *BASE_MACRO[ticker]]
    required = [*old,'Value',TARGET]
    rows=joined.dropna(subset=required).sort_values('Signal')
    values=[]
    for _,row in rows.iterrows():
        train=rows.loc[rows.Exit<=row.Signal]
        if len(train)<TRAINING_MINIMUM:
            continue
        assert train.Exit.max()<=row.Signal<row.Entry<row.Exit
        candidate, slope=regression(train,row,old+['Value'],new='Value',clipped=clipped)
        values.append(dict(ETF=ticker,Version='Training ±3 SD' if clipped else 'Unclipped',Factor=row.Factor,
                           Signal=row.Signal,Entry=row.Entry,Exit=row.Exit,Actual=row[TARGET],TrainN=len(train),
                           LatestTrainingExit=train.Exit.max(),Eligible=row.StrategyEligible,
                           A=regression(train,row,CONTROLS)[0],B=regression(train,row,old)[0],
                           C=candidate,Mean=train[TARGET].mean(),Slope=slope))
    return pd.DataFrame(values)


def run(args):
    args.out.mkdir(parents=True,exist_ok=True)
    disjoint_sample, disjoint_months = NONOVERLAPPING[args.horizon]
    seed=pd.read_csv(args.targets,parse_dates=['Signal','Entry','Exit'])
    seed=seed.loc[seed.ETF.isin(BASE_MACRO)&seed.Horizon.eq(args.horizon)&seed.Entry.ge('2017-04-03')].copy()
    sessions=pd.DatetimeIndex(pd.read_csv(args.calendar,usecols=['Date'],parse_dates=['Date']).Date)
    indices=sessions.get_indexer(seed.Signal)
    assert min(indices)>=1
    seed['Cutoff']=sessions[indices-1]
    us=pd.read_csv(args.us,parse_dates=['Signal','AsOf'])
    chip=sales_signal(args.sia,seed.loc[seed.ETF.eq('SOXX')])
    sources=pd.concat([us,chip],ignore_index=True) if not chip.empty else us
    joined=seed.merge(sources,on=['ETF','Signal'],how='left',validate='one_to_one')
    valid=joined.Value.notna()
    assert joined.loc[valid,'AsOf'].notna().all() and (joined.loc[valid,'AsOf']<=joined.loc[valid,'Cutoff']).all()
    joined.to_csv(args.out/'matched_inputs.csv',index=False)
    metrics, rows, coverage = [], [], []
    for ticker,g in joined.groupby('ETF'):
        available = g.loc[g.Value.notna()]
        paired = available.dropna(subset=[*CONTROLS, *BASE_MACRO[ticker], TARGET])
        coverage.append(dict(ETF=ticker,RawLabels=len(g),UsableFactors=len(available),PairedLabels=len(paired),
                             DistinctObservations=available.Observation.nunique(),FirstFactor=available.Signal.min(),
                             LastFactor=available.Signal.max()))
        for clipped in (True,False):
            result=forecasts(g,ticker,clipped)
            if result.empty:
                continue
            for f in ['A','B','C','Mean']:
                result['loss_'+f.lower()] = (result.Actual-result[f])**2
            disjoint = result.loc[result.Signal.dt.month.isin(disjoint_months)]
            assert disjoint.Entry.ge(disjoint.Exit.shift()).iloc[1:].all()
            subsets={'Monthly': result,disjoint_sample:disjoint,
                     'Matured by end-2021':result.loc[result.Exit<'2022-01-01'],
                     'Signals since 2022':result.loc[result.Signal>='2022-01-01'],
                     'Strategy eligible (diagnostic)':result.loc[result.Eligible]}
            for label, subset in subsets.items():
                if len(subset)>=2:
                    metrics.append(row_metrics(subset,label,max(3,args.horizon-1)))
            rows.append(result)
    candidates=pd.DataFrame(metrics)
    if candidates.empty:
        raise ValueError('No prediction cohort was available')
    candidates['Holm4']=np.nan
    primary=candidates.loc[candidates.Version.eq('Training ±3 SD')&candidates.Sample.eq('Monthly')]
    sorted_index=primary.P.sort_values().index
    candidate_count=len(BASE_MACRO)
    candidates.loc[sorted_index,'Holm4']=np.maximum.accumulate(np.minimum(1,primary.loc[sorted_index,'P']*(candidate_count-np.arange(len(sorted_index)))))
    pd.DataFrame(coverage).to_csv(args.out/'coverage.csv',index=False)
    candidates.to_csv(args.out/'results.csv',index=False)
    pd.concat(rows,ignore_index=True).to_csv(args.out/'predictions.csv',index=False)
    print(pd.DataFrame(coverage).to_string(index=False))
    print(candidates.loc[candidates.Sample.isin(['Monthly',disjoint_sample])].round(5).to_string(index=False))
    receipt={'candidate_count':candidate_count,'horizon_months':args.horizon,'primary_training_min':TRAINING_MINIMUM,
             'primary_new_factor_cap_sd':3,'disjoint_signal_months':sorted(disjoint_months),
             'last_exit':str(seed.Exit.max().date()),'source_rows':int(valid.sum())}
    (args.out/'manifest.json').write_text(json.dumps(receipt,indent=2)+'\n')


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--targets', type=Path, required=True, help='SectorETF02 beta_hedged_windows.csv')
    cli.add_argument('--calendar', type=Path, required=True, help='US trading sessions CSV with a Date column')
    cli.add_argument('--us', type=Path, required=True,
                     help='ITB, XLF and IJR CSV with ETF, Signal, Factor, Value, Observation, AsOf, SourceURL')
    cli.add_argument('--sia', type=Path,
                     help='SIA press releases CSV with observation_month, release_date, yoy (decimal), source_url')
    cli.add_argument('--out', type=Path, required=True, help='Output directory for results and verification data')
    cli.add_argument('--horizon', type=int, choices=NONOVERLAPPING, default=3)
    run(cli.parse_args())
