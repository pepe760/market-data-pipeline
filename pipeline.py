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

FILES = {'prices.jsonl', 'events.jsonl', 'quality.json'}
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


def collect(config, dest):
    import yfinance as yf
    asof = now()
    sessions = closed_sessions(asof)
    if not sessions:
        raise ValueError('No completed session')
    prices, events, checks = [], [], []
    tripped = False
    for ticker in config['prices']:
        if tripped:
            checks.append(dict(ticker=ticker, kind='prices', status='NOT_ATTEMPTED_RATE_LIMIT'))
            continue
        try:
            frame = yf.Ticker(ticker).history(start=sessions[0],
                end=str(dt.date.fromisoformat(sessions[-1]) + dt.timedelta(days=1)),
                auto_adjust=False, back_adjust=False, repair=False, actions=True,
                keepna=True, timeout=20, raise_errors=True)
            observed = now().isoformat()
            seen, rejected = set(), 0
            for stamp, row in frame.iterrows():
                day = str(stamp.date())
                if day not in sessions or day in seen:
                    rejected += 1
                    continue
                try:
                    prices.append(price_row(ticker, day, row, observed))
                    seen.add(day)
                except ValueError:
                    rejected += 1
            missing = sorted(set(sessions) - seen)
            checks.append(dict(ticker=ticker, kind='prices', missing_sessions=missing,
                               rejected_rows=rejected, status='OK' if not missing and not rejected else 'PARTIAL'))
        except Exception as exc:
            tripped = 'RateLimit' in type(exc).__name__
            checks.append(dict(ticker=ticker, kind='prices', status='RATE_LIMIT' if tripped else 'ERROR'))
        time.sleep(0.5)
    for ticker in config['events']:
        for kind in ('calendar', 'news'):
            if tripped:
                checks.append(dict(ticker=ticker, kind=kind, status='NOT_ATTEMPTED_RATE_LIMIT'))
                continue
            try:
                obj = yf.Ticker(ticker)
                payload = obj.calendar if kind == 'calendar' else obj.get_news(count=10)
                # Keep no copyrighted full article text or vendor debug response.
                if kind == 'news':
                    sanitized = []
                    for item in payload:
                        content = item.get('content', item)
                        link = content.get('canonicalUrl') or {}
                        provider = content.get('provider') or {}
                        sanitized.append(dict(title=content.get('title'),
                            url=link.get('url') or content.get('link'),
                            publisher=provider.get('displayName') or content.get('publisher'),
                            published_at=content.get('pubDate') or content.get('providerPublishTime')))
                    payload = sanitized
                events.append(dict(ticker=ticker, kind=kind, available_at=now().isoformat(),
                                   source='Yahoo/yfinance', payload=payload, pit_status='OBSERVATION_ONLY'))
                checks.append(dict(ticker=ticker, kind=kind, status='OK' if payload else 'EMPTY_UNVERIFIED'))
            except Exception as exc:
                tripped = 'RateLimit' in type(exc).__name__
                checks.append(dict(ticker=ticker, kind=kind, status='RATE_LIMIT' if tripped else 'ERROR'))
            time.sleep(0.5)
    for name, rows in [('prices.jsonl', prices), ('events.jsonl', events)]:
        with (dest / name).open('w') as handle:
            for row in rows:
                handle.write(json.dumps(row, default=str, allow_nan=False) + '\n')
    status = 'COMPLETE' if all(c['status'] == 'OK' for c in checks) else 'PARTIAL'
    write_json(dest / 'quality.json', dict(status=status, checks=checks,
        universe_as_of=config['as_of'], expected_sessions=sessions,
        expected_price_tickers=len(config['prices']), event_tickers=len(config['events']),
        research_gate='RESEARCH_ONLY_UNVERIFIED', cross_vendor_random_check='PENDING',
        gap_policy='All missing sessions retained; no forward fill; older outages need explicit backfill'))
    return status


def seal(dest):
    manifest = dict(schema=1, run_id=dest.name, created_at=now().isoformat(),
        code_revision=os.getenv('GITHUB_SHA', 'local-unversioned'),
        dependencies={n: importlib.metadata.version(n) for n in ('yfinance', 'exchange-calendars')},
        files={name: digest(dest / name) for name in sorted(FILES)})
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
    if manifest['schema'] != 1 or manifest['run_id'] != dest.name or set(manifest['files']) != FILES:
        raise ValueError('Invalid manifest')
    for name, sha in manifest['files'].items():
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
        for kind in ('prices', 'events'):
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


def cloud():
    global STAGE
    target = remote()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        STAGE = 'download_config'
        rclone('copyto', target + 'config/universe.json', base / 'universe.json')
        STAGE = 'validate_config'
        config = universe(json.loads((base / 'universe.json').read_text()))
        dest = base / (now().strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:12])
        dest.mkdir()
        STAGE = 'collect_and_serialize'
        status = collect(config, dest)
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
    parser.add_argument('mode', choices=['cloud', 'sync'])
    parser.add_argument('--root', type=Path)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        # Vendor libraries can print response details: never expose them in public logs.
        with open(os.devnull, 'w') as null, contextlib.redirect_stdout(null), contextlib.redirect_stderr(null):
            if args.mode == 'cloud':
                code = cloud()
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
