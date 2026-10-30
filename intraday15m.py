"""Bounded free-source 15m landing lane. No paid APIs, synthetic quotes or trades.

Raw bytes and revisions are private and immutable. This is not PIT certification.
"""
import argparse
import contextlib
import csv
import datetime as dt
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

UTC = dt.timezone.utc
VERSION = 'intraday15m-v2'
STEP = 900
SYMBOL = re.compile(r'^[A-Z0-9][A-Z0-9.\-^=]{0,24}$')
RUN = re.compile(r'^\d{8}T\d{6}Z-[a-f0-9]{12}$')


def now(): return dt.datetime.now(UTC)
def sha(data): return hashlib.sha256(data).hexdigest()
def save(path, obj):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
    tmp.replace(path)


class BudgetStop(Exception): pass
class SourceError(Exception): pass


class HTTP:
    def __init__(self, calls=60, byte_limit=64_000_000, rate_limit_retries=2, max_retry_after=30):
        self.calls, self.byte_limit, self.used_calls, self.used_bytes = calls, byte_limit, 0, 0
        self.last = 0.0;self.rate_limit_retries=rate_limit_retries;self.max_retry_after=max_retry_after
        self.retry_events=[]

    def retry_delay(self, value, retry):
        from email.utils import parsedate_to_datetime
        delay=None
        if value:
            try:
                delay=float(int(value)) if value.strip().isdigit() else (parsedate_to_datetime(value)-now()).total_seconds()
            except (ValueError,TypeError,OverflowError):pass
        delay=max(0,delay) if delay is not None else 2**(retry+1)
        # Do not retry before a server deadline that exceeds the bounded wait.
        if delay>self.max_retry_after:raise BudgetStop('HTTP_429_RETRY_AFTER_EXCEEDS_BOUND')
        return delay

    def get(self, url):
        for retry in range(self.rate_limit_retries+1):
            if self.used_calls>=self.calls:raise BudgetStop('REQUEST_BUDGET_EXHAUSTED')
            if self.used_bytes>=self.byte_limit:raise BudgetStop('BYTE_BUDGET_EXHAUSTED')
            time.sleep(max(0,1.0-(time.monotonic()-self.last)));self.last=time.monotonic()
            self.used_calls+=1
            limit=min(12_000_000,self.byte_limit-self.used_bytes)
            try:
                req=urllib.request.Request(url,headers={'User-Agent':'WilliamResearchArchive/1.0'})
                with urllib.request.urlopen(req,timeout=25) as response:payload=response.read(limit+1)
            except urllib.error.HTTPError as exc:
                if exc.code==451:raise BudgetStop('HTTP_451_REGION_BLOCK') from None
                if exc.code==429:
                    if retry>=self.rate_limit_retries:raise BudgetStop('HTTP_429_RETRIES_EXHAUSTED') from None
                    if self.used_calls>=self.calls:raise BudgetStop('REQUEST_BUDGET_EXHAUSTED') from None
                    delay=self.retry_delay(exc.headers.get('Retry-After') if exc.headers else None,retry)
                    self.retry_events.append({'reason':'HTTP_429','delay_seconds':delay})
                    time.sleep(delay);continue
                raise SourceError('HTTP_'+str(exc.code)) from None
            except (urllib.error.URLError,TimeoutError):raise SourceError('NETWORK') from None
            self.used_bytes+=len(payload)
            if len(payload)>limit:raise BudgetStop('RESPONSE_BYTES_EXCEEDED')
            return payload


