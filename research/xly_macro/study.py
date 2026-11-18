"""One-factor-at-a-time additions to the frozen XLY three-month forecast."""

import argparse
from pathlib import Path
import json
from math import erfc

import numpy as np
import pandas as pd
from sources import LIQUIDITY_IDS, VINTAGE_IDS, trading_sessions


OUT = Path('../outputs/xly_macro_study')
INPUTS = Path('../outputs/xly_macro_inputs')
SEED = Path('../outputs/xly_forward/training_seed.csv')
BASE = ['RS3','RS20','RiskOff','2Y yield']
Y = 'Frozen 252d'
PHASE = (11,2,5,8)
FACTORS = {'Curve': 'Curve steepening', 'RealYield': '10-year real yield decline',
           'Credit': 'Baa spread tightening', 'Claims': 'Initial claims declining',
           'CoreCPI': 'Core inflation decelerating', 'Payrolls': 'Payroll growth',
           'Reserves': 'Reserve balance growth', 'DurablePCE': 'Real durable goods spending growth'}
VINTAGE_LABELS = dict(zip(VINTAGE_IDS, ['Claims','CoreCPI','Payrolls','Reserves','DurablePCE']))
DAILY_LABELS = {'T10Y2Y': ('Curve',1), 'DFII10': ('RealYield',-1), 'BAA10Y': ('Credit',-1)}
BUNDLE = 'Ridge bundle'


def vintage_value(observations, name, cutoff):
    """Transform a single historical release snapshot without revised-value substitution."""
    s = observations.loc[:cutoff].dropna()
    if s.empty or (cutoff-s.index[-1]).days > (21 if name in ('ICSA','WRESBAL') else 95):
        return np.nan
    if name in ('ICSA','WRESBAL'):
        current = s.reindex(pd.date_range(end=s.index[-1],periods=4,freq='7D'))
        prior = s.reindex(current.index-pd.Timedelta(weeks=13))
        if current.isna().any() or prior.isna().any() or prior.mean()<=0:
            return np.nan
        return (-1 if name=='ICSA' else 1) * (current.mean()/prior.mean()-1)
    monthly = s.set_axis(s.index.to_period('M'))
    date = monthly.index[-1]
    intervals = [0,3,6] if name=='CPILFESL' else [0,3]
    values = monthly.reindex([date-gap for gap in intervals]).to_numpy()
    if not np.isfinite(values).all() or min(values)<=0:
        return np.nan
    gain = np.log(values[0]/values[1])
    if name=='CPILFESL':
        return 4*(np.log(values[1]/values[2])-gain)
    return gain


def attach_daily(seeds, sessions, daily_directory):
    ends = sessions.to_series().groupby(sessions.to_period('M')).last()
    signal_index = pd.DatetimeIndex(seeds.Signal)
    cut = sessions[sessions.get_indexer(signal_index)-1]
    previous = pd.DatetimeIndex([ends[d.to_period('M')-3] for d in signal_index])
    older = sessions[sessions.get_indexer(previous)-1]
    assert (cut<signal_index).all() and (older<previous).all()
    panel = seeds.assign(Cutoff=cut, PriorCutoff=older)
    sources = []
    for name, (factor, sign) in DAILY_LABELS.items():
        series = pd.read_csv(daily_directory/(name+'.csv'),index_col=0,parse_dates=True)[name].dropna()
        recent = series.reindex(cut,method='ffill').to_numpy()
        past = series.reindex(older,method='ffill').to_numpy()
        panel[factor] = sign*(recent-past)/100
        sources.append(dict(Factor=factor,Series=name,Version='Daily archive; previous-session cutoff',Rows=int(panel[factor].notna().sum())))
    return panel,sources


