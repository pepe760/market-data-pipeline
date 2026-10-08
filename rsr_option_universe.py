"""Read-only, commit-pinned RSR dashboard universe for private option batches."""
import datetime as dt
import hashlib
import json
import math
import re
import urllib.request
from zoneinfo import ZoneInfo

REPO='pepe760/USstockRSv260420'

def parse_dashboard(html, observed):
    match=re.search(r'const BUILD_TS_HK\s*=\s*"([^"\n]+)"',html)
    if not match: raise ValueError('Missing RSR build clock')
    built=dt.datetime.fromisoformat(match.group(1)).replace(tzinfo=ZoneInfo('Asia/Hong_Kong'))
    age=(observed-built.astimezone(dt.timezone.utc)).total_seconds()
    if age < -300 or age > 4*86400: raise ValueError('RSR dashboard stale or future dated')
    groups={}; memberships={}
    for label,variable in [('RSR','POOL_RSR'),('NRSR','POOL_NRSR'),('URSR','POOL_URSR')]:
        match=re.search(r'const '+variable+r'\s*=\s*',html)
        if not match: raise ValueError('Missing RSR ranking group')
        rows,_=json.JSONDecoder().raw_decode(html[match.end():])
        if not isinstance(rows,list) or len(rows)!=30: raise ValueError('Incomplete Top30')
        tickers=[]
        for rank,row in enumerate(rows,1):
            ticker=row.get('ticker')
            score=row.get('RS_Rank')
            if not isinstance(ticker,str) or not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,10}',ticker):
                raise ValueError('Invalid ranking ticker')
            if not isinstance(score,(float,int)) or not math.isfinite(score):
                raise ValueError('Invalid ranking score')
            tickers.append(ticker)
            memberships.setdefault(ticker,[]).append({'group':label,'position':rank,'display_score':score})
        if len(set(tickers))!=30: raise ValueError('Duplicate Top30 ticker')
        groups[label]=tickers
    return dict(groups=groups,memberships=memberships,source_build_hk=built.isoformat(),
                captured_at=observed.isoformat(),html_sha256=hashlib.sha256(html.encode()).hexdigest(),
                qualification='CURRENT_DASHBOARD_OBSERVATION_NOT_HISTORICAL_PIT',
                freshness_basis='DASHBOARD_BUILD_CLOCK_NOT_PER_TICKER_PRICE_CLOCK')

def extend_config(config,token,observed):
    if not token: raise ValueError('RSR read credential missing')
    def get(path):
        request=urllib.request.Request('https://api.github.com/repos/'+REPO+'/'+path,
            headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github.raw+json',
                     'User-Agent':'PrivateMarketCollector/1.0'})
        with urllib.request.urlopen(request,timeout=45) as response:
            content=response.read(20_000_001)
        if len(content)>20_000_000: raise ValueError('Oversized RSR source')
        return content
    commit=json.loads(get('commits/main'))['sha']
    if not re.fullmatch('[a-f0-9]{40}',commit): raise ValueError('Invalid RSR commit')
    html=get('contents/index.html?ref='+commit).decode('utf-8')
    provenance=parse_dashboard(html,observed)
    provenance.update(repository=REPO,commit=commit)
    tickers=sorted(set(config['tickers']) | set(provenance['memberships']))
    if len(tickers)>103: raise ValueError('Expanded option universe exceeds bound')
    return dict(config,tickers=tickers,rsr_top30=provenance)