def validate_config(c):
    if c.get('schema_version') != 1: raise ValueError('schema')
    allowed = {'schema_version','stocks','crypto','crypto_market','stock_days','crypto_days','max_jobs',
               'max_requests','max_bytes','options_provider','universe_observed_at','universe_sha256',
               'use_us_directory_snapshot','crypto_interval'}
    if set(c) - allowed: raise ValueError('unknown_config_key')
    for key in ('stocks', 'crypto'):
        xs = c.get(key, [])
        if not isinstance(xs, list) or len(xs) > 20000 or len(xs) != len(set(xs)):
            raise ValueError('universe')
        if any(not isinstance(s, str) or not SYMBOL.fullmatch(s) for s in xs):
            raise ValueError('symbol')
    if c.get('crypto_market', 'spot') not in ('spot', 'um'): raise ValueError('market')
    if c.get('crypto_interval','15m') not in ('15m','1h'):raise ValueError('interval')
    for key, default, maximum in [('stock_days', 21, 30), ('crypto_days', 2, 3650),
                                  ('max_jobs', 25, 500), ('max_requests', 60, 1000),
                                  ('max_bytes', 64000000, 1000000000)]:
        value = c.get(key, default)
        if type(value) is not int or not 1 <= value <= maximum: raise ValueError(key)
    if c.get('options_provider') not in (None, 'disabled'): raise ValueError('unsupported_options_provider')
    if type(c.get('use_us_directory_snapshot',False)) is not bool: raise ValueError('universe_flag')
    if len(c.get('crypto',[])) * c.get('crypto_days',2) + len(c.get('stocks',[])) > 200000:
        raise ValueError('split_large_backfill_into_windows')
    return c


def listing_symbols(payload, kind):
    text = payload.decode('utf-8-sig')
    header = 'Symbol' if kind == 'us_nasdaq' else 'NASDAQ Symbol'
    rows = list(csv.DictReader(io.StringIO(text), delimiter='|'))
    if not rows or header not in rows[0] or 'Test Issue' not in rows[0]: raise SourceError('LISTING_SCHEMA')
    result = []
    for row in rows:
        if row.get('Test Issue') != 'N': continue
        symbol = row.get(header, '')
        if SYMBOL.fullmatch(symbol):
            result.append({'symbol': symbol, 'etf': row.get('ETF'),
                           'security_name': row.get('Security Name'), 'listing_source': kind})
    if not result: raise SourceError('EMPTY_LISTING')
    return result


def discover(root, kind, http):
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    stamp = now().strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:12]
    dest = root / stamp; dest.mkdir()
    def get(url):
        try: return http.get(url)
        except (BudgetStop,SourceError) as exc:
            save(dest/'DISCOVERY_FAILURE.json',{'kind':kind,'observed_at':now().isoformat(),
                 'status':'SOURCE_UNAVAILABLE','reason':str(exc), 'requests':http.used_calls,
                 'download_bytes':http.used_bytes,'fallback':'NONE; no bypass or invented listing'})
            raise
    if kind == 'us':
        items = []
        for filename, label in [('nasdaqlisted.txt', 'us_nasdaq'), ('otherlisted.txt', 'us_other')]:
            blob = get('https://www.nasdaqtrader.com/dynamic/SymDir/' + filename)
            (dest / filename).write_bytes(blob)
            items.extend(listing_symbols(blob, label))
        symbols = sorted(set(x['symbol'] for x in items))
    else:
        blob = get('https://data-api.binance.vision/api/v3/exchangeInfo')
        (dest / 'exchangeInfo.json').write_bytes(blob)
        items = json.loads(blob)['symbols']
        symbols = sorted(set(x['symbol'] for x in items if x.get('status') == 'TRADING'
                             and x.get('quoteAsset') == 'USDT' and SYMBOL.fullmatch(x['symbol'])))
    save(dest / 'universe.json', {'schema_version': 1, 'observed_at': now().isoformat(),
         'kind': kind, 'symbols': symbols, 'records': items,
         'qualification': 'CURRENT_LISTING_NOT_HISTORICAL_PIT; omitted symbols are not deleted from prior archives'})
    return dest / 'universe.json', len(symbols)


def stock_grid(asof, days):
    import exchange_calendars as xc
    cal = xc.get_calendar('XNYS')
    sessions = cal.sessions_in_range(str((asof - dt.timedelta(days=days)).date()), str(asof.date()))
    grid = []
    for session in sessions:
        end = cal.session_close(session).to_pydatetime()
        if end + dt.timedelta(minutes=90) > asof: continue
        start = cal.session_open(session).to_pydatetime()
        grid.extend(range(int(start.timestamp()), int(end.timestamp()), STEP))
    return grid