def feature_panel(args):
    seeds = pd.read_csv(args.seed,parse_dates=['Signal','Entry','Exit']).sort_values('Signal')
    sessions = trading_sessions(args.seed,args.calendar_archive)
    panel,sources = attach_daily(seeds,sessions,args.daily_dir)
    for name, factor in VINTAGE_LABELS.items():
        values = {}
        obs_days = {}
        for file in sorted(args.inputs.glob(f'{name}_*.csv')):
            data = pd.read_csv(file,index_col=0,parse_dates=True)
            for column in data:
                day=pd.to_datetime(column.rsplit('_',1)[1],format='%Y%m%d')
                if day in values:
                    raise ValueError(f'Duplicate vintage {name} {day}')
                values[day]=vintage_value(data[column],name,day)
                available = data[column].loc[:day].dropna()
                obs_days[day] = available.index.max() if len(available) else pd.NaT
        panel[factor]=panel.Cutoff.map(values)
        panel[factor+'LatestObs']=panel.Cutoff.map(obs_days)
        assert panel[factor+'LatestObs'].dropna().le(panel.Cutoff).all()
        sources.append(dict(Factor=factor,Series=name,Version='ALFRED vintage by previous US session',Rows=int(panel[factor].notna().sum())))
    panel.to_csv(args.out/'feature_panel.csv',index=False)
    pd.DataFrame(sources).to_csv(args.out/'factor_coverage.csv',index=False)
    return panel


def liquidity_series(inputs, cutoffs):
    active = set(cutoffs)
    vintages = {}
    for name in LIQUIDITY_IDS:
        for path in inputs.glob(f'{name}_*.csv'):
            data=pd.read_csv(path,index_col=0,parse_dates=True)
            for col in data:
                day=pd.to_datetime(col.rsplit('_',1)[1],format='%Y%m%d')
                if day in active:
                    vintages.setdefault(day,{})[name]=data[col].loc[:day].dropna()
    result = {}
    for day, series in vintages.items():
        if set(series)!=set(LIQUIDITY_IDS):
            continue
        values=pd.DataFrame(series).dropna()
        values=values.loc[values.index.dayofweek==2]
        if values.empty or (day-values.index[-1]).days>21:
            continue
        previous_day = values.index[-1]-pd.Timedelta(weeks=13)
        past=values.loc[:previous_day]
        if past.empty or (previous_day-past.index[-1]).days>14 or past.WALCL.iloc[-1]<=0:
            continue
        proxy = values.WALCL-values.WDTGAL-1000*values.RRPONTSYD
        result[day]=(proxy.iloc[-1]-proxy.loc[past.index[-1]])/past.WALCL.iloc[-1]
    return result


def forecast(train, row, features, ridge=False, clip=False):
    means=train[features].mean()
    std=train[features].std(ddof=0)
    active=[name for name in features if std[name]>1e-12]
    norm_train=(train[active]-means[active])/std[active]
    norm_row=((row[active]-means[active])/std[active]).to_numpy(dtype=float,copy=True)
    if clip:
        # Only new predictors are capped. Boundaries use the current training set.
        new=[j for j,name in enumerate(active) if name not in BASE]
        norm_train.iloc[:,new]=norm_train.iloc[:,new].clip(-3.,3.)
        norm_row[new]=np.clip(norm_row[new],-3.,3.)
    x=np.column_stack([np.ones(len(train)),norm_train])
    target=train[Y].to_numpy()
    if ridge:
        # Preserve the established B coefficients without direct penalty. Shrink only new terms.
        penalty=np.diag([0., *[0. if name in BASE else len(train)*.5 for name in active]])
        fit=np.linalg.solve(x.T@x+penalty,x.T@target)
    else:
        fit=np.linalg.lstsq(x,target,rcond=None)[0]
    values=np.r_[1.,norm_row]
    beta_new=fit[-1] if len(features)==len(BASE)+1 and features[-1] in active else np.nan
    return float(values@fit),float(beta_new)


