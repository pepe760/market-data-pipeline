import copy
import datetime as dt
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import intraday15m as m


def crypto_zip(task, micro=False, omit=False, invalid=False, duplicate=False):
    start=int(dt.datetime.fromisoformat(task['date']).replace(tzinfo=m.UTC).timestamp())
    div=1000000 if micro else 1000
    rows=[]
    for i in range(95 if omit else 96):
        stamp=(start+i*900)*div
        row=[stamp,100,102,99,101,10,stamp+900*div-1,1000,4,5,500,0]
        if invalid and i==0: row[2]=98
        rows.append(','.join(map(str,row)))
    if duplicate: rows.append(rows[0])
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr(f"{task['symbol']}-15m-{task['date']}.csv",'\n'.join(rows))
    return buf.getvalue()


class Tests(unittest.TestCase):
    def setUp(self):
        self.task={'kind':'crypto','symbol':'BTCUSDT','date':'2025-01-01','market':'spot'}
        self.observed='2025-01-03T00:00:00+00:00'

    def test_config_boundaries(self):
        for c in [{'schema_version':2},{'schema_version':1,'stocks':['../bad']},
                  {'schema_version':1,'crypto':['A','A']},{'schema_version':1,'max_jobs':False},
                  {'schema_version':1,'api_key':'do-not-store'},
                  {'schema_version':1,'options_provider':'pretend_real_greeks'}]:
            with self.assertRaises(ValueError): m.validate_config(c)

    def test_crypto_millisecond_and_microsecond(self):
        a,q=m.parse_crypto(crypto_zip(self.task),self.task,self.observed)
        b,r=m.parse_crypto(crypto_zip(self.task,micro=True),self.task,self.observed)
        self.assertEqual(a,b); self.assertEqual(q['saved'],96); self.assertFalse(q['missing'])

    def test_crypto_gap_not_filled(self):
        a,q=m.parse_crypto(crypto_zip(self.task,omit=True),self.task,self.observed)
        self.assertEqual(len(a),95); self.assertEqual(len(q['missing']),1)

    def test_crypto_invalid_ohlc(self):
        a,q=m.parse_crypto(crypto_zip(self.task,invalid=True),self.task,self.observed)
        self.assertEqual(len(a),95); self.assertEqual(len(q['rejected']),1)

    def test_crypto_duplicate(self):
        a,q=m.parse_crypto(crypto_zip(self.task,duplicate=True),self.task,self.observed)
        self.assertEqual(len(a),96); self.assertEqual(len(q['rejected']),1)

    def test_unfinished_bars_rejected(self):
        a,q=m.parse_crypto(crypto_zip(self.task),self.task,'2025-01-01T00:00:00+00:00')
        self.assertEqual(a,[]); self.assertEqual(len(q['missing']),96)

    def test_unsafe_zip(self):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z: z.writestr('../outside','bad')
        with self.assertRaises(m.SourceError): m.parse_crypto(buf.getvalue(),self.task,self.observed)

    def test_listing_current_not_test_issue(self):
        b=b'Symbol|Security Name|Test Issue|ETF\nABC|A|N|N\nTEST|T|Y|N\nFile Creation Time: x|||\n'
        self.assertEqual([x['symbol'] for x in m.listing_symbols(b,'us_nasdaq')],['ABC'])
        with self.assertRaises(m.SourceError): m.listing_symbols(b'<html>blocked</html>','us_nasdaq')

    def test_stock_arrays_gaps_duplicates_identity(self):
        task={'kind':'stocks','symbol':'SPY','grid':[1735828200,1735829100]}
        data={'chart':{'result':[{'meta':{'symbol':'SPY'},'timestamp':[1735828200,1735828200],
           'indicators':{'quote':[{'open':[100,100],'high':[102,102],'low':[99,99],'close':[101,101],'volume':[10,10]}]}}]}}
        rows,q=m.parse_stocks(json.dumps(data).encode(),task,self.observed)
        self.assertEqual(len(rows),1); self.assertEqual(len(q['missing']),1); self.assertEqual(len(q['rejected']),1)
        data['chart']['result'][0]['meta']['symbol']='QQQ'
        with self.assertRaises(m.SourceError): m.parse_stocks(json.dumps(data).encode(),task,self.observed)

    @unittest.skipUnless(importlib.util.find_spec('exchange_calendars'), 'calendar dependency')
    def test_actual_nyse_calendar_early_close_and_dst(self):
        grid=m.stock_grid(dt.datetime(2025,11,29,4,tzinfo=m.UTC),1)
        self.assertEqual(len(grid),14)  # Nov28 early close: 09:30–13:00.
        a=m.stock_grid(dt.datetime(2025,3,8,4,tzinfo=m.UTC),1)
        b=m.stock_grid(dt.datetime(2025,3,11,4,tzinfo=m.UTC),1)
        self.assertEqual(dt.datetime.fromtimestamp(a[0],m.UTC).hour,14)
        self.assertEqual(dt.datetime.fromtimestamp(b[0],m.UTC).hour,13)

    def test_seal_verify_and_tamper(self):
        with tempfile.TemporaryDirectory() as t:
            d=Path(t)/'20250103T000000Z-0123456789ab';d.mkdir()
            (d/'quality.json').write_text('{}');m.seal(d);m.verify(d)
            (d/'quality.json').write_text('{"changed":true}')
            with self.assertRaises(ValueError):m.verify(d)

    def test_checksum_failure_preserves_bytes(self):
        class Fake:
            def get(self,url):return b'not matching' if url.endswith('CHECKSUM') else b'raw'
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(m.SourceError):m.fetch_task(self.task,Path(t),Fake())
            self.assertEqual((Path(t)/'raw.zip').read_bytes(),b'raw')

    @unittest.skipUnless(importlib.util.find_spec('pyarrow'), 'optional intraday parquet dependency')
    def test_resume_no_repeat_and_catalog_idempotent(self):
        class Fake:
            used_calls=used_bytes=0
        config={'schema_version':1,'stocks':[],'crypto':['BTCUSDT'],'crypto_days':1}
        asof=dt.datetime(2025,1,2,12,tzinfo=m.UTC)
        def fetch(task,folder,http):
            rows,q=m.parse_crypto(crypto_zip(task),task,self.observed)
            q['status']='COMPLETE';return rows,q
        with tempfile.TemporaryDirectory() as t,patch.object(m,'fetch_task',side_effect=fetch) as f:
            d,q=m.collect(config,Path(t),Fake(),asof)
            self.assertEqual(q['jobs_new_complete'],1);self.assertEqual(q['options']['rows'],0)
            m.collect(config,Path(t),Fake(),asof);self.assertEqual(f.call_count,1)
            self.assertTrue(m.import_batch(d,Path(t)/'catalog.sqlite'))
            self.assertFalse(m.import_batch(d,Path(t)/'catalog.sqlite'))

    @unittest.skipUnless(importlib.util.find_spec('pyarrow'), 'optional intraday parquet dependency')
    def test_budget_does_not_mark_complete(self):
        class Fake: used_calls=used_bytes=0
        config={'schema_version':1,'stocks':[],'crypto':['BTCUSDT'],'crypto_days':1}
        with tempfile.TemporaryDirectory() as t,patch.object(m,'fetch_task',side_effect=m.BudgetStop):
            d,q=m.collect(config,Path(t),Fake(),dt.datetime(2025,1,2,tzinfo=m.UTC))
            self.assertEqual(q['jobs_new_complete'],0);self.assertEqual(q['jobs_remaining'],1)
            self.assertEqual(json.loads((Path(t)/'checkpoint.json').read_text())['completed'],[])


if __name__=='__main__': unittest.main()