def tasks(config, asof):
    result = []
    grid = stock_grid(asof, config.get('stock_days', 21)) if config.get('stocks') else []
    # Fetch recent history only; no claim Yahoo can backfill arbitrary intraday history.
    for symbol in sorted(config.get('stocks', [])):
        if grid: result.append({'kind': 'stocks', 'symbol': symbol, 'grid': grid})
    market = config.get('crypto_market', 'spot')
    for days_ago in range(config.get('crypto_days', 2), 0, -1):
        day = asof.date() - dt.timedelta(days=days_ago)
        for symbol in sorted(config.get('crypto', [])):
            item={'kind': 'crypto', 'symbol': symbol, 'date': str(day), 'market': market}
            if config.get('crypto_interval','15m')!='15m':item['interval']=config['crypto_interval']
            result.append(item)
    for task in result: task['id'] = sha(json.dumps(task, sort_keys=True).encode())
    return result


def bar(ts, values, task, observed):
    step={'15m':900,'1h':3600}[task.get('interval','15m')]
    if len(values) != 5: raise ValueError('ohlcv')
    o, h, l, c, v = [float(x) for x in values]
    if not all(math.isfinite(x) for x in (o, h, l, c, v)): raise ValueError('nonfinite')
    if min(o, h, l, c) <= 0 or v < 0 or h < max(o, l, c) or l > min(o, h, c):
        raise ValueError('invalid_market')
    start = dt.datetime.fromtimestamp(ts, UTC)
    if start + dt.timedelta(seconds=step) > dt.datetime.fromisoformat(observed):
        raise ValueError('unfinished')
    return {'source': 'Yahoo/chart' if task['kind'] == 'stocks' else 'Binance/public-archive',
            'venue': 'US_VENDOR_CONSOLIDATION_UNVERIFIED' if task['kind'] == 'stocks' else 'BINANCE_' + task['market'],
            'symbol': task['symbol'], 'asset': task['kind'], 'bar_start': start.isoformat(),
            'bar_end': (start + dt.timedelta(seconds=step)).isoformat(),
            'available_at': observed, 'interval_seconds': step,
            'open': o, 'high': h, 'low': l, 'close': c, 'volume': v,
            'adjustment': 'VENDOR_UNADJUSTED_REQUEST_NOT_RAW_CERTIFIED' if task['kind'] == 'stocks' else 'EXCHANGE_NATIVE',
            'qualification': 'RESEARCH_ONLY_UNVERIFIED', 'executable_quote': False}


def parse_crypto(blob, task, observed):
    import crypto_lifecycle as lifecycle
    interval=task.get('interval','15m');step={'15m':900,'1h':3600}[interval]
    filename = f"{task['symbol']}-{interval}-{task['date']}.csv"
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        entries = z.infolist()
        if len(entries) != 1 or entries[0].filename != filename or entries[0].file_size > 12_000_000:
            raise SourceError('UNSAFE_ARCHIVE')
        if entries[0].external_attr >> 16 & 0o170000 == 0o120000: raise SourceError('SYMLINK')
        text = z.read(filename).decode()
    start = int(dt.datetime.fromisoformat(task['date']).replace(tzinfo=UTC).timestamp())
    policy_task=dict(task,kind='crypto',market=task.get('market','spot'))
    policy=lifecycle.lifecycle_policy(policy_task)
    expected=set(policy['expected_slots'])
    saved, rejected, seen = [], [], set()
    for i, row in enumerate(csv.reader(io.StringIO(text))):
        if i == 0 and row and row[0] == 'open_time': continue
        try:
            if len(row) != 12: raise ValueError('schema')
            raw = int(row[0]); divisor = 1_000_000 if raw >= 10**15 else 1000
            if raw % (divisor * step): raise ValueError('grid')
            ts = raw // divisor
            close_raw = int(row[6])
            clock=lifecycle.close_clock_policy(policy_task,raw,close_raw,divisor)
            if not clock['allowed']: raise ValueError('close_clock')
            if ts not in expected or ts in seen: raise ValueError('date_or_duplicate')
            item = bar(ts, [*row[1:5], row[5]], task, observed)
            if clock['close_clock_exception']:
                item.update({k:v for k,v in clock.items() if k!='allowed'})
            seen.add(ts); saved.append(item)
        except (ValueError, TypeError, OverflowError) as exc:
            rejected.append({'row': i, 'reason': str(exc) if str(exc) in ('schema','grid','close_clock','date_or_duplicate','unfinished','nonfinite','invalid_market') else 'INVALID_ROW'})
    return saved, {'expected':len(expected),'saved':len(saved),'missing':sorted(expected-seen),'rejected':rejected,
                   'lifecycle_status':policy['status'],'retired_not_expected_slots':len(policy['retired_not_expected_slots']),
                   'lifecycle_policy_version':policy['policy_version'],'lifecycle_source':policy['source_url']}