def forecasts(panel, name, added, common, clip=False):
    required=[*FACTORS] if common else added
    rows=panel.dropna(subset=[*BASE,Y,*required])
    output=[]
    for _, row in rows.iterrows():
        train=rows.loc[rows.Exit<=row.Signal]
        if len(train)<36:
            continue
        assert train.Exit.max()<row.Entry
        c, beta=forecast(train,row,BASE+added,ridge=name==BUNDLE,clip=clip)
        output.append(dict(Model=name,Sample='Common' if common else 'Available',Signal=row.Signal,
                           Entry=row.Entry,Exit=row.Exit,Actual=row[Y],NTrain=len(train),LatestTrainExit=train.Exit.max(),
                           A=forecast(train,row,BASE[:-1])[0],B=forecast(train,row,BASE)[0],C=c,
                           Mean=train[Y].mean(),BetaAdditional=beta,QuarterPhase=row.Signal.month in PHASE))
    return pd.DataFrame(output)


def loss_statistics(errors, lag):
    """HAC paired forecast loss difference with calendar distance, normal approximation."""
    n=len(errors)
    freq='M' if lag==3 else 'Q-NOV'
    periods=errors.Signal.dt.to_period(freq).astype('int64').to_numpy()
    difference=(errors.ErrB-errors.ErrC).to_numpy()
    avg=difference.mean()
    z=np.zeros(periods.max()-periods.min()+1)
    z[periods-periods.min()]=difference-avg
    cov=z@z
    for k in range(1,lag+1):
        cov+=2*(1-k/(lag+1))*(z[k:]@z[:-k])
    se=np.sqrt(max(cov,0))/n*np.sqrt(n/(n-1))
    return erfc(abs(avg)/(se*np.sqrt(2))) if se else 1.


def improvement(errors, benchmark):
    return 1-errors.ErrC.sum()/errors['Err'+benchmark].sum()


def summary_row(name, category, phase, data):
    d=data.copy()
    for label in ['A','B','C','Mean']:
        d['Err'+label]=(d.Actual-d[label])**2
    favorable=(d.ErrB-d.ErrC).idxmax()
    n=len(d)
    return dict(Model=name,Sample=category,Phase=phase,N=n,Start=d.Signal.min(),End=d.Signal.max(),
                VsA=improvement(d,'A'),VsB=improvement(d,'B'),VsMean=improvement(d,'Mean'),
                VsBWithoutBest=improvement(d.drop(index=favorable),'B'),
                VsMeanWithoutBest=improvement(d.drop(index=favorable),'Mean'),
                PositiveBetaFraction=(d.BetaAdditional>0).sum()/d.BetaAdditional.notna().sum() if d.BetaAdditional.notna().any() else np.nan,
                P=loss_statistics(d,3 if phase=='Monthly' else 1) if n>=12 else np.nan)


def diagnostics(panel,args):
    robust=[]
    for factor in ('Payrolls','RealYield',BUNDLE):
        additions=list(FACTORS) if factor==BUNDLE else [factor]
        data=forecasts(panel,factor,additions,common=factor==BUNDLE,clip=True)
        for phase,rows in [('Monthly',data),('Fixed quarterly',data.loc[data.QuarterPhase])]:
            robust.append(summary_row(factor,'Clip new factors at ±3 training SD',phase,rows))
    liquidity = panel.assign(NetLiquidity=panel.Cutoff.map(liquidity_series(args.inputs,panel.Cutoff)))
    if liquidity.NetLiquidity.notna().sum()>40:
        net=forecasts(liquidity,'NetLiquidity',['NetLiquidity'],common=False)
        for phase,rows in [('Monthly',net),('Fixed quarterly',net.loc[net.QuarterPhase])]:
            robust.append(summary_row('NetLiquidity','Same-Wednesday proxy, separate test',phase,rows))
    if args.older:
        old = pd.read_csv(args.older,parse_dates=['Signal','Entry','Exit'])
        old = old.loc[old.ETF.eq('XLY')&old.Horizon.eq(3)&(old.Entry<pd.Timestamp('2017-04-03')),panel.columns.intersection(old.columns)]
        old, _=attach_daily(old,pd.DatetimeIndex(pd.read_csv(args.older_sessions,usecols=['Date'],parse_dates=['Date']).Date),args.daily_dir)
        combined=pd.concat([old,panel],ignore_index=True).sort_values('Signal')
        assert combined.Signal.is_unique
        for factor in DAILY_LABELS.values():
            name=factor[0]
            results=forecasts(combined,name,[name],False)
            for subset, desc in [(results.loc[results.Exit<'2017-04-03'],'Older holdouts entirely before 2017-04-03'),
                                 (results.loc[results.Entry>='2017-04-03'],'2017 onward with earlier training')]:
                for phase,rows in [('Monthly',subset),('Fixed quarterly',subset.loc[subset.QuarterPhase])]:
                    robust.append(summary_row(name,desc,phase,rows))
    pd.DataFrame(robust).to_csv(args.out/'sensitivity.csv',index=False)


