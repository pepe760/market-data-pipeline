import datetime as dt
import io
import json
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

import intraday15m as c
import stock_capture as s


def rate_error(code, retry_after=None):
    headers=Message()
    if retry_after is not None:headers['Retry-After']=retry_after
    return urllib.error.HTTPError('https://example.invalid',code,'redacted',headers,None)


class HTTPTests(unittest.TestCase):
    def request(self, responses, http=None):
        http=http or c.HTTP()
        with patch.object(c.urllib.request,'urlopen',side_effect=responses) as get,patch.object(c.time,'sleep') as sleep:
            result=http.get('https://example.invalid')
        return result,http,get.call_count,[call.args[0] for call in sleep.call_args_list]

    def test_429_retry_after_seconds_then_success(self):
        result,http,calls,sleeps=self.request([rate_error(429,'3'),io.BytesIO(b'ok')])
        self.assertEqual(result,b'ok');self.assertEqual(calls,2);self.assertEqual(http.used_calls,2)
        self.assertIn(3,sleeps);self.assertEqual(http.retry_events,[{'reason':'HTTP_429','delay_seconds':3.0}])

    def test_429_http_date_then_success(self):
        clock=dt.datetime(2026,10,30,12,0,tzinfo=c.UTC)
        with patch.object(c,'now',return_value=clock):
            _result,http,calls,sleeps=self.request([rate_error(429,'Fri, 30 Oct 2026 12:00:05 GMT'),io.BytesIO(b'ok')])
        self.assertEqual(calls,2);self.assertIn(5,sleeps)

    def test_429_exhaustion_is_bounded_and_precise(self):
        http=c.HTTP()
        with patch.object(c.urllib.request,'urlopen',side_effect=[rate_error(429)]*3) as get,patch.object(c.time,'sleep'):
            with self.assertRaisesRegex(c.BudgetStop,'HTTP_429_RETRIES_EXHAUSTED'):http.get('https://example.invalid')
        self.assertEqual(get.call_count,3);self.assertEqual(http.used_calls,3)
        self.assertEqual([x['delay_seconds'] for x in http.retry_events],[2,4])

    def test_long_retry_after_stops_without_retrying_early(self):
        http=c.HTTP()
        with patch.object(c.urllib.request,'urlopen',side_effect=[rate_error(429,'31')]) as get,patch.object(c.time,'sleep'):
            with self.assertRaisesRegex(c.BudgetStop,'HTTP_429_RETRY_AFTER_EXCEEDS_BOUND'):http.get('https://example.invalid')
        self.assertEqual(get.call_count,1);self.assertEqual(http.retry_events,[])

    def test_451_is_never_retried(self):
        http=c.HTTP()
        with patch.object(c.urllib.request,'urlopen',side_effect=rate_error(451)) as get,patch.object(c.time,'sleep'):
            with self.assertRaisesRegex(c.BudgetStop,'HTTP_451_REGION_BLOCK'):http.get('https://example.invalid')
        self.assertEqual(get.call_count,1);self.assertEqual(http.retry_events,[])

    def test_retry_consumes_budget_and_cannot_escape_it(self):
        http=c.HTTP(calls=1)
        with patch.object(c.urllib.request,'urlopen',side_effect=rate_error(429)) as get,patch.object(c.time,'sleep'):
            with self.assertRaisesRegex(c.BudgetStop,'REQUEST_BUDGET_EXHAUSTED'):http.get('https://example.invalid')
        self.assertEqual(get.call_count,1)

    def test_other_http_error_does_not_retry(self):
        with patch.object(c.urllib.request,'urlopen',side_effect=rate_error(404)) as get,patch.object(c.time,'sleep'):
            with self.assertRaisesRegex(c.SourceError,'HTTP_404'):c.HTTP().get('https://example.invalid')
        self.assertEqual(get.call_count,1)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.start=1735828200;self.task={'kind':'stocks','symbol':'SPY','grid':[self.start,self.start+900]}
        self.observed='2026-10-30T12:00:00+00:00'

    def parse(self, values, timestamps=None):
        timestamps=timestamps if timestamps is not None else [self.start+i*900 for i in range(len(values))]
        quote={key:[v[i] for v in values] for i,key in enumerate(['open','high','low','close','volume'])}
        blob=json.dumps({'chart':{'result':[{'meta':{'symbol':'SPY'},'timestamp':timestamps,'indicators':{'quote':[quote]}}]}}).encode()
        rows,q=c.parse_stocks(blob,self.task,self.observed)
        q['status']='COMPLETE' if rows and not q['missing'] and not q['rejected'] else 'PARTIAL'
        return rows,q

    def test_null_bar_is_missing_not_filled_and_capture_stays_qualified_as_gap(self):
        rows,q=self.parse([[100,102,99,101,10],[None,None,None,None,None]])
        self.assertEqual(len(rows),1);self.assertEqual(q['rejection_counts'],{'NULL_OHLCV':1})
        self.assertEqual(q['missing'],[self.start+900]);self.assertEqual(s.stock_task_capture_status(q),'CAPTURED_WITH_DATA_GAPS')
        out=s.summarize_stock_capture([q],1,0)
        self.assertEqual(out['data_status'],'PARTIAL');self.assertEqual(out['capture_status'],'CAPTURED_WITH_DATA_GAPS')

    def test_all_null_and_empty_history_explicit_no_valid_bars(self):
        for values in ([[None]*5],[]):
            with self.subTest(values=values):
                rows,q=self.parse(values)
                self.assertEqual(rows,[]);self.assertIn(q['data_availability'],['NO_VALID_BARS','NO_HISTORY_IN_WINDOW'])
                out=s.summarize_stock_capture([q],1,0)
                self.assertEqual(out['no_valid_bars'],1);self.assertEqual(out['data_status'],'PARTIAL')
                self.assertEqual(out['capture_status'],'CAPTURED_WITH_DATA_GAPS')

    def test_bad_ohlcv_reasons_cannot_become_capture_success(self):
        cases=[([100,99,98,101,10],'INVALID_MARKET'),([float('nan'),102,99,101,10],'NONFINITE_OHLCV'),
               ([None,99,98,101,10],'INVALID_MARKET'),
               ([None,float('inf'),98,101,10],'NONFINITE_OHLCV'),([None,102,99,101,-1],'INVALID_MARKET')]
        for values,reason in cases:
            with self.subTest(reason=reason):
                rows,q=self.parse([values]);self.assertEqual(rows,[])
                self.assertEqual(q['rejection_counts'],{reason:1});self.assertEqual(s.stock_task_capture_status(q),'CAPTURE_FAILED')

    def test_bool_and_nonnumeric_ohlcv_are_schema_errors_even_outside_grid(self):
        for value in (True,False,'100','bad',{},[]):
            for timestamps in ([self.start],[self.start-900]):
                with self.subTest(value=value,timestamps=timestamps):
                    with self.assertRaisesRegex(c.SourceError,'OHLCV_TYPE'):
                        self.parse([[value,102,99,101,10]],timestamps)

    def test_timestamp_schema_checked_before_outside_grid_filter(self):
        for timestamp in (str(self.start),None,True,float('nan'),float('inf'),float(self.start),-900):
            with self.subTest(timestamp=timestamp):
                with self.assertRaisesRegex(c.SourceError,'TIMESTAMP_SCHEMA'):
                    self.parse([[100,102,99,101,10]],[timestamp])

    def test_irregular_native_integer_is_excluded_without_rounding_or_losing_grid_rows(self):
        rows,q=self.parse([[100,102,99,101,10]]*3,[self.start,self.start+1,self.start+900])
        self.assertEqual(len(rows),2);self.assertEqual(q['status'],'PARTIAL')
        self.assertEqual(q['rejected'],[{'row':1,'reason':'UNALIGNED_SOURCE_TIMESTAMP_EXCLUDED','original_timestamp':self.start+1}])
        self.assertEqual(s.stock_task_capture_status(q),'CAPTURED_WITH_DATA_GAPS')
        rows,q=self.parse([[100,102,99,101,10],[100,99,98,101,10]],[self.start,self.start+1])
        self.assertEqual(len(rows),1);self.assertEqual(q['rejected'][0]['reason'],'INVALID_MARKET')
        self.assertEqual(s.stock_task_capture_status(q),'CAPTURE_FAILED')

    def test_valid_extra_close_auction_timestamp_is_filtered(self):
        rows,q=self.parse([[100,102,99,101,10]]*3,[self.start,self.start+900,self.start+1800])
        self.assertEqual(len(rows),2);self.assertEqual(q['status'],'COMPLETE')

    def test_empty_history_requires_explicit_timestamp_and_required_arrays(self):
        def encoded(result):return json.dumps({'chart':{'result':[result]}}).encode()
        valid={'meta':{'symbol':'SPY'},'timestamp':[],
               'indicators':{'quote':[{key:[] for key in ['open','high','low','close','volume']}]}}
        rows,q=c.parse_stocks(encoded(valid),self.task,self.observed)
        self.assertEqual(rows,[]);self.assertEqual(q['data_availability'],'NO_HISTORY_IN_WINDOW')
        for mutation,reason in [('timestamp','TIMESTAMP_SCHEMA'),('arrays','OHLCV_SCHEMA'),('quote','STOCK_SCHEMA')]:
            bad=json.loads(json.dumps(valid))
            if mutation=='timestamp':del bad['timestamp']
            elif mutation=='arrays':bad['indicators']['quote']=[{}]
            else:bad['indicators']['quote']=[]
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(c.SourceError,reason):c.parse_stocks(encoded(bad),self.task,self.observed)

    def test_malformed_nested_shapes_are_source_errors(self):
        for payload in ([],{}, {'chart':[]},{'chart':{'result':[]}},
                        {'chart':{'result':[None]}},{'chart':{'result':[{'meta':None}]}}):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(c.SourceError,'STOCK_SCHEMA'):
                    c.parse_stocks(json.dumps(payload).encode(),self.task,self.observed)

    def test_duplicate_is_bad_even_when_first_observation_is_null(self):
        _rows,q=self.parse([[None]*5,[100,102,99,101,10]],[self.start,self.start])
        self.assertEqual(q['rejection_counts'],{'DUPLICATE_TIMESTAMP':1,'NULL_OHLCV':1})
        self.assertEqual(s.stock_task_capture_status(q),'CAPTURE_FAILED')

    def test_errors_unattempted_and_legacy_rejections_are_not_success(self):
        for q in [{'status':'SOURCE_FAILED','reason':'SYMBOL_MISMATCH'},
                  {'status':'DEFERRED_SOURCE_OR_BUDGET','reason':'HTTP_451_REGION_BLOCK'},
                  {'status':'PARTIAL','source_observation':'OBSERVED','reason':'HTTP_451_REGION_BLOCK'},
                  {'status':'PARTIAL','rejected':[{'reason':'INVALID_OR_DUPLICATE_BAR'}]}]:
            with self.subTest(q=q):self.assertEqual(s.stock_task_capture_status(q),'CAPTURE_FAILED')
        _rows,q=self.parse([[100,102,99,101,10]]*2)
        self.assertEqual(s.summarize_stock_capture([q],2,1)['capture_status'],'CAPTURE_FAILED')
        self.assertEqual(s.summarize_stock_capture([q],2,0,already_complete=1)['capture_status'],'CAPTURED_COMPLETE')

    def test_accounting_must_cover_all_configured_tasks(self):
        with self.assertRaisesRegex(ValueError,'stock capture accounting'):s.summarize_stock_capture([],232,0)


if __name__=='__main__':unittest.main()