def parse_stocks(blob, task, observed):
    payload=json.loads(blob)
    if not isinstance(payload,dict) or not isinstance(payload.get('chart'),dict):raise SourceError('STOCK_SCHEMA')
    data=payload['chart']
    if data.get('error'):raise SourceError('VENDOR_ERROR')
    results=data.get('result')
    if not isinstance(results,list) or len(results)!=1 or not isinstance(results[0],dict):raise SourceError('STOCK_SCHEMA')
    result=results[0]
    if not isinstance(result.get('meta'),dict):raise SourceError('STOCK_SCHEMA')
    if result['meta'].get('symbol') != task['symbol'].replace('.', '-'):
        raise SourceError('SYMBOL_MISMATCH')
    indicators=result.get('indicators')
    if not isinstance(indicators,dict):raise SourceError('STOCK_SCHEMA')
    quotes=indicators.get('quote')
    if not isinstance(quotes,list) or len(quotes)!=1 or not isinstance(quotes[0],dict):raise SourceError('STOCK_SCHEMA')
    quote=quotes[0];stamps=result.get('timestamp')
    if not isinstance(stamps,list):raise SourceError('TIMESTAMP_SCHEMA')
    if any(not isinstance(ts,int) or isinstance(ts,bool) or ts<0 for ts in stamps):
        raise SourceError('TIMESTAMP_SCHEMA')
    for key in ('open','high','low','close','volume'):
        if not isinstance(quote.get(key),list):raise SourceError('OHLCV_SCHEMA')
        if len(quote[key])!=len(stamps):raise SourceError('ARRAY_LENGTH')
        if any(value is not None and (isinstance(value,bool) or not isinstance(value,(int,float))) for value in quote[key]):
            raise SourceError('OHLCV_TYPE')
    expected = set(task['grid']); saved, rejected, seen, encountered = [], [], set(), set()
    window_start,window_end=min(expected),max(expected)+STEP
    for i, ts in enumerate(stamps):
        if ts not in expected and (ts%STEP==0 or ts<window_start or ts>=window_end):continue
        try:
            if ts in encountered:raise ValueError('DUPLICATE_TIMESTAMP')
            encountered.add(ts)
            values=[quote[k][i] for k in ('open','high','low','close','volume')]
            # Null or irregular timestamp labels never excuse present corrupt OHLCV.
            known=[None if value is None else float(value) for value in values]
            if any(value is not None and not math.isfinite(value) for value in known):raise ValueError('nonfinite')
            o,h,l,close,volume=known
            if (any(value is not None and value<=0 for value in known[:4]) or
                (volume is not None and volume<0) or
                (h is not None and any(value is not None and h<value for value in (o,l,close))) or
                (l is not None and any(value is not None and l>value for value in (o,h,close)))):
                raise ValueError('invalid_market')
            if ts%STEP:raise ValueError('UNALIGNED_SOURCE_TIMESTAMP_EXCLUDED')
            if any(v is None for v in values):raise ValueError('NULL_OHLCV')
            item=bar(ts,values,task,observed)
            seen.add(ts); saved.append(item)
        except (ValueError,TypeError,OverflowError) as exc:
            reason={'nonfinite':'NONFINITE_OHLCV','invalid_market':'INVALID_MARKET','unfinished':'UNFINISHED_BAR',
                    'DUPLICATE_TIMESTAMP':'DUPLICATE_TIMESTAMP','NULL_OHLCV':'NULL_OHLCV',
                    'UNALIGNED_SOURCE_TIMESTAMP_EXCLUDED':'UNALIGNED_SOURCE_TIMESTAMP_EXCLUDED'}.get(str(exc),'INVALID_OHLCV')
            rejected.append({'row':i,'reason':reason,**({'original_timestamp':ts} if ts%STEP else {})})
    counts={reason:sum(r['reason']==reason for r in rejected) for reason in sorted({r['reason'] for r in rejected})}
    availability=('NO_HISTORY_IN_WINDOW' if not encountered else 'NO_VALID_BARS' if not saved else
                  'PARTIAL_BARS' if expected-seen or rejected else 'BARS_OBSERVED')
    return saved, {'expected': len(expected), 'saved': len(saved), 'missing': sorted(expected - seen),
                   'rejected': rejected, 'corporate_actions': result.get('events', {}),
                   'source_observation':'OBSERVED','data_availability':availability,'rejection_counts':counts,
                   'identity': 'CURRENT_TICKER_NOT_PIT; no successor-price splice'}


