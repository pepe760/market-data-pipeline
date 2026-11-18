"""Fetch official macro series and genuine previous-session monthly vintages."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import csv
import subprocess
from urllib.parse import urlencode

import pandas as pd


VINTAGE_IDS = ('ICSA', 'CPILFESL', 'PAYEMS', 'WRESBAL', 'PCEDGC96')
LIQUIDITY_IDS = ('WALCL','WDTGAL','RRPONTSYD')
DAILY_IDS = ('T10Y2Y','DFII10','BAA10Y')


def trading_sessions(seed, archive=None):
    if archive:
        return pd.DatetimeIndex(pd.read_csv(archive,usecols=['Date'],parse_dates=['Date']).Date)
    import exchange_calendars as xc
    dates = pd.read_csv(seed,parse_dates=['Signal','Exit'])
    cal = xc.get_calendar('XNYS', start='2000-01-01', end=dates.Exit.max()+pd.Timedelta(days=31))
    return cal.sessions


def request(out, name, dates):
    if dates:
        query = urlencode({'id': ','.join([name]*len(dates)), 'vintage_date': ','.join(dates), 'cosd': '2015-01-01'})
        url = 'https://alfred.stlouisfed.org/graph/alfredgraph.csv?' + query
        stem = f'{name}_{dates[0]}.csv'
        expected = [name+'_'+v.replace('-','') for v in dates]
    else:
        query = urlencode({'id':name, 'cosd':'2000-01-01', 'coed':'2026-10-30'})
        url = 'https://fred.stlouisfed.org/graph/fredgraph.csv?' + query
        stem = f'{name}.csv'
        expected = [name]
    path = out/stem
    if not path.exists():
        part = path.with_suffix('.partial')
        subprocess.run(['curl','-fsSL','--retry','2','--max-time','35', url,'-o',str(part)],check=True)
        part.replace(path)
    with path.open() as stream:
        cols = next(csv.reader(stream))
    if cols[1:] != expected:
        raise ValueError(f'{name}: unexpected source columns in {stem}')
    return dict(file=stem, url=url, columns=len(expected))


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--seed',type=Path,default=Path('../outputs/xly_forward/training_seed.csv'))
    cli.add_argument('--out',type=Path,default=Path('../outputs/xly_macro_inputs'))
    cli.add_argument('--calendar-archive',type=Path)
    cli.add_argument('--daily',action='store_true',help='Also download the three daily source series for local research.')
    args = cli.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    sessions = trading_sessions(args.seed,args.calendar_archive)
    signals = pd.DatetimeIndex(pd.read_csv(args.seed,parse_dates=['Signal']).Signal)
    indices = sessions.get_indexer(signals)
    if (indices<1).any():
        raise ValueError('The seed has a signal outside the archived trading sessions.')
    cutoffs = sessions[indices-1].strftime('%Y-%m-%d').tolist()
    jobs = [(s,cutoffs[i:i+12]) for s in (*VINTAGE_IDS,*LIQUIDITY_IDS) for i in range(0,len(cutoffs),12)]
    if args.daily:
        jobs.extend((s,[]) for s in DAILY_IDS)
    with ThreadPoolExecutor(max_workers=10) as pool:
        snapshots = list(pool.map(lambda job:request(args.out,*job), jobs))
    pd.DataFrame(snapshots).to_csv(args.out/'sources.csv',index=False)
    print(f'Validated {len(snapshots)} downloads for {len(cutoffs)} signal cutoffs')


if __name__ == '__main__':
    main()
