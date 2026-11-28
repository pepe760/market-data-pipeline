"""Manual event repair using unchanged, verified current-window price bytes."""
import contextlib
import datetime as dt
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys

import pipeline as p


M7 = {'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA'}
MAX_MANIFESTS = 32
MAX_PRICE_BATCHES = 8


def timestamp(value):
    stamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Naive observation clock')
    return stamp


def validate_snapshot(folder, config, sessions, asof):
    """Fail closed on any incomplete or inconsistent price identity/window."""
    manifest = p.verify(folder)
    if manifest['schema'] != 1:
        raise ValueError('Wrong batch schema')
    if not re.fullmatch(r'[a-f0-9]{40}', manifest.get('code_revision', '')):
        raise ValueError('Missing source revision')
    created = timestamp(manifest['created_at'])
    if created > asof:
        raise ValueError('Future batch clock')
    quality = json.loads((folder / 'quality.json').read_text())
    if (quality.get('expected_sessions') != sessions or
            quality.get('universe_as_of') != config['as_of'] or
            quality.get('expected_price_tickers') != len(config['prices'])):
        raise ValueError('Price window or universe mismatch')
    checks = quality.get('checks')
    if not isinstance(checks, list) or not all(isinstance(c, dict) for c in checks):
        raise ValueError('Invalid checks')
    price_checks = [c for c in checks if c.get('kind') == 'prices']
    if (len(price_checks) != len(config['prices']) or
            {c.get('ticker') for c in price_checks} != set(config['prices']) or
            len({c.get('ticker') for c in price_checks}) != len(price_checks)):
        raise ValueError('Price check identity mismatch')
    expected = {(ticker, day) for ticker in config['prices'] for day in sessions
                if day <= p.RETIRED_AFTER.get(ticker, '9999-12-31')}
    for check in price_checks:
        ticker = check['ticker']
        retired = not any(t == ticker for t, _ in expected)
        status = 'NOT_EXPECTED_RETIRED' if retired else 'OK'
        if (check.get('status') != status or check.get('missing_sessions') != [] or
                (retired and check.get('retired_after') != p.RETIRED_AFTER.get(ticker))):
            raise ValueError('Incomplete price coverage')
    event_checks = [c for c in checks if c.get('kind') != 'prices']
    event_keys = [(c.get('ticker'), c.get('kind')) for c in event_checks]
    if (len(event_keys) != len(M7) * 2 or set(event_keys) !=
            {(ticker, kind) for ticker in M7 for kind in ('calendar', 'news')}):
        raise ValueError('Unexpected event check identity')
    if quality.get('status') != p.batch_status(checks):
        raise ValueError('Inconsistent batch status')
    seen = set()
    with (folder / 'prices.jsonl').open() as handle:
        for line in handle:
            row = json.loads(line)
            key = (row['ticker'], row['session'])
            if key not in expected or key in seen:
                raise ValueError('Unexpected or duplicate price key')
            seen.add(key)
            values, actions = row['values'], row['actions']
            if (set(values) != {'Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume'} or
                    set(actions) != {'Dividends', 'Stock Splits'} or
                    any(isinstance(v, bool) or not isinstance(v, (int, float))
                        for v in list(values.values()) + list(actions.values()))):
                raise ValueError('Invalid raw price fields')
            p.price_row(row['ticker'], row['session'], {**values, **actions}, row['available_at'])
            observed = timestamp(row['available_at'])
            if (observed > created or observed.date() < dt.date.fromisoformat(row['session']) or
                    row.get('source') != 'Yahoo/yfinance' or
                    row.get('provider_symbol') != p.yahoo_symbol(*key) or
                    row.get('price_basis') != 'VENDOR_SPLIT_ADJUSTED_NOT_RAW' or
                    row.get('pit_status') != 'NOT_CERTIFIED'):
                raise ValueError('Invalid price provenance or clock')
    if seen != expected:
        raise ValueError('Missing price keys')
    return manifest, quality, price_checks


