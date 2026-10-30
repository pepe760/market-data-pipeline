"""Pure observation/coverage summaries. No collection, price filling or exit policy."""
from collections import Counter
import datetime as dt
import hashlib
import json


def observe_empty_history_response(blob,task,observed,strict_parser_reason):
    """Recognize one constrained native Yahoo empty-history shape, never a bar.

    Only the explicit broad source-capture contract calls this after the strict
    parser rejects missing timestamp/arrays. It does not infer no trading.
    """
    if strict_parser_reason not in ('TIMESTAMP_SCHEMA','OHLCV_SCHEMA'):return None
    if task.get('kind')!='stocks':return None
    try:
        payload=json.loads(blob);chart=payload['chart'];results=chart['result']
        if chart.get('error') is not None or not isinstance(results,list) or len(results)!=1:return None
        result=results[0];meta=result['meta']
        if (meta.get('symbol')!=task['symbol'].replace('.','-') or meta.get('instrumentType') not in ('EQUITY','ETF')
            or meta.get('dataGranularity')!='15m' or meta.get('exchangeTimezoneName')!='America/New_York'
            or type(meta.get('regularMarketTime')) is not int or meta['regularMarketTime']<=0
            or not isinstance(meta.get('validRanges'),list) or not meta['validRanges']
            or any(not isinstance(x,str) or not x for x in meta['validRanges'])):return None
        stamps=result.get('timestamp')
        if stamps is not None and stamps!=[]:return None
        if result.get('indicators',{}).get('quote')!=[{}]:return None
        clock=dt.datetime.fromisoformat(observed)
        if clock.tzinfo is None or meta['regularMarketTime']>clock.timestamp():return None
        grid=task['grid']
        if not grid or any(type(x) is not int or x<0 or x%900 for x in grid) or len(set(grid))!=len(grid):return None
    except (ValueError,KeyError,TypeError,AttributeError):return None
    return [],{'status':'PARTIAL','expected':len(grid),'saved':0,'missing':sorted(grid),'rejected':[],
        'source_observation':'OBSERVED','data_availability':'NATIVE_EMPTY_HISTORY_RESPONSE_UNVERIFIED',
        'empty_history_profile':'YAHOO_EQUITY_15M_EMPTY_RESULT_V1','strict_parser_reason':strict_parser_reason,
        'fetched_at':observed,'raw_sha256':hashlib.sha256(blob).hexdigest(),'independent_crosscheck':'PENDING',
        'identity':'CURRENT_TICKER_MATCH_ONLY_NOT_PIT','qualification':'NO_BARS_RETURNED; NOT_NO_TRADING_OR_DELISTING_EVIDENCE; NO_SYNTHETIC_PRICES'}


def stock_task_capture_status(quality):
    if quality.get('reason') or quality.get('error_class'):return 'CAPTURE_FAILED'
    if quality.get('source_observation')!='OBSERVED':return 'CAPTURE_FAILED'
    expected,saved,missing=quality.get('expected'),quality.get('saved'),quality.get('missing')
    if (type(expected) is not int or expected<=0 or type(saved) is not int or not 0<=saved<=expected
        or not isinstance(missing,list) or any(type(x) is not int or x<0 for x in missing)
        or len(missing)!=len(set(missing)) or saved+len(missing)!=expected
        or not isinstance(quality.get('rejected'),list)):
        return 'CAPTURE_FAILED'
    status=quality.get('status')
    if status=='COMPLETE' and not quality.get('missing') and not quality.get('rejected'):
        return 'CAPTURED_COMPLETE'
    # Legacy undifferentiated errors cannot be assumed to be benign nulls.
    if status!='PARTIAL':return 'CAPTURE_FAILED'
    if any(row.get('reason') not in ('NULL_OHLCV','UNALIGNED_SOURCE_TIMESTAMP_EXCLUDED') for row in quality.get('rejected',[])):
        return 'CAPTURE_FAILED'
    return 'CAPTURED_WITH_DATA_GAPS'


def summarize_stock_capture(checks, configured, unattempted, already_complete=0):
    if (any(not isinstance(n,int) or isinstance(n,bool) or n<0 for n in (configured,unattempted,already_complete))
        or len(checks)+unattempted+already_complete!=configured):
        raise ValueError('stock capture accounting')
    states=Counter(stock_task_capture_status(q) for q in checks)
    states['CAPTURED_COMPLETE']+=already_complete
    failed=bool(unattempted or states['CAPTURE_FAILED'])
    gaps=bool(states['CAPTURED_WITH_DATA_GAPS'])
    reasons=Counter(r.get('reason','UNKNOWN') for q in checks for r in q.get('rejected',[]))
    return {'policy':'OBSERVED_SOURCE_CAPTURE_V1_NO_PRICE_FILL',
            'capture_status':'CAPTURE_FAILED' if failed else 'CAPTURED_WITH_DATA_GAPS' if gaps else 'CAPTURED_COMPLETE',
            'data_status':'PARTIAL' if failed or gaps else 'COMPLETE',
            'configured_symbols':configured,'observed_checks':len(checks),'already_complete':already_complete,
            'unattempted':unattempted,'task_capture_counts':dict(states),
            'no_valid_bars':sum(q.get('saved')==0 for q in checks if q.get('source_observation')=='OBSERVED'),
            'empty_history_response_count':sum(bool(q.get('empty_history_profile')) for q in checks),
            'missing_slots':sum(len(q.get('missing',[])) for q in checks),
            'rejected_rows':sum(len(q.get('rejected',[])) for q in checks),'rejection_counts':dict(reasons)}