def fetch_task(task, dest, http):
    if task['kind'] == 'crypto':
        market = 'spot' if task['market'] == 'spot' else 'futures/um'
        interval=task.get('interval','15m')
        if interval not in ('15m','1h'):raise ValueError('interval')
        file = f"{task['symbol']}-{interval}-{task['date']}.zip"
        url = f"https://data.binance.vision/data/{market}/daily/klines/{task['symbol']}/{interval}/{file}"
        blob = http.get(url)
        (dest / 'raw.zip').write_bytes(blob)
        checksum = http.get(url + '.CHECKSUM')
        (dest / 'raw.CHECKSUM').write_bytes(checksum)
        parts = checksum.decode().strip().split()
        if len(parts) != 2 or parts[0] != sha(blob) or parts[1].lstrip('*') != file:
            raise SourceError('CHECKSUM_MISMATCH')
    else:
        qs = urllib.parse.urlencode({'period1': min(task['grid']), 'period2': max(task['grid']) + STEP,
                                    'interval': '15m', 'includePrePost': 'false', 'events': 'div,splits'})
        symbol = urllib.parse.quote(task['symbol'].replace('.', '-'), safe='')
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{qs}'
        blob = http.get(url)
        (dest / 'raw.json').write_bytes(blob)
    observed = now().isoformat()  # Not task start or historical bar time.
    rows, quality = (parse_crypto if task['kind'] == 'crypto' else parse_stocks)(blob, task, observed)
    quality.update(status='COMPLETE' if rows and not quality['missing'] and not quality['rejected'] else 'PARTIAL',
                   fetched_at=observed, raw_sha256=sha(blob), independent_crosscheck='PENDING')
    return rows, quality


def seal(dest):
    dependency=Path(__file__).with_name('crypto_lifecycle.py')
    target=dest/dependency.name
    if target.exists() and target.read_bytes()!=dependency.read_bytes():raise ValueError('lifecycle_source_conflict')
    if not target.exists():target.write_bytes(dependency.read_bytes())
    payload = {str(p.relative_to(dest)): sha(p.read_bytes()) for p in sorted(dest.rglob('*')) if p.is_file()}
    save(dest/'manifest.json', {'schema_version':1, 'run_id':dest.name, 'code_version':VERSION,
         'code_sha256':sha(Path(__file__).read_bytes()),'code_dependencies':{'crypto_lifecycle.py':payload['crypto_lifecycle.py']}, 'files':payload})
    save(dest/'COMMITTED.json', {'manifest_sha256':sha((dest/'manifest.json').read_bytes())})