def select_snapshot(config, target, base, sessions, asof):
    result = p.subprocess.run(['rclone', 'lsf', target + 'batches', '--dirs-only'],
                              capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError('transfer')
    names = sorted({line.rstrip('/') for line in result.stdout.decode().splitlines()
                    if p.RUN.fullmatch(line.rstrip('/'))}, reverse=True)
    search = dict(checked_manifests=0, checked_price_batches=0,
                  manifest_limit=MAX_MANIFESTS, price_batch_limit=MAX_PRICE_BATCHES,
                  candidate_rejections=[])
    for name in names:
        if (search['checked_manifests'] >= MAX_MANIFESTS or
                search['checked_price_batches'] >= MAX_PRICE_BATCHES):
            break
        search['checked_manifests'] += 1
        folder = Path(base) / 'event_repair_history' / name
        folder.mkdir(parents=True, exist_ok=True)
        try:
            p.rclone('copyto', target + 'batches/' + name + '/manifest.json', folder / 'manifest.json')
            header = json.loads((folder / 'manifest.json').read_text())
            if header.get('schema') != 1:
                continue
            search['checked_price_batches'] += 1
            p.rclone('copy', target + 'batches/' + name, folder)
            manifest, quality, checks = validate_snapshot(folder, config, sessions, asof)
            return folder, manifest, quality, checks, search
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            search['candidate_rejections'].append(dict(batch_id=name, reason=type(exc).__name__))
    raise ValueError('No eligible current-window complete price snapshot within search bounds')


def collector(config, dest, target, base):
    p.universe(config)
    if len(config['prices']) != 615 or set(config['events']) != M7:
        raise ValueError('Events-only repair requires the existing full universe and M7')
    asof = p.now()
    sessions = p.closed_sessions(asof)
    if not sessions:
        raise ValueError('No completed session')
    p.STAGE = 'read_event_repair_price_snapshot'
    folder, manifest, source_quality, price_checks, search = select_snapshot(
        config, target, base, sessions, asof)
    p.STAGE = 'collect_events_only'
    p.collect({**config, 'prices': []}, dest)
    quality = json.loads((dest / 'quality.json').read_text())
    if (quality.get('expected_sessions') != sessions or p.closed_sessions(p.now()) != sessions or
            quality.get('expected_price_tickers') != 0 or
            any(c.get('kind') == 'prices' for c in quality['checks']) or
            (dest / 'prices.jsonl').read_bytes()):
        raise ValueError('Event repair changed the price window or collected prices')
    shutil.copyfile(folder / 'prices.jsonl', dest / 'prices.jsonl')
    if p.digest(dest / 'prices.jsonl') != manifest['files']['prices.jsonl']:
        raise ValueError('Copied price bytes mismatch')
    checks = price_checks + quality['checks']
    previous = source_quality.get('price_snapshot', {})
    quality.update(checks=checks, status=p.batch_status(checks), expected_price_tickers=len(config['prices']),
                   price_snapshot=dict(batch_id=folder.name, manifest_sha256=p.digest(folder / 'manifest.json'),
                       prices_sha256=manifest['files']['prices.jsonl'],
                       quality_sha256=manifest['files']['quality.json'],
                       code_revision=manifest['code_revision'], created_at=manifest['created_at'],
                       original_code_revision=previous.get('original_code_revision', manifest['code_revision']),
                       source_status=source_quality['status'], search=search,
                       observation_policy='Exact price JSONL bytes and all available_at clocks preserved; NOT_CERTIFIED PIT'))
    for field in ('vendor_price_defects', 'pending_official_closes', 'overdue_official_closes'):
        quality[field] = source_quality.get(field, [])
    p.write_json(dest / 'quality.json', quality)
    return quality['status']


def main():
    logging.disable(logging.CRITICAL)
    try:
        with open(os.devnull, 'w') as null, contextlib.redirect_stdout(null), contextlib.redirect_stderr(null):
            code, message = p.cloud(collector=collector)
        print(message)
        return code
    except Exception as exc:
        reason = str(exc) if type(exc) is RuntimeError and str(exc) in {'quota', 'transfer', 'credential_expired'} else type(exc).__name__
        print('Pipeline failed at ' + p.STAGE + ' (' + reason + '). No data printed.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
