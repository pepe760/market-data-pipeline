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
            for expiry in selected:
                if tripped:
                    checks.append(dict(ticker=ticker, expiry=expiry, status='NOT_ATTEMPTED_RATE_LIMIT'))
                    continue
                try:
                    started = clock()
                    chain = obj.option_chain(expiry)
                    observed = clock()
                    underlying = chain.underlying or {}
                    seen, rejected, side_counts, flags = set(), 0, {}, {}
                    for side, frame in [('call', chain.calls), ('put', chain.puts)]:
                        side_counts[side] = 0
                        if frame is None:
                            continue
                        for _, row in frame.iterrows():
                            try:
                                record = normalize(ticker, expiry, side, row, underlying, observed, started)
                                record['ranking_memberships'] = config.get('rsr_top30',{}).get('memberships',{}).get(ticker,[])
                                key = record['contract']
                                if key in seen:
                                    rejected += 1
                                    continue
                                seen.add(key)
                                handle.write(json.dumps(record, allow_nan=False) + '\n')
                                count += 1
                                side_counts[side] += 1
                                for flag in record['flags']:
                                    flags[flag] = flags.get(flag, 0) + 1
                            except (ValueError, TypeError):
                                rejected += 1
                    checks.append(dict(ticker=ticker, expiry=expiry, selected_expiries=selected,
                        offered_expiry_count=len(offered), rows=side_counts, rejected_rows=rejected,
                        flag_counts=flags, status='OK' if all(side_counts.values()) and not rejected else 'PARTIAL'))
                except Exception as exc:
                    tripped = 'RateLimit' in type(exc).__name__
                    checks.append(dict(ticker=ticker, expiry=expiry, status='RATE_LIMIT' if tripped else 'CHAIN_ERROR'))
                pause(0.5)
    # Empty legacy files keep a single private batch transport and importer.
    (dest / 'prices.jsonl').write_text('')
    (dest / 'events.jsonl').write_text('')
    status = 'COMPLETE' if count and all(c['status'] == 'OK' for c in checks) else 'PARTIAL'
    (dest / 'quality.json').write_text(json.dumps(dict(status=status, kind='options',
        checks=checks, option_rows=count, expected_tickers=len(config['tickers']),
        target_dtes=config['target_dtes'], collection_date=today.isoformat(),
        rsr_top30=config.get('rsr_top30'),
        research_gate='RESEARCH_ONLY_UNVERIFIED', cross_vendor_random_check='PENDING',
        coverage='All vendor-returned strikes on selected expiries only; not all listed expiries',
        gap_policy='No historical backfill; never relabel collection day as an earlier session'), indent=2) + '\n')
    return status