def verify(dest):
    if not RUN.fullmatch(dest.name) or dest.is_symlink(): raise ValueError('batch')
    marker = json.loads((dest/'COMMITTED.json').read_text())
    if marker['manifest_sha256'] != sha((dest/'manifest.json').read_bytes()): raise ValueError('manifest_hash')
    manifest = json.loads((dest/'manifest.json').read_text())
    if manifest['schema_version'] != 1 or manifest['run_id'] != dest.name: raise ValueError('schema')
    actual = {str(p.relative_to(dest)) for p in dest.rglob('*') if p.is_file()} - {'manifest.json','COMMITTED.json'}
    if actual != set(manifest['files']): raise ValueError('file_set')
    for name, expected in manifest['files'].items():
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts or any((dest/Path(*rel.parts[:i])).is_symlink()
                                                        for i in range(1,len(rel.parts)+1)):
            raise ValueError('unsafe_path')
        if sha((dest/rel).read_bytes()) != expected: raise ValueError('payload_hash')
    return manifest


def collect(config, root, http=None, asof=None):
    import pandas as pd
    config = validate_config(config); root = Path(root); root.mkdir(parents=True, exist_ok=True)
    http = http or HTTP(config.get('max_requests',60), config.get('max_bytes',64000000))
    asof = asof or now()
    with (root/'.writer.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path = root/'checkpoint.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {'schema_version':1, 'completed':[]}
        if state.get('schema_version')!=1 or not isinstance(state.get('completed'),list) or any(
            not isinstance(x,str) or not re.fullmatch('[a-f0-9]{64}',x) for x in state['completed']):
            raise ValueError('checkpoint_schema')
        jobs = tasks(config, asof)
        pending = [t for t in jobs if t['id'] not in state['completed']]
        # Rotate failed/deferred symbols; one permanently missing ticker must not
        # starve a broad universe. Interleave asset classes within each run.
        attempts = state.setdefault('attempted',{})
        groups = [[t for t in pending if t['kind']==kind] for kind in ('stocks','crypto')]
        for group in groups: group.sort(key=lambda t: (attempts.get(t['id'],''), t['id']))
        ordered=[]
        for i in range(max(map(len,groups),default=0)):
            for group in groups:
                if i < len(group): ordered.append(group[i])
        dest = root/'batches'/(now().strftime('%Y%m%dT%H%M%SZ-')+uuid.uuid4().hex[:12]); dest.mkdir(parents=True)
        save(dest/'config.json', config)
        checks, completed = [], []
        for task in ordered[:config.get('max_jobs',25)]:
            attempts[task['id']]=now().isoformat()
            folder = dest/task['id']; folder.mkdir()
            save(folder/'task.json', task)
            try:
                rows, quality = fetch_task(task, folder, http)
                if rows: pd.DataFrame(rows).to_parquet(folder/'bars.parquet', index=False, compression='zstd')
                if quality['status'] == 'COMPLETE': completed.append(task['id'])
            except BudgetStop:
                quality = {'status':'DEFERRED_BUDGET_OR_SOURCE_BLOCK','independent_crosscheck':'PENDING'}
                save(folder/'quality.json', quality); checks.append({'id':task['id'],**quality}); break
            except (SourceError, ValueError, KeyError, zipfile.BadZipFile) as exc:
                quality = {'status':'SOURCE_FAILED','reason':str(exc) if isinstance(exc,SourceError) else type(exc).__name__}
            save(folder/'quality.json', quality); checks.append({'id':task['id'],**quality})
        quality = {'schema_version':1, 'created_at':now().isoformat(), 'jobs_planned':len(jobs),
                   'jobs_already_complete':len(jobs)-len(pending), 'jobs_attempted':len(checks),
                   'jobs_new_complete':len(completed), 'jobs_remaining':len(pending)-len(completed),
                   'checks':checks, 'requests':http.used_calls, 'download_bytes':http.used_bytes,
                   'options':{'status':'BLOCKED_NO_AUTHORIZED_15M_QUOTE_GREEKS_PROVIDER','rows':0},
                   'status':'PARTIAL', 'bar_lane_status':'COMPLETE' if len(pending)==len(completed) else 'PARTIAL',
                   'research_gate':'RESEARCH_ONLY_UNVERIFIED', 'independent_crosscheck':'PENDING'}
        # Fixed-seed review queue, NOT an independent-vendor validation result.
        # Stratify by asset and symbol: at least one bar from every accepted file.
        rng=random.Random(20261025); samples=[]
        for parquet in sorted(dest.glob('*/bars.parquet')):
            frame=pd.read_parquet(parquet)
            for index in rng.sample(range(len(frame)),min(2,len(frame))):
                row=frame.iloc[index]
                samples.append({key:row[key] for key in ('source','venue','symbol','asset','bar_start','bar_end','available_at')})
        save(dest/'crosscheck_sample.json',{'status':'PENDING_INDEPENDENT_SOURCE','seed':20261025,
             'strata':'accepted asset/symbol files, up to2 bars each','samples':samples,
             'rule':'Compare same instrument/quote currency/venue semantics and timestamps; retain unmatched/differences; checksum is not cross-vendor validation'})
        # Overall stays PARTIAL while the requested option/Greek lane is unavailable.
        save(dest/'quality.json', quality); seal(dest); verify(dest)
        state['completed'] = sorted(set(state['completed']) | set(completed))
        save(state_path, state)
        return dest, quality


def import_batch(batch, db):
    verify(batch)
    signature = sha((batch/'manifest.json').read_bytes())
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,sha TEXT NOT NULL,path TEXT NOT NULL,quality TEXT NOT NULL)')
        old = conn.execute('SELECT sha FROM batches WHERE id=?',(batch.name,)).fetchone()
        if old:
            if old[0] != signature: raise ValueError('immutable_batch_conflict')
            return False
        conn.execute('INSERT INTO batches VALUES(?,?,?,?)',(batch.name,signature,str(batch), (batch/'quality.json').read_text()))
    return True