def run(args):
    args.out.mkdir(parents=True,exist_ok=True)
    panel=feature_panel(args)
    additions={**{f:[f] for f in FACTORS},BUNDLE:list(FACTORS)}
    series=[forecasts(panel,name,added,common) for name,added in additions.items() for common in (False,True)]
    baseline=forecasts(panel,'B only',[],False)
    verified=None
    if args.audit:
        original=pd.read_csv(args.audit,parse_dates=['Signal'])
        checks=baseline.merge(original,on='Signal',validate='one_to_one',suffixes=('_macro','_old'))
        assert len(checks)==len(baseline)==len(original)==73
        for label in ['A','B','Mean','Actual']:
            np.testing.assert_allclose(checks[label+'_macro'],checks[label+'_old'],atol=1e-12,rtol=0)
        verified=len(checks)
    all_predictions=pd.concat([baseline,*series],ignore_index=True)
    all_predictions.to_csv(args.out/'walk_forward_forecasts.csv',index=False)
    results=[]
    for (name,sample),data in all_predictions.groupby(['Model','Sample']):
        for phase,rows in [('Monthly',data),('Fixed quarterly',data.loc[data.QuarterPhase])]:
            if len(rows)>=2:
                results.append(summary_row(name,sample,phase,rows))
    summary=pd.DataFrame(results)
    summary['Holm']=np.nan
    for (_,phase),g in summary.loc[summary.Model.isin(FACTORS)].groupby(['Sample','Phase']):
        order=g.P.fillna(1).sort_values().index
        adj=np.maximum.accumulate(np.minimum(1,summary.loc[order,'P'].fillna(1)*np.arange(len(g),0,-1)))
        summary.loc[order,'Holm']=adj
    summary.to_csv(args.out/'walk_forward_summary.csv',index=False)
    diagnostics(panel,args)
    print(pd.read_csv(args.out/'factor_coverage.csv').to_string(index=False))
    print(summary[['Model','Sample','Phase','N','VsA','VsB','VsMean','VsBWithoutBest','P','Holm']].to_string(index=False))
    receipt={'baseline_verified_rows':verified,'labeled_windows':len(panel),'complete_macro_windows':int(panel[list(FACTORS)].notna().all(axis=1).sum()),
             'historical_last_exit':str(panel.Exit.max().date()),'primary_candidate_count':len(FACTORS),'quarter_months':list(PHASE)}
    (args.out/'manifest.json').write_text(json.dumps(receipt,indent=2))
    print(receipt)


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--seed',type=Path,default=SEED)
    cli.add_argument('--calendar-archive',type=Path,default=None)
    cli.add_argument('--inputs',type=Path,default=INPUTS)
    cli.add_argument('--daily-dir',type=Path,default=Path('raw'))
    cli.add_argument('--out',type=Path,default=OUT)
    cli.add_argument('--audit',type=Path)
    cli.add_argument('--older',type=Path)
    cli.add_argument('--older-sessions',type=Path)
    args=cli.parse_args()
    if bool(args.older)!=bool(args.older_sessions):
        cli.error('--older and --older-sessions are both required for the early-history test')
    run(args)
