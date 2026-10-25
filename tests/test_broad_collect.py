import datetime as dt,hashlib,importlib.util,io,json,tempfile,unittest,zipfile
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch
import broad_collect as b
import intraday15m as c
import expand_universe as u

def hourly(task,micro=False,omit=False):
    div=1000000 if micro else 1000
    start=int(dt.datetime.fromisoformat(task['date']).replace(tzinfo=c.UTC).timestamp())
    rows=[]
    for i in range(23 if omit else 24):
        stamp=(start+3600*i)*div
        rows.append(','.join(map(str,[stamp,10,12,9,11,50,stamp+3600*div-1,500,5,20,200,0])))
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:z.writestr(task['symbol']+'-1h-'+task['date']+'.csv','\n'.join(rows))
    return buf.getvalue()

class Tests(unittest.TestCase):
    def setUp(self):self.task={'kind':'crypto','market':'spot','symbol':'BTCUSDT','date':'2025-01-01','interval':'1h'}
    def test_hourly_not_fifteen_minute(self):
        a,q=c.parse_crypto(hourly(self.task),self.task,'2025-01-03T00:00:00+00:00')
        self.assertEqual(len(a),24);self.assertEqual(q['expected'],24);self.assertEqual(a[0]['interval_seconds'],3600)
        self.assertEqual(a[0]['bar_end'],'2025-01-01T01:00:00+00:00')
    def test_hourly_ms_and_us_identical(self):
        a,_=c.parse_crypto(hourly(self.task),self.task,'2025-01-03T00:00:00+00:00')
        x,_=c.parse_crypto(hourly(self.task,True),self.task,'2025-01-03T00:00:00+00:00');self.assertEqual(a,x)
    def test_hourly_missing_not_filled(self):
        a,q=c.parse_crypto(hourly(self.task,omit=True),self.task,'2025-01-03T00:00:00+00:00')
        self.assertEqual(len(a),23);self.assertEqual(len(q['missing']),1)
    def test_hourly_config_and_task_id(self):
        cfg={'schema_version':1,'crypto':['BTCUSDT'],'crypto_interval':'1h','crypto_days':1}
        c.validate_config(cfg);ts=c.tasks(cfg,dt.datetime(2025,1,3,tzinfo=c.UTC))
        self.assertEqual(ts[0]['interval'],'1h')
        with self.assertRaises(ValueError):c.validate_config(dict(cfg,crypto_interval='1s'))
    def test_config_secret_unknown_duplicate_rejected(self):
        for cfg in [{'schema_version':1,'token':'secret'},{'schema_version':1,'crypto':['BTCUSDT','BTCUSDT']},{'schema_version':1,'crypto':['../file']}]:
            with self.assertRaises(ValueError):b.validate(cfg)
    def test_archive_pagination_marker(self):
        payload=b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>true</IsTruncated><NextMarker>x</NextMarker><CommonPrefixes><Prefix>data/spot/daily/klines/BTCUSDT/</Prefix></CommonPrefixes></ListBucketResult>'
        prefixes,truncated,marker=u.archive_prefixes(payload);self.assertTrue(truncated);self.assertEqual(marker,'x');self.assertEqual(len(prefixes),1)
    def test_eligible_pair_filters(self):
        self.assertTrue(u.eligible_crypto('BTCUSDT'));self.assertFalse(u.eligible_crypto('USDCUSDT'));self.assertFalse(u.eligible_crypto('BTCUPUSDT'))
    def test_option_metadata_identity_and_currency(self):
        meta=[{'instrument_name':'BTC-X-P','kind':'option','is_active':True,'expiration_timestamp':2000000000000,'base_currency':'BTC','quote_currency':'BTC','counter_currency':'USD','instrument_type':'reversed','settlement_currency':'BTC','strike':50000,'option_type':'put','contract_size':1}]
        summary=[{'instrument_name':'BTC-X-P','creation_timestamp':1704067200000,'bid_price':0.01,'ask_price':0.02,'underlying_price':60000}]
        rows,unknown=b.option_rows(meta,summary*2,'2024-01-01T00:00:02+00:00')
        self.assertEqual(len(rows),1);self.assertFalse(unknown);self.assertEqual(rows[0]['premium_currency'],'BTC');self.assertEqual(rows[0]['strike_currency'],'USD');self.assertIsNone(rows[0]['greeks']);self.assertFalse(rows[0]['executable_quote'])
        summary[0]['ask_price']=0.001;rows,_=b.option_rows(meta,summary,'2024-01-01T00:00:02+00:00');self.assertIn('INVALID_BBO',rows[0]['flags'])
    def test_unknown_metadata_not_invented(self):
        rows,unknown=b.option_rows([],[{'instrument_name':'UNKNOWN'}],'2024-01-01T00:00:00+00:00');self.assertEqual(rows,[]);self.assertEqual(unknown,['UNKNOWN'])
    def test_option_raw_roundtrip_and_cache_not_rejuvenated(self):
        import os,time
        metadata={'result':[{'instrument_name':'BTC-X-P','kind':'option','is_active':True,'expiration_timestamp':2000000000000,'base_currency':'BTC','quote_currency':'USD','settlement_currency':'BTC','strike':50000,'option_type':'put','contract_size':1}]}
        summary={'result':[{'instrument_name':'BTC-X-P','creation_timestamp':int(time.time()*1000),'bid_price':0.01,'ask_price':0.02}]}
        class Fake:
            used_calls=0
            def __init__(self,*a):pass
            def get(self,url):
                self.used_calls+=1
                return json.dumps(metadata if 'get_instruments?' in url else summary).encode()
        with tempfile.TemporaryDirectory() as tmp,patch.object(c,'HTTP',Fake):
            root=Path(tmp);out,q=b.collect_options(root);self.assertEqual(q['requests'],2)
            with zipfile.ZipFile(out/'raw_bundle.zip') as z:
                hashes=json.loads((out/'raw_response_manifest.json').read_text())
                self.assertTrue(all(hashlib.sha256(z.read(n)).hexdigest()==h for n,h in hashes.items()))
            cache=root/'instrument_cache.json';t=time.time()-1000;os.utime(cache,(t,t));before=cache.stat().st_mtime
            out,q=b.collect_options(root);self.assertEqual(q['requests'],1);self.assertEqual(cache.stat().st_mtime,before)
    @unittest.skipUnless(importlib.util.find_spec('pandas'),'optional dataframe dependency')
    def test_stale_sharadar_not_alias_spliced(self):
        import pandas as pd
        d=pd.DataFrame([{'ticker':ticker,'isdelisted':flag,'category':'Domestic Common Stock','exchange':'NYSE','currency':'USD'} for ticker,flag in [('A','N'),('B.B','N'),('DEAD','Y')]])
        result=u.compare(d,{'A','B-B','NEW'},['A','ETF'])
        self.assertEqual(result['stocks'],['A']);self.assertEqual(result['snapshot_active_not_current'],['B.B']);self.assertEqual(result['new_matched'],0)
    @unittest.skipUnless(importlib.util.find_spec('pyarrow'),'optional parquet dependency')
    def test_collect_300_resume_and_hash(self):
        class Fake:
            used_calls=used_bytes=0
            def __init__(self,*a):pass
            def get(self,url):
                self.used_calls+=1;filename=urlparse(url).path.rsplit('/',1)[-1].removesuffix('.CHECKSUM')
                symbol=filename.split('-1h-')[0];date=filename.split('-1h-')[1].removesuffix('.zip')
                task={'symbol':symbol,'date':date};blob=hourly(task)
                return (hashlib.sha256(blob).hexdigest()+'  '+filename).encode() if url.endswith('CHECKSUM') else blob
        cfg={'schema_version':1,'crypto_candidates':['C%dUSDT'%i for i in range(300)]}
        with tempfile.TemporaryDirectory() as tmp,patch.object(c,'HTTP',Fake):
            root=Path(tmp);out,q=b.collect(cfg,root,'crypto1h',date='2025-01-01',qualify=True)
            self.assertEqual(q['qualified_symbols'],300);self.assertEqual(q['bars'],7200);self.assertEqual(q['status'],'COMPLETE');c.verify(out)
            selected=json.loads((root/'selected_universe.json').read_text());out,q=b.collect(selected,root,'crypto1h',date='2025-01-01')
            self.assertEqual(q['already_complete'],300);self.assertEqual(q['requests'],0);self.assertEqual(q['status'],'COMPLETE')

if __name__=='__main__':unittest.main()
