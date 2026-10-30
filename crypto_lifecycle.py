"""Small, explicit crypto lifecycle exceptions; no network, price or identity inference.

This registry currently knows only the officially announced EPICUSDT spot
retirement. An absent override is not certification of another market's lifetime.
Original archive integers are returned unchanged by the close-clock evaluator.
"""
import datetime as dt

UTC = dt.timezone.utc
POLICY_VERSION = 'crypto-lifecycle-20261030-v1'
EPIC_SPOT_RETIRED_AT = dt.datetime(2026, 10, 28, 3, tzinfo=UTC)
EPIC_RETIREMENT_SOURCE = 'https://www.binance.com/en-TR/support/announcement/detail/290a660c0b2e4e888c582cd29ecf9ecc'
EPIC_RETIREMENT_POLICY = 'EPICUSDT_SPOT_OFFICIAL_RETIREMENT_20261028T030000Z'
TERMINAL_PRECISION_QUALIFICATION = (
    'MILLISECOND_LAST_TICK_LABEL_IN_MICROSECOND_FIELD_EPIC_TERMINAL_ONLY; '
    'RAW_CLOCKS_PRESERVED; NOT_TRADE_COMPLETENESS_OR_PRICE_ACCURACY_CERTIFIED'
)


def _task_clock(task):
    if task.get('kind') != 'crypto':
        raise ValueError('crypto_task')
    date = dt.date.fromisoformat(task['date'])
    if date.isoformat() != task['date']:
        raise ValueError('date')
    interval = task.get('interval', '15m')
    if interval not in ('15m', '1h'):
        raise ValueError('interval')
    start = int(dt.datetime.combine(date, dt.time(), UTC).timestamp())
    return start, {'15m': 900, '1h': 3600}[interval]


def _epic_spot(task):
    return task.get('symbol') == 'EPICUSDT' and task.get('market') == 'spot'


def lifecycle_policy(task):
    """Return this date's expected UTC bar-start seconds and download decision.

    Retirement removes expected slots; it neither creates observations nor makes
    missing/rejected pre-retirement bars complete. Fully retired dates need no
    HTTP request and are distinct from a technically complete 24-hour archive.
    """
    start, step = _task_clock(task)
    full = list(range(start, start + 86400, step))
    retired_at = int(EPIC_SPOT_RETIRED_AT.timestamp()) if _epic_spot(task) else None
    expected = [slot for slot in full if retired_at is None or slot < retired_at]
    retired = [slot for slot in full if retired_at is not None and slot >= retired_at]
    status = ('RETIRED_NOT_EXPECTED' if not expected else
              'LIFECYCLE_LIMITED_EXPECTED' if retired else 'FULL_DAY_EXPECTED')
    return {
        'policy_version': POLICY_VERSION,
        'policy_id': EPIC_RETIREMENT_POLICY if _epic_spot(task) else 'NO_KNOWN_LIFECYCLE_OVERRIDE',
        'symbol': task['symbol'], 'market': task['market'], 'date': task['date'],
        'interval_seconds': step, 'status': status,
        'expected_slots': expected, 'retired_not_expected_slots': retired,
        'expected': len(expected), 'nominal_full_day_slots': len(full),
        'should_fetch': bool(expected),
        'retired_at_utc': EPIC_SPOT_RETIRED_AT.isoformat() if _epic_spot(task) else None,
        'source_url': EPIC_RETIREMENT_SOURCE if _epic_spot(task) else None,
        'qualification': ('SINGLE_OFFICIAL_RETIREMENT_OVERRIDE; '
                          'NO_INCEPTION_OR_OTHER_MARKET_LIFECYCLE_CERTIFICATION; '
                          'NO_FORWARD_FILL_OR_IDENTITY_RELABEL'),
    }


def close_clock_policy(task, raw_open, raw_close, divisor):
    """Evaluate exact source close integers without rewriting or rounding them.

    A strict last tick remains the default. The only alternate allowed label is
    EPICUSDT spot's 2026-10-28 02:00--03:00 UTC terminal 1h bucket: a millisecond
    last-tick label encoded in the archive's microsecond field. Callers must keep
    this result on accepted rows and still enforce grid, OHLCV and missing checks.
    """
    _start, step = _task_clock(task)
    result = {
        'allowed': False, 'raw_open_timestamp': raw_open,
        'raw_close_timestamp': raw_close, 'raw_timestamp_divisor': divisor,
        'precision_qualification': 'REJECTED_CLOSE_CLOCK',
        'close_clock_policy_version': POLICY_VERSION,
        'close_clock_exception': False,
    }
    if (not isinstance(raw_open, int) or isinstance(raw_open, bool) or
            not isinstance(raw_close, int) or isinstance(raw_close, bool) or
            not isinstance(divisor, int) or isinstance(divisor, bool) or
            divisor not in (1000, 1_000_000)):
        return result
    if raw_open % (step * divisor):
        return result
    if raw_close == raw_open + step * divisor - 1:
        result.update(allowed=True, precision_qualification=(
            'EXACT_MICROSECOND_LAST_TICK_LABEL' if divisor == 1_000_000 else
            'EXACT_MILLISECOND_LAST_TICK_LABEL'))
        return result
    terminal_end = int(EPIC_SPOT_RETIRED_AT.timestamp()) * 1_000_000
    if (_epic_spot(task) and task['date'] == '2026-10-28' and
            task.get('interval', '15m') == '1h' and divisor == 1_000_000 and
            raw_open == terminal_end - 3600 * 1_000_000 and
            raw_close == terminal_end - 1000):
        result.update(allowed=True, close_clock_exception=True,
                      precision_qualification=TERMINAL_PRECISION_QUALIFICATION)
    return result
