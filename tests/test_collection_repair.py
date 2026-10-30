import copy
import importlib.util
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlparse
import zipfile

import broad_collect as b
import intraday15m as c
import stock_capture as s


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.start=1735828200
        self.task={'kind':'stocks','symbol':'SPY','grid':[self.start,self.start+900]}
        self.observed='2026-10-30T00:00:00+00:00'

    def empty(self):
        return {'chart':{'error':None,'result':[{'meta':{'symbol':'SPY','instrumentType':'EQUITY',
            'dataGranularity':'15m','exchangeTimezoneName':'America/New_York','validRanges':['1d','max'],
            'regularMarketTime':self.start},'indicators':{'quote':[{}]}}]}}

    def test_exact_empty_profile_is_observation_not_price_or_no_trade_evidence(self):
        blob=json.dumps(self.empty()).encode()
        with self.assertRaises(c.SourceError):c.parse_stocks(blob,self.task,self.observed)
        rows,q=s.observe_empty_history_response(blob,self.task,self.observed,'TIMESTAMP_SCHEMA')
        self.assertEqual(rows,[]);self.assertEqual(q['saved'],0);self.assertEqual(q['missing'],self.task['grid'])
        self.assertEqual(q['status'],'PARTIAL');self.assertEqual(q['strict_parser_reason'],'TIMESTAMP_SCHEMA')
        self.assertEqual(s.stock_task_capture_status(q),'CAPTURED_WITH_DATA_GAPS')
        self.assertEqual(s.summarize_stock_capture([q],1,0)['empty_history_response_count'],1)

    def test_empty_profile_does_not_excuse_wrong_identity_bad_metadata_or_price_arrays(self):
        changes=[('symbol','WRONG'),('instrumentType','CRYPTOCURRENCY'),('dataGranularity','1m'),
                 ('exchangeTimezoneName',None),('regularMarketTime',True),('validRanges',[])]
        for key,value in changes:
            payload=self.empty();payload['chart']['result'][0]['meta'][key]=value
            self.assertIsNone(s.observe_empty_history_response(json.dumps(payload).encode(),self.task,self.observed,'TIMESTAMP_SCHEMA'))
        for stamp in (['corrupt'],[True],[self.start]):
            payload=self.empty();payload['chart']['result'][0]['timestamp']=stamp
            self.assertIsNone(s.observe_empty_history_response(json.dumps(payload).encode(),self.task,self.observed,'TIMESTAMP_SCHEMA'))
        payload=self.empty();payload['chart']['result'][0]['indicators']['quote']=[{'close':[100]}]
        self.assertIsNone(s.observe_empty_history_response(json.dumps(payload).encode(),self.task,self.observed,'OHLCV_SCHEMA'))
        self.assertIsNone(s.observe_empty_history_response(b'{}',self.task,self.observed,'TIMESTAMP_SCHEMA'))

    def test_forged_complete_or_incoherent_capture_counters_fail(self):
        for q in ({'status':'COMPLETE'}, {'source_observation':'OBSERVED','status':'COMPLETE','expected':2,'saved':1,'missing':[],'rejected':[]}):
            self.assertEqual(s.stock_task_capture_status(q),'CAPTURE_FAILED')

    def test_checkpoint_complete_noop_and_real_source_faults_have_different_exits(self):
        q={'lane':'stocks15m','status':'COMPLETE','bars':0,'already_complete':2,'configured_symbols':2,
           'capture':s.summarize_stock_capture([],2,0,2)}
        self.assertEqual(b.run_exit_code(q,'observed-source'),0)
        q['already_complete']=0;self.assertEqual(b.run_exit_code(q,'observed-source'),2)
        q={'lane':'stocks15m','status':'PARTIAL','bars':10,'already_complete':0,'configured_symbols':2,
           'capture':s.summarize_stock_capture([{'status':'DEFERRED_SOURCE_OR_BUDGET','reason':'HTTP_451_REGION_BLOCK'}],2,1)}
        self.assertEqual(b.run_exit_code(q,'observed-source'),2)
        self.assertEqual(b.run_exit_code(q,'strict'),2)

    def test_retired_market_makes_no_http_request_and_does_not_count_as_active(self):
        class HTTP:
            used_calls=used_bytes=0
            def __init__(self,*a):pass
            def get(self,url):raise AssertionError('retired market must not be fetched')
        cfg={'schema_version':1,'crypto':['EPICUSDT'],'crypto_asset_ids':{'EPICUSDT':'epic-chain'},'observed_at':self.observed}
        with tempfile.TemporaryDirectory() as tmp,patch.object(c,'HTTP',HTTP):
            out,q=b.collect(cfg,Path(tmp),'crypto1h',date='2026-10-29')
            self.assertEqual(q['retired_not_expected_symbols'],1);self.assertEqual(q['active_symbols_at_day_end'],0)
            self.assertEqual(q['qualified_symbols'],0);self.assertEqual(q['bars'],0);self.assertEqual(q['requests'],0)
            self.assertEqual(q['checks'][0]['status'],'RETIRED_NOT_EXPECTED');c.verify(out)
            self.assertTrue((out/'crypto_lifecycle.py').exists());self.assertTrue((out/'stock_capture.py').exists())
            self.assertEqual(json.loads((Path(tmp)/'checkpoint.json').read_text())['completed'],[])

    def test_known_terminal_clock_is_accepted_only_with_lifecycle_and_raw_provenance(self):
        task={'kind':'crypto','market':'spot','symbol':'EPICUSDT','date':'2026-10-28','interval':'1h'}
        start=int(dt.datetime(2026,10,28,tzinfo=c.UTC).timestamp())*1000000
        rows=[]
        for hour in range(3):
            stamp=start+hour*3600*1000000;close=stamp+3600*1000000-(1000 if hour==2 else 1)
            rows.append(','.join(map(str,[stamp,10,12,9,11,50,close,500,5,20,200,0])))
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:z.writestr('EPICUSDT-1h-2026-10-28.csv','\n'.join(rows))
        bars,q=c.parse_crypto(stream.getvalue(),task,self.observed)
        self.assertEqual(len(bars),3);self.assertEqual(q['expected'],3);self.assertEqual(q['retired_not_expected_slots'],21)
        self.assertEqual(q['missing'],[]);self.assertEqual(q['rejected'],[])
        self.assertTrue(bars[-1]['close_clock_exception']);self.assertEqual(bars[-1]['raw_close_timestamp'],start+3*3600*1000000-1000)

    @unittest.skipUnless(importlib.util.find_spec('pyarrow'),'optional parquet dependency; full CI installs intraday requirements')
    def test_partial_stock_observations_never_enter_complete_checkpoint(self):
        grid=self.task['grid']
        class HTTP:
            def __init__(self,*a):self.used_calls=self.used_bytes=0;self.retry_events=[]
            def get(self,url):
                symbol=unquote(urlparse(url).path.rsplit('/',1)[-1]);self.used_calls+=1
                quote={key:([100,100] if key!='volume' else [10,10]) for key in ('open','high','low','close','volume')}
                if symbol=='EMPTY':quote={key:[None,None] for key in quote}
                blob=json.dumps({'chart':{'error':None,'result':[{'meta':{'symbol':symbol},'timestamp':grid,'indicators':{'quote':[quote]}}]}}).encode()
                self.used_bytes+=len(blob);return blob
        with tempfile.TemporaryDirectory() as tmp,patch.object(c,'HTTP',HTTP),patch.object(c,'stock_grid',return_value=grid):
            root=Path(tmp);out,q=b.collect({'schema_version':1,'stocks':['SPY','EMPTY']},root,'stocks15m',shards=1,stock_capture_policy='observed-source')
            self.assertEqual(q['status'],'PARTIAL');self.assertEqual(q['qualified_symbols'],1)
            self.assertEqual(q['capture']['capture_status'],'CAPTURED_WITH_DATA_GAPS')
            self.assertEqual(b.run_exit_code(q,'strict'),2);self.assertEqual(b.run_exit_code(q,'observed-source'),0)
            self.assertEqual(len(json.loads((root/'checkpoint.json').read_text())['completed']),1)
            c.verify(out)

    @unittest.skipUnless(importlib.util.find_spec('pyarrow'),'optional parquet dependency; full CI installs intraday requirements')
    def test_dated_identity_clock_is_bound_to_checkpoint_and_unchanged_clock_resumes(self):
        class HTTP:
            used_calls=used_bytes=0
            def __init__(self,*a):pass
        called=[]
        def fetch(task,dest,http):
            called.append(dict(task));(dest/'raw.fixture.json').write_text('{}')
            start=1735689600
            rows=[c.bar(start+3600*i,[10,12,9,11,1],task,self.observed) for i in range(24)]
            return rows,{'status':'COMPLETE','expected':24,'saved':24,'missing':[],'rejected':[]}
        cfg={'schema_version':1,'crypto':['ACHUSDT'],'crypto_asset_ids':{'ACHUSDT':'alchemy-pay'},
             'observed_at':self.observed,'crypto_identity_observed_at':{'ACHUSDT':'2026-10-30T13:30:00+00:00'}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(c,'HTTP',HTTP),patch.object(c,'fetch_task',side_effect=fetch):
            root=Path(tmp);b.collect(cfg,root,'crypto1h',date='2025-01-01')
            cfg['crypto_identity_observed_at']['ACHUSDT']='2026-10-30T13:31:00+00:00'
            b.collect(cfg,root,'crypto1h',date='2025-01-01');_out,q=b.collect(cfg,root,'crypto1h',date='2025-01-01')
            self.assertEqual(len(called),2);self.assertNotEqual(called[0]['asset_identity_available_at'],called[1]['asset_identity_available_at'])
            self.assertEqual(q['already_complete'],1);self.assertEqual(q['bars'],0)
            self.assertEqual(len(json.loads((root/'checkpoint.json').read_text())['completed']),2)


if __name__=='__main__':unittest.main()