def transfer(mode, root):
    import pipeline as p
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    target = p.remote(local=True) + 'intraday15m/'
    if mode == 'publish':
        # No credentials or unrelated databases are copied. Commit marker goes LAST.
        with (root/'.writer.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for batch in sorted((root/'batches').iterdir()):
                verify(batch)
                p.rclone('copy',batch,target+'batches/'+batch.name,'--exclude','COMMITTED.json','--immutable')
                check=root/'readback'/batch.name
                p.rclone('copy',target+'batches/'+batch.name,check)
                (check/'COMMITTED.json').write_bytes((batch/'COMMITTED.json').read_bytes())
                verify(check)
                p.rclone('copyto',batch/'COMMITTED.json',target+'batches/'+batch.name+'/COMMITTED.json','--immutable')
                p.rclone('copyto',target+'batches/'+batch.name+'/COMMITTED.json',check/'COMMITTED.json')
                verify(check)
            # Resume state advances only after all immutable batches were read back.
            p.rclone('copyto',root/'checkpoint.json',target+'checkpoint.json')
            p.rclone('copyto',target+'checkpoint.json',root/'checkpoint.readback.json')
            if sha((root/'checkpoint.readback.json').read_bytes()) != sha((root/'checkpoint.json').read_bytes()):
                raise ValueError('checkpoint_readback')
    else:
        with (root/'.writer.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            p.rclone('copy',target+'batches',root/'batches','--immutable')
            imported = pending = 0
            for batch in sorted((root/'batches').iterdir()):
                if not (batch/'COMMITTED.json').exists(): pending += 1; continue
                imported += import_batch(batch,root/'catalog.sqlite')
            save(root/'SYNC_STATUS.json',{'checked_at':now().isoformat(),'imported':imported,'incomplete_batches':pending,
                 'scope':'intraday15m only; market-data/PIT databases unchanged'})


def cloud():
    import pipeline as p
    target=p.remote()
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        p.rclone('copyto',target+'config/intraday15m.json',root/'config.json')
        config=validate_config(json.loads((root/'config.json').read_text()))
        p.rclone('mkdir',target+'intraday15m')
        listing=subprocess.run(['rclone','lsf',target+'intraday15m'],capture_output=True,timeout=120)
        if listing.returncode: raise SourceError('PRIVATE_STATE_LISTING_FAILED')
        if 'checkpoint.json' in listing.stdout.decode().splitlines():
            p.rclone('copyto',target+'intraday15m/checkpoint.json',root/'checkpoint.json')
        if config.get('use_us_directory_snapshot'):
            p.rclone('copyto',target+'config/intraday15m_universe.json',root/'universe.json')
            u=json.loads((root/'universe.json').read_text())
            if u.get('kind')!='us': raise ValueError('wrong_universe')
            config.update(stocks=u['symbols'],universe_observed_at=u['observed_at'],
                          universe_sha256=sha((root/'universe.json').read_bytes()))
            validate_config(config)
        batch,q=collect(config,root)
        # cloud credentials and local remote share existing routing helper, but
        # this lane never touches the old batches/ or observations.sqlite.
        transfer('publish',root)
        return q


def main():
    os.umask(0o077)
    ap=argparse.ArgumentParser()
    ap.add_argument('mode',choices=['plan','collect','verify','publish','sync','discover-us','discover-crypto','cloud'])
    ap.add_argument('--root',type=Path)
    ap.add_argument('--config',type=Path)
    ap.add_argument('--universe',type=Path,help='Saved US discovery file; expands stocks without exposing symbols in public config')
    args=ap.parse_args()
    if args.mode=='cloud':
        q=cloud()
        print(json.dumps({k:q[k] for k in ('status','bar_lane_status','jobs_attempted','jobs_new_complete','jobs_remaining')}))
        sys.exit(0 if q['bar_lane_status']=='COMPLETE' else 2)
    if args.root is None or not args.root.is_absolute(): raise ValueError('absolute_private_root_required')
    if args.mode.startswith('discover-'):
        path,count=discover(args.root,args.mode.split('-')[1],HTTP(4,8000000))
        print(json.dumps({'discovered':count,'saved_private':True})); return
    if args.mode in ('publish','sync'): transfer(args.mode,args.root); print('Private intraday transfer verified.'); return
    if args.mode=='verify': verify(args.root); print('Private intraday batch hash verified.'); return
    config=validate_config(json.loads(args.config.read_text()))
    if args.universe:
        u=json.loads(args.universe.read_text())
        if u.get('kind')!='us': raise ValueError('wrong_universe')
        config['stocks']=u['symbols']; validate_config(config)
        config['universe_observed_at']=u['observed_at']
        config['universe_sha256']=sha(args.universe.read_bytes())
    if args.mode=='plan':
        planned=tasks(config,now())
        print(json.dumps({'planned_jobs':len(planned),'max_jobs_per_run':config.get('max_jobs',25),
                          'max_requests':config.get('max_requests',60),'options':'BLOCKED_NO_PROVIDER'})); return
    dest,q=collect(config,args.root)
    import_batch(dest,args.root/'catalog.sqlite')
    print(json.dumps({k:q[k] for k in ('status','bar_lane_status','jobs_attempted','jobs_new_complete','jobs_remaining','requests','download_bytes')}))
    sys.exit(0 if q['bar_lane_status']=='COMPLETE' else 2)


if __name__=='__main__':
    try: main()
    except Exception as exc:
        # No response bodies, symbol lists, tokens or provider error strings in logs.
        print('Intraday lane failed ('+type(exc).__name__+'). Private evidence preserved.')
        sys.exit(1)
