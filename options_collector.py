"""Bounded current option snapshots; no synthetic prices or historical backfill."""
import datetime as dt
import json
import math
import re
import time
from zoneinfo import ZoneInfo


def validate_config(config):
    tickers = config['tickers']
    targets = config['target_dtes']
    if (not isinstance(tickers, list) or not 1 <= len(tickers) <= 103
            or len(set(tickers)) != len(tickers)
            or any(not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,10}', t) for t in tickers)):
        raise ValueError('Invalid option universe')
    if (not isinstance(targets, list) or not 1 <= len(targets) <= 12
            or any(type(d) is not int or not 0 <= d <= 1095 for d in targets)):
        raise ValueError('Invalid DTE targets')
    return config


def select_expiries(expiries, today, targets):
    valid = sorted(set(e for e in expiries if 0 <= (dt.date.fromisoformat(e) - today).days <= 1095))
    if not valid:
        return []
    return sorted({min(valid, key=lambda e: (abs((dt.date.fromisoformat(e) - today).days - d), e))
                   for d in targets})


def _days_out(expiry, today):
    return (dt.date.fromisoformat(expiry) - today).days


def replacement_expiry(offered, today, targets, bad, already):
    """Next listed expiry for the DTE target that selected an unusable expiry."""
    universe = [e for e in dict.fromkeys(list(offered) + [bad]) if 0 <= _days_out(e, today) <= 1095]
    valid = [e for e in universe if e != bad]
    if not valid or not targets:
        return None
    owners = [d for d in targets
              if min(universe, key=lambda e: (abs(_days_out(e, today) - d), e)) == bad]
    if not owners:
        owners = list(targets)
    unused = [e for e in valid if e not in already]
    pool = unused or valid

    def rank(expiry):
        return min((abs(_days_out(expiry, today) - d), expiry) for d in owners)

    return min(pool, key=rank)


def has_live_quote(record):
    bid, ask = record['values']['bid'], record['values']['ask']
    return (bid is not None and bid > 0) or (ask is not None and ask > 0)


def parse_chain(ticker, expiry, chain, observed, started, memberships):
    underlying = getattr(chain, 'underlying', None) or {}
    seen, rejected, side_counts, flags, records = set(), 0, {}, {}, []
    for side, frame in [('call', chain.calls), ('put', chain.puts)]:
        side_counts[side] = 0
        if frame is None:
            continue
        for _, row in frame.iterrows():
            try:
                record = normalize(ticker, expiry, side, row, underlying, observed, started)
                record['ranking_memberships'] = memberships
                key = record['contract']
                if key in seen:
                    rejected += 1
                    continue
                seen.add(key)
                records.append(record)
                side_counts[side] += 1
                for flag in record['flags']:
                    flags[flag] = flags.get(flag, 0) + 1
            except (ValueError, TypeError):
                rejected += 1
    return dict(records=records, rejected=rejected, rows=side_counts, flags=flags)


def merge_parsed(parts):
    chosen = {}
    for part in parts:
        for record in part['records']:
            current = chosen.get(record['contract'])
            if current is None or (has_live_quote(record) and not has_live_quote(current)):
                chosen[record['contract']] = record
    records = list(chosen.values())
    side_counts, flags = {'call': 0, 'put': 0}, {}
    for record in records:
        side_counts[record['side']] += 1
        for flag in record['flags']:
            flags[flag] = flags.get(flag, 0) + 1
    return dict(records=records, rejected=max(part['rejected'] for part in parts),
                rows=side_counts, flags=flags)


def empty_sides(parsed):
    return tuple(sorted(side for side, count in parsed['rows'].items() if count == 0))


def confirmed_vendor_one_sided(parts):
    """Same side empty on two reads, and the listed side has a real bid or ask."""
    if len(parts) < 2 or any(part['rejected'] for part in parts):
        return False
    empties = [empty_sides(part) for part in parts]
    if any(len(sides) != 1 for sides in empties) or len(set(empties)) != 1:
        return False
    return any(has_live_quote(record) for part in parts for record in part['records'])


def quote_stripped_one_sided(parsed):
    populated = sum(parsed['rows'].values())
    return (len(empty_sides(parsed)) == 1 and populated > 0 and not parsed['rejected']
            and not any(has_live_quote(record) for record in parsed['records']))


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def timestamp(value):
    try:
        if isinstance(value, (int, float)):
            value = dt.datetime.fromtimestamp(value, dt.timezone.utc)
        elif hasattr(value, 'to_pydatetime'):
            value = value.to_pydatetime()
        elif isinstance(value, str):
            value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if not isinstance(value, dt.datetime) or value.tzinfo is None:
            return None
        return value.astimezone(dt.timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def normalize(ticker, expiry, side, row, underlying, observed, started):
    contract = row.get('contractSymbol')
    if not isinstance(contract, str) or not contract:
        raise ValueError('Missing contract identity')
    values = {k: number(row.get(k)) for k in ('strike', 'bid', 'ask', 'lastPrice',
              'impliedVolatility', 'volume', 'openInterest')}
    bid, ask = values['bid'], values['ask']
    flags = ['OPTION_QUOTE_CLOCK_UNKNOWN', 'CONTRACT_DELIVERABLE_UNVERIFIED']
    if bid is None or ask is None:
        flags.append('MISSING_BID_ASK')
    if bid == 0:
        flags.append('ZERO_BID')
    if bid is not None and ask is not None and (bid < 0 or ask < 0 or bid > ask):
        flags.append('INVALID_OR_CROSSED_MARKET')
    valid_market = bid is not None and ask is not None and 0 < bid <= ask
    if values['strike'] is None or values['strike'] <= 0:
        flags.append('INVALID_STRIKE')
    last_time = timestamp(row.get('lastTradeDate'))
    if last_time is None:
        flags.append('LAST_TRADE_TIME_UNKNOWN')
    elif (observed - dt.datetime.fromisoformat(last_time)).total_seconds() > 3 * 86400:
        flags.append('LAST_TRADE_OLDER_THAN_3_CALENDAR_DAYS')
    quote_time = timestamp(underlying.get('regularMarketTime'))
    spot = number(underlying.get('regularMarketPrice'))
    if spot is None or spot <= 0:
        flags.append('UNDERLYING_PRICE_MISSING')
    age = None if quote_time is None else (observed - dt.datetime.fromisoformat(quote_time)).total_seconds()
    if age is None:
        flags.append('UNDERLYING_TIME_UNKNOWN')
    elif age < 0 or age > 36 * 3600:
        flags.append('UNDERLYING_TIME_OUTSIDE_36H')
    vendor_symbol = underlying.get('symbol')
    if vendor_symbol != ticker:
        flags.append('UNDERLYING_SYMBOL_MISMATCH_OR_MISSING')
    return dict(ticker=ticker, expiry=expiry, side=side, contract=contract,
        source='Yahoo/yfinance', available_at=observed.isoformat(),
        request_started_at=started.isoformat(), collection_date=observed.astimezone(ZoneInfo('America/New_York')).date().isoformat(),
        values=values, midpoint=(bid + ask) / 2 if valid_market else None,
        spread=ask - bid if valid_market else None,
        last_trade_at=last_time, option_quote_at=None,
        currency=row.get('currency') if isinstance(row.get('currency'), str) else None,
        contract_size_label=row.get('contractSize') if isinstance(row.get('contractSize'), str) else None,
        multiplier=None, greeks=None,
        underlying=dict(symbol=vendor_symbol, price=spot, vendor_time=quote_time,
                        age_seconds=age, currency=underlying.get('currency'),
                        match_method='SAME_OPTION_RESPONSE_NOT_SYNCHRONOUS_QUOTE',
                        price_basis='VENDOR_SPOT_NOT_HISTORICAL_ADJUSTED_CLOSE'),
        pit_status='NOT_CERTIFIED', executable_quote=False, flags=flags)


def collect(config, dest, ticker_factory=None, clock=None, pause=time.sleep):
    if ticker_factory is None:
        import yfinance as yf
        ticker_factory = yf.Ticker
    if clock is None:
        clock = lambda: dt.datetime.now(dt.timezone.utc)
    validate_config(config)
    checks, count, tripped = [], 0, False
    today = clock().astimezone(ZoneInfo('America/New_York')).date()

    def read_once(obj, ticker, expiry, memberships):
        started = clock()
        chain = obj.option_chain(expiry)
        observed = clock()
        return parse_chain(ticker, expiry, chain, observed, started, memberships)

    def read_expiry(obj, ticker, expiry, memberships):
        # One confirmation read when the first payload is missing a side.
        # Opposite sides from the two reads are unioned; quotes are never invented.
        parts, error = [], None
        for attempt in (1, 2):
            try:
                parts.append(read_once(obj, ticker, expiry, memberships))
                if all(parts[-1]['rows'].values()):
                    break
                if attempt == 1:
                    pause(1)
            except Exception as exc:
                if 'RateLimit' in type(exc).__name__:
                    raise
                error = exc
                if parts or attempt == 2:
                    break
                pause(1)
        if not parts:
            raise error
        return merge_parsed(parts), parts

    def write_check(handle, ticker, expiry, parsed, selected, offered_count, extra):
        nonlocal count
        for record in parsed['records']:
            handle.write(json.dumps(record, allow_nan=False) + '\n')
            count += 1
        check = dict(ticker=ticker, expiry=expiry, selected_expiries=selected,
                     offered_expiry_count=offered_count, rows=parsed['rows'],
                     rejected_rows=parsed['rejected'], flag_counts=parsed['flags'])
        check.update(extra)
        checks.append(check)

    with (dest / 'options.jsonl').open('w') as handle:
        for ticker in config['tickers']:
            if tripped:
                checks.append(dict(ticker=ticker, status='NOT_ATTEMPTED_RATE_LIMIT'))
                continue
            try:
                obj = ticker_factory(ticker)
                offered = list(obj.options)
                selected = select_expiries(offered, today, config['target_dtes'])
                if not selected:
                    checks.append(dict(ticker=ticker, status='EMPTY_UNVERIFIED', selected_expiries=[]))
                    continue
            except Exception as exc:
                tripped = 'RateLimit' in type(exc).__name__
                checks.append(dict(ticker=ticker, status='RATE_LIMIT' if tripped else 'EXPIRIES_ERROR'))
                continue
            memberships = config.get('rsr_top30', {}).get('memberships', {}).get(ticker, [])
            pending = [(expiry, [], obj) for expiry in selected]
            done = set()
            while pending and not tripped:
                expiry, notes, source = pending.pop(0)
                if expiry in done:
                    attached = False
                    for check in checks:
                        if check.get('ticker') == ticker and check.get('expiry') == expiry:
                            check.setdefault('discarded_expiries', []).extend(notes)
                            attached = True
                    if not attached and notes:
                        checks.append(dict(ticker=ticker, expiry=expiry, selected_expiries=selected,
                                           status='PARTIAL', reason='UNRESOLVED_REPLACEMENT',
                                           discarded_expiries=notes))
                    continue
                done.add(expiry)
                try:
                    parsed, parts = read_expiry(source, ticker, expiry, memberships)
                except Exception as exc:
                    tripped = 'RateLimit' in type(exc).__name__
                    checks.append(dict(ticker=ticker, expiry=expiry, selected_expiries=selected,
                                       status='RATE_LIMIT' if tripped else 'CHAIN_ERROR'))
                    pause(0.5)
                    continue
                if all(parsed['rows'].values()):
                    status = 'OK' if not parsed['rejected'] else 'PARTIAL'
                    extra = dict(status=status, attempts=len(parts))
                    if notes:
                        extra['discarded_expiries'] = notes
                    if status != 'OK':
                        extra['reason'] = 'REJECTED_CONTRACTS'
                    write_check(handle, ticker, expiry, parsed, selected, len(list(source.options)), extra)
                    pause(0.5)
                    continue
                try:
                    fresh = ticker_factory(ticker)
                    refreshed = list(fresh.options)
                except Exception as exc:
                    tripped = 'RateLimit' in type(exc).__name__
                    checks.append(dict(ticker=ticker, expiry=expiry, selected_expiries=selected,
                                       rows=parsed['rows'], rejected_rows=parsed['rejected'],
                                       status='RATE_LIMIT' if tripped else 'CONFIRM_ERROR'))
                    pause(0.5)
                    continue
                if (confirmed_vendor_one_sided(parts) and expiry in refreshed):
                    extra = dict(status='OK', attempts=len(parts),
                                 vendor_empty_sides=list(empty_sides(parts[0])))
                    if notes:
                        extra['discarded_expiries'] = notes
                    write_check(handle, ticker, expiry, parsed, selected, len(refreshed), extra)
                    pause(0.5)
                    continue
                reason = None
                if expiry not in refreshed:
                    reason = 'NOT_LISTED_ON_CONFIRM'
                elif quote_stripped_one_sided(parsed):
                    reason = 'QUOTE_STRIPPED_ONE_SIDED'
                # One substitute hop. A bad substitute stays PARTIAL rather than walking the chain.
                if reason and not parsed['rejected'] and not notes:
                    repl = replacement_expiry(refreshed, today, config['target_dtes'], expiry, done)
                    note = dict(expiry=expiry, reason=reason, rows=parsed['rows'],
                                attempts=len(parts), rejected_rows=parsed['rejected'])
                    queued = False
                    if repl:
                        for queued_expiry, queued_notes, _queued_source in pending:
                            if queued_expiry == repl:
                                queued_notes.append(note)
                                queued = True
                                break
                        if not queued and repl in done:
                            for check in checks:
                                if check.get('ticker') == ticker and check.get('expiry') == repl:
                                    check.setdefault('discarded_expiries', []).append(note)
                                    queued = True
                        if not queued and repl not in done:
                            pending.insert(0, (repl, notes + [note], fresh))
                            queued = True
                    if queued:
                        pause(0.5)
                        continue
                if not reason:
                    reason = 'REJECTED_CONTRACTS' if parsed['rejected'] else (
                        'EMPTY_CHAIN' if not any(parsed['rows'].values()) else 'ONE_SIDED_UNCONFIRMED')
                extra = dict(status='PARTIAL', attempts=len(parts), reason=reason)
                if notes:
                    extra['discarded_expiries'] = notes
                write_check(handle, ticker, expiry, parsed, selected, len(refreshed), extra)
                pause(0.5)
            if tripped:
                for expiry, _notes, _source in pending:
                    checks.append(dict(ticker=ticker, expiry=expiry, status='NOT_ATTEMPTED_RATE_LIMIT'))
    # Empty legacy files keep a single private batch transport and importer.
    (dest / 'prices.jsonl').write_text('')
    (dest / 'events.jsonl').write_text('')
    status = 'COMPLETE' if count and all(c['status'] == 'OK' for c in checks) else 'PARTIAL'
    (dest / 'quality.json').write_text(json.dumps(dict(status=status, kind='options',
        checks=checks, option_rows=count, expected_tickers=len(config['tickers']),
        target_dtes=config['target_dtes'], collection_date=today.isoformat(),
        rsr_top30=config.get('rsr_top30'),
        research_gate='RESEARCH_ONLY_UNVERIFIED', cross_vendor_random_check='PENDING',
        coverage='Vendor strikes for the nearest listed expiry, or the next listed expiry when that payload is unusable',
        gap_policy='No historical backfill; never relabel collection day as an earlier session; no synthetic quotes'), indent=2) + '\n')
    return status
