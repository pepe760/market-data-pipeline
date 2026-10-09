"""Private append-only landing lane. No raw/vendor exceptions in public logs."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

FILES = {'prices.jsonl', 'events.jsonl', 'quality.json'}
OPTION_FILES = FILES | {'options.jsonl'}
RUN = re.compile(r'^\d{8}T\d{6}Z-[a-f0-9]{12}$')
STAGE = 'preflight'


def now():
    return dt.datetime.now(dt.timezone.utc)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False,
                               default=str) + '\n')


def universe(value):
    dt.date.fromisoformat(value['as_of'])
    for field in ('prices', 'events'):
        items = value[field]
        if not isinstance(items, list) or len(items) > 1000 or len(set(items)) != len(items):
            raise ValueError('Invalid universe')
        if any(not isinstance(t, str) or not re.fullmatch(r'[A-Z0-9.^=-]{1,20}', t)
               for t in items):
            raise ValueError('Invalid ticker')
    if not value['prices']:
        raise ValueError('Empty price universe')
    return value


def closed_sessions(asof):
    import exchange_calendars as xc
    cal = xc.get_calendar('XNYS')
    start = (asof - dt.timedelta(days=21)).date()
    sessions = cal.sessions_in_range(str(start), str(asof.date()))
    # Allow 90 minutes after the official close, including early-close sessions.
    return [str(s.date()) for s in sessions
            if cal.session_close(s).to_pydatetime() + dt.timedelta(minutes=90) <= asof]


def yahoo_symbol(ticker, session=None):
    """Canonical input identity is retained; Yahoo share classes use dashes."""
    # Issuer June 22 announcement: same security / CUSIP, effective June 24.
    if ticker == 'SATS' and session and session >= '2026-06-24':
        return 'ECHO'
    if ticker == 'PSKY':
        return 'SKYD'  # SEC Oct 6: same Class B common stock, new trading symbol.
    return {'BF.B': 'BF-B', 'BRK.B': 'BRK-B'}.get(ticker, ticker)


# Verified issuer/SEC completion dates. Never splice successor prices into the
# old security. Prior-window history remains in existing immutable batches.
RETIRED_AFTER = {'AVB': '2026-08-17', 'EQR': '2026-08-17', 'EA': '2026-08-04', 'WBD': '2026-10-05'}


def news_payload(yf, ticker):
    """Headlines/links only; retain distinct provider provenance."""
    for source, fetch in [('Yahoo/yfinance', lambda: yf.Ticker(ticker).get_news(count=10)),
                          ('Yahoo/search', lambda: yf.Search(ticker, max_results=1, news_count=10).news)]:
        try:
            items = fetch()
            out = []
            for item in items:
                content = item.get('content', item)
                link = content.get('canonicalUrl') or {}
                provider = content.get('provider') or {}
                title = content.get('title')
                url = link.get('url') or content.get('link')
                if title and url:
                    out.append(dict(title=title,url=url,publisher=provider.get('displayName') or content.get('publisher'),
                                    published_at=content.get('pubDate') or content.get('providerPublishTime')))
            if out: return out, source
        except Exception as exc:
            if 'RateLimit' in type(exc).__name__: raise
    names={'AAPL':'Apple','MSFT':'Microsoft','GOOGL':'Alphabet Google','AMZN':'Amazon',
           'NVDA':'Nvidia','META':'Meta Platforms','TSLA':'Tesla'}
    query=urllib.parse.urlencode({'q':names.get(ticker,ticker)+' stock when:7d', 'hl':'en-US','gl':'US','ceid':'US:en'})
    request=urllib.request.Request('https://news.google.com/rss/search?'+query,headers={'User-Agent':'MarketResearchCollector/1.0'})
    with urllib.request.urlopen(request,timeout=20) as response:
        root=ET.fromstring(response.read(2_000_000))
    out=[dict(title=i.findtext('title'),url=i.findtext('link'),publisher=i.findtext('source'),
              published_at=i.findtext('pubDate')) for i in root.findall('./channel/item')[:10]
         if i.findtext('title') and i.findtext('link')]
    return out,'GoogleNews/RSS'


def fetch_history(yf, symbol, expected):
    return yf.Ticker(symbol).history(start=expected[0],
        end=str(dt.date.fromisoformat(expected[-1]) + dt.timedelta(days=1)),
        auto_adjust=False, back_adjust=False, repair=False, actions=True,
        keepna=True, timeout=20, raise_errors=True)


def consume_history(frame, ticker, expected, observed, symbol, skip=()):
    """Keep only finite in-window rows. Duplicate or out-of-window rows are counted, not stored."""
    saved, seen, rejected, details = [], set(), 0, []
    skip = set(skip)
    if frame is None:
        return saved, seen, rejected, details
    for stamp, row in frame.iterrows():
        day = str(stamp.date())
        if day in skip:
            continue
        if day not in expected or day in seen:
            rejected += 1
            continue
        try:
            item = price_row(ticker, day, row, observed)
            item['provider_symbol'] = symbol
            saved.append(item)
            seen.add(day)
        except ValueError as exc:
            rejected += 1
            details.append(dict(session=day, reason=str(exc)))
    return saved, seen, rejected, details


def price_row(ticker, date, row, observed):
    vals = {key: float(row[key]) for key in ('Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume')}
    if not all(math.isfinite(v) for v in vals.values()):
        raise ValueError('Non-finite price')
    if min(vals[k] for k in ('Open', 'High', 'Low', 'Close', 'Adj Close')) <= 0:
        raise ValueError('Non-positive price')
    if (vals['Volume'] < 0 or vals['High'] < max(vals['Open'], vals['Low'], vals['Close'])
            or vals['Low'] > min(vals['Open'], vals['Close'])):
        raise ValueError('Invalid OHLCV')
    actions = {key: float(row.get(key, 0)) for key in ('Dividends', 'Stock Splits')}
    if not all(math.isfinite(v) and v >= 0 for v in actions.values()):
        raise ValueError('Invalid action')
    return dict(ticker=ticker, session=date, available_at=observed, source='Yahoo/yfinance',
                price_basis='VENDOR_SPLIT_ADJUSTED_NOT_RAW', pit_status='NOT_CERTIFIED',
                values=vals, actions=actions)


def collect(config, dest, archived=None):
    import yfinance as yf
    asof = now()
    sessions = closed_sessions(asof)
    if not sessions:
        raise ValueError('No completed session')
    prices, events, checks = [], [], []
    tripped = False
    archived = archived or {}
    for ticker in config['prices']:
        if ticker in RETIRED_AFTER and sessions[0] > RETIRED_AFTER[ticker]:
            checks.append(dict(ticker=ticker, kind='prices', status='NOT_EXPECTED_RETIRED',
                               retired_after=RETIRED_AFTER[ticker], missing_sessions=[]))
            continue
        if tripped:
            checks.append(dict(ticker=ticker, kind='prices', status='NOT_ATTEMPTED_RATE_LIMIT'))
            continue
        try:
            symbol = yahoo_symbol(ticker, sessions[0])
            expected = [s for s in sessions if s <= RETIRED_AFTER.get(ticker,'9999-12-31')]
            try:
                frame = fetch_history(yf, symbol, expected)
            except Exception as exc:
                if 'RateLimit' in type(exc).__name__ or not all((ticker,s) in archived for s in expected):
                    raise
                frame=None  # all required observations have verified archive coverage
            observed = now().isoformat()
            saved, seen, rejected, rejected_details = consume_history(frame, ticker, expected, observed, symbol)
            prices.extend(saved)
            missing_now = set(expected) - seen
            refetched_missing = []
            # A non-finite or absent session is one vendor read, not proof the session is missing.
            if missing_now and frame is not None:
                refetched_missing = sorted(missing_now)
                time.sleep(1)
                try:
                    refetch = fetch_history(yf, symbol, expected)
                except Exception as exc:
                    if 'RateLimit' in type(exc).__name__:
                        raise
                    refetch = None
                if refetch is not None:
                    saved, seen2, _ignored, details2 = consume_history(
                        refetch, ticker, expected, now().isoformat(), symbol, skip=seen)
                    prices.extend(saved)
                    rejected -= sum(1 for item in rejected_details if item['session'] in seen2)
                    rejected_details = [item for item in rejected_details if item['session'] not in seen2]
                    known = {item['session'] for item in rejected_details}
                    for item in details2:
                        if item['session'] not in seen and item['session'] not in seen2 and item['session'] not in known:
                            rejected_details.append(item)
                            rejected += 1
                    seen |= seen2
            recovered=[]
            for day in sorted(set(expected)-seen):
                saved=archived.get((ticker,day))
                if saved:
                    prices.append(dict(saved, recovery='VERIFIED_PRIOR_BATCH'))
                    seen.add(day); recovered.append(day)
            missing = sorted(set(expected) - seen)
            check = dict(ticker=ticker, kind='prices', missing_sessions=missing,
                         rejected_rows=rejected, rejected_details=rejected_details,
                         provider_symbol=symbol,
                         recovered_sessions=recovered,
                         status='OK' if not missing else 'PARTIAL')
            if refetched_missing:
                check['refetched_missing_sessions'] = refetched_missing
            checks.append(check)
        except Exception as exc:
            tripped = 'RateLimit' in type(exc).__name__
            checks.append(dict(ticker=ticker, kind='prices', provider_symbol=yahoo_symbol(ticker, sessions[0]),
                               error_type=type(exc).__name__, status='RATE_LIMIT' if tripped else 'ERROR'))
        time.sleep(0.5)
    for ticker in config['events']:
        for kind in ('calendar', 'news'):
            if tripped:
                checks.append(dict(ticker=ticker, kind=kind, status='NOT_ATTEMPTED_RATE_LIMIT'))
                continue
            try:
                obj = yf.Ticker(ticker)
                event_source='Yahoo/yfinance'
                if kind == 'news':
                    payload,event_source=news_payload(yf,ticker)
                else:
                    payload=obj.calendar
                if kind == 'news' and not payload:
                    # Empty is not proof that no news exists; one bounded retry.
                    time.sleep(1)
                    payload,event_source=news_payload(yf,ticker)
                # Keep no copyrighted full article text or vendor debug response.
                # news_payload already returns only the four allowed metadata fields.
                events.append(dict(ticker=ticker, kind=kind, available_at=now().isoformat(),
                                   source=event_source, payload=payload, pit_status='OBSERVATION_ONLY'))
                checks.append(dict(ticker=ticker, kind=kind, source=event_source,status='OK' if payload else 'EMPTY_UNVERIFIED'))
            except Exception as exc:
                tripped = 'RateLimit' in type(exc).__name__
                checks.append(dict(ticker=ticker, kind=kind, status='RATE_LIMIT' if tripped else 'ERROR'))
            time.sleep(0.5)
    for name, rows in [('prices.jsonl', prices), ('events.jsonl', events)]:
        with (dest / name).open('w') as handle:
            for row in rows:
                handle.write(json.dumps(row, default=str, allow_nan=False) + '\n')
    status = 'COMPLETE' if all(c['status'] in ('OK', 'NOT_EXPECTED_RETIRED') for c in checks) else 'PARTIAL'
    write_json(dest / 'quality.json', dict(status=status, checks=checks,
        universe_as_of=config['as_of'], expected_sessions=sessions,
        expected_price_tickers=len(config['prices']), event_tickers=len(config['events']),
        research_gate='RESEARCH_ONLY_UNVERIFIED', cross_vendor_random_check='PENDING',
        gap_policy='All missing sessions retained; no forward fill; older outages need explicit backfill'))
    return status


def seal(dest):
    files = OPTION_FILES if (dest / 'options.jsonl').exists() else FILES
    manifest = dict(schema=2 if files == OPTION_FILES else 1, run_id=dest.name, created_at=now().isoformat(),
        code_revision=os.getenv('GITHUB_SHA', 'local-unversioned'),
        dependencies={n: importlib.metadata.version(n) for n in ('yfinance', 'exchange-calendars')},
        files={name: digest(dest / name) for name in sorted(files)})
    write_json(dest / 'manifest.json', manifest)
    write_json(dest / 'COMMITTED.json', dict(manifest_sha256=digest(dest / 'manifest.json')))


def verify(dest):
    if not RUN.fullmatch(dest.name):
        raise ValueError('Invalid batch name')
    required = FILES | {'manifest.json', 'COMMITTED.json'}
    if any(not (dest / n).is_file() or (dest / n).is_symlink() for n in required):
        raise ValueError('Incomplete batch')
    marker = json.loads((dest / 'COMMITTED.json').read_text())
    if marker['manifest_sha256'] != digest(dest / 'manifest.json'):
        raise ValueError('Manifest mismatch')
    manifest = json.loads((dest / 'manifest.json').read_text())
    expected = {1: FILES, 2: OPTION_FILES}.get(manifest.get('schema'))
    if expected is None or manifest['run_id'] != dest.name or set(manifest['files']) != expected:
        raise ValueError('Invalid manifest')
    for name, sha in manifest['files'].items():
        if not (dest / name).is_file() or (dest / name).is_symlink():
            raise ValueError('Missing or unsafe payload')
        if digest(dest / name) != sha:
            raise ValueError('Content mismatch')
    return manifest


def import_batch(dest, db):
    manifest = verify(dest)
    signature = digest(dest / 'manifest.json')
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, sha TEXT, quality TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS observations (batch TEXT, kind TEXT, row_no INTEGER, payload TEXT, PRIMARY KEY(batch,kind,row_no))')
        prior = conn.execute('SELECT sha FROM batches WHERE id=?', (dest.name,)).fetchone()
        if prior:
            if prior[0] != signature:
                raise ValueError('Immutable batch conflict')
            return False
        kinds = ('prices', 'events', 'options') if manifest['schema'] == 2 else ('prices', 'events')
        for kind in kinds:
            with (dest / (kind + '.jsonl')).open() as handle:
                for index, line in enumerate(handle):
                    row = json.loads(line)
                    if not row.get('ticker') or not row.get('available_at') or not row.get('source'):
                        raise ValueError('Invalid observation')
                    conn.execute('INSERT INTO observations VALUES (?,?,?,?)', (dest.name, kind, index, line))
        conn.execute('INSERT INTO batches VALUES (?,?,?)',
                     (dest.name, signature, (dest / 'quality.json').read_text()))
    return True


def remote(local=False):
    if local and os.getenv('LOCAL_RCLONE_REMOTE'):
        name = os.environ['LOCAL_RCLONE_REMOTE']
        folder = os.environ['LOCAL_DRIVE_FOLDER_ID']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ValueError('Invalid remote')
        os.environ['RCLONE_CONFIG_' + name.upper() + '_ROOT_FOLDER_ID'] = folder
        return name + ':'
    if not os.getenv('DRIVE_TOKEN') or not os.getenv('DRIVE_FOLDER_ID'):
        raise ValueError('Private Drive credentials missing')
    os.environ['RCLONE_CONFIG_PRIVATE_TYPE'] = 'drive'
    os.environ['RCLONE_CONFIG_PRIVATE_TOKEN'] = os.environ['DRIVE_TOKEN']
    os.environ['RCLONE_CONFIG_PRIVATE_ROOT_FOLDER_ID'] = os.environ['DRIVE_FOLDER_ID']
    for key in ('CLIENT_ID', 'CLIENT_SECRET'):
        if os.getenv('DRIVE_' + key):
            os.environ['RCLONE_CONFIG_PRIVATE_' + key] = os.environ['DRIVE_' + key]
    return 'private:'


def rclone(*args):
    result = subprocess.run(['rclone', *map(str, args), '--retries', '3', '--low-level-retries', '2',
                             '--retries-sleep', '30s', '--tpslimit', '2'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1200)
    if result.returncode:
        # Allowlisted diagnostics only; never print vendor messages, URLs or credentials.
        raw = result.stderr.lower()
        reason = 'quota' if b'quota' in raw or b'ratelimit' in raw else 'transfer'
        if b'invalid_grant' in raw:
            reason = 'credential_expired'
        raise RuntimeError(reason)


def archived_price_rows(target, base):
    """Recover only validated observations from hash-verified private batches."""
    result=subprocess.run(['rclone','lsf',target+'batches','--dirs-only'],capture_output=True,timeout=120)
    if result.returncode: raise RuntimeError('transfer')
    names=sorted(n.rstrip('/') for n in result.stdout.decode().splitlines() if RUN.fullmatch(n.rstrip('/')))
    sessions=closed_sessions(now())
    required={('WBD',s) for s in sessions if s <= '2026-10-05'} | {('PSKY',s) for s in sessions if s < '2026-10-06'}
    rows={}
    if not required: return rows
    for name in reversed(names):
        if required <= rows.keys(): break
        if name[:8] < sessions[0].replace('-',''): break
        folder=base/'history'/name; folder.mkdir(parents=True)
        rclone('copyto',target+'batches/'+name+'/manifest.json',folder/'manifest.json')
        manifest=json.loads((folder/'manifest.json').read_text())
        if 'options.jsonl' in str(manifest): continue
        rclone('copy',target+'batches/'+name,folder)
        verify(folder)
        for line in (folder/'prices.jsonl').read_text().splitlines():
            saved=json.loads(line); ticker=saved['ticker']; day=saved['session']
            if ticker not in ('WBD','PSKY'): continue
            if day > RETIRED_AFTER.get(ticker,'9999-12-31'): continue
            try:
                price_row(ticker,day,{**saved['values'],**saved.get('actions',{})},saved['available_at'])
            except (ValueError,KeyError): continue
            rows.setdefault((ticker,day),dict(saved,recovery_batch=name))
    return rows


def cloud(options=False):
    global STAGE
    target = remote()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        STAGE = 'download_config'
        name = 'options.json' if options else 'universe.json'
        rclone('copyto', target + 'config/' + name, base / name)
        STAGE = 'validate_config'
        if options:
            import options_collector
            config = options_collector.validate_config(json.loads((base / name).read_text()))
            if os.getenv('RSR_OPTIONS_ENABLED') == 'true':
                import rsr_option_universe
                config=rsr_option_universe.extend_config(config,os.getenv('RSR_READ_TOKEN'),now())
                options_collector.validate_config(config)
        else:
            config = universe(json.loads((base / name).read_text()))
        dest = base / (now().strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:12])
        dest.mkdir()
        STAGE = 'read_verified_history'
        archived = {} if options else archived_price_rows(target,base)
        STAGE = 'collect_and_serialize'
        status = options_collector.collect(config, dest) if options else collect(config, dest,archived)
        STAGE = 'seal'
        seal(dest)
        verify(dest)
        out = target + 'batches/' + dest.name
        STAGE = 'upload_batch'
        rclone('copy', dest, out, '--exclude', 'COMMITTED.json', '--immutable')
        # Download readback before publishing the final marker.
        check = base / 'readback' / dest.name
        STAGE = 'readback_batch'
        rclone('copy', out, check)
        (check / 'COMMITTED.json').write_bytes((dest / 'COMMITTED.json').read_bytes())
        verify(check)
        STAGE = 'commit_marker'
        rclone('copyto', dest / 'COMMITTED.json', out + '/COMMITTED.json', '--immutable')
        rclone('copyto', out + '/COMMITTED.json', check / 'COMMITTED.json')
        verify(check)
        return 0 if status == 'COMPLETE' else 2


def sync(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.sync.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        target = remote(local=True)
        incoming = root / 'batches'
        # No delete or --ignore-existing: a mutated batch must fail, not pass silently.
        rclone('copy', target + 'batches', incoming, '--immutable')
        count = 0
        pending = 0
        for batch in sorted(incoming.iterdir()):
            if not batch.is_dir() or batch.is_symlink() or not RUN.fullmatch(batch.name):
                raise ValueError('Unexpected batch entry')
            if not (batch / 'COMMITTED.json').exists():
                pending += 1
                continue
            count += import_batch(batch, root / 'observations.sqlite')
        write_json(root / 'SYNC_STATUS.json', dict(checked_at=now().isoformat(),
            imported_new_batches=count, incomplete_batches=pending,
            destination='observations.sqlite', research_gate='RESEARCH_ONLY_UNVERIFIED'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['cloud', 'cloud-options', 'sync'])
    parser.add_argument('--root', type=Path)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        # Vendor libraries can print response details: never expose them in public logs.
        with open(os.devnull, 'w') as null, contextlib.redirect_stdout(null), contextlib.redirect_stderr(null):
            if args.mode in ('cloud', 'cloud-options'):
                code = cloud(options=args.mode == 'cloud-options')
            else:
                if args.root is None or not args.root.is_absolute():
                    raise ValueError('Absolute private landing root required')
                sync(args.root)
                code = 0
        print('Private pipeline finished.' if code == 0 else 'Private batch saved with incomplete coverage; inspect private quality report.')
        sys.exit(code)
    except Exception as exc:
        reason = str(exc) if type(exc) is RuntimeError and str(exc) in {'quota', 'transfer', 'credential_expired'} else type(exc).__name__
        print('Pipeline failed at ' + STAGE + ' (' + reason + '). No data printed.')
        sys.exit(1)
