import gzip
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import broad_collect as b
import intraday15m as c
import pipeline as p


def instrument(name, currency='BTC'):
    return dict(instrument_name=name, kind='option', is_active=True,
                expiration_timestamp=4000000000000, base_currency=currency,
                quote_currency='USD', settlement_currency=currency,
                strike=50000, option_type='put', contract_size=1)


def summary(name):
    return dict(instrument_name=name, creation_timestamp=1704067200000,
                bid_price=0.01, ask_price=0.02)


def api(metadata, summaries):
    class Fake:
        def __init__(self, calls, byte_limit):
            self.calls=calls;self.used_calls=0;self.urls=[];self.metadata_reads=0
        def get(self, url):
            if self.used_calls>=self.calls:raise c.BudgetStop('budget')
            self.used_calls+=1;self.urls.append(url)
            if 'get_instruments?' in url:
                value=metadata[self.metadata_reads];self.metadata_reads+=1
            else:
                currency=parse_qs(urlparse(url).query)['currency'][0]
                value={'result':summaries[currency]}
            if isinstance(value,Exception):raise value
            return value if isinstance(value,bytes) else json.dumps(value).encode()
    return Fake


class Tests(unittest.TestCase):
    def setUp(self):
        self.known=[instrument('BTC-%d-P'%i) for i in range(300)]
        self.book=[summary(row['instrument_name']) for row in self.known]

    def collect(self, metadata, summaries):
        with tempfile.TemporaryDirectory() as tmp,patch.object(c,'HTTP',api(metadata,summaries)):
            out,q=b.collect_options(Path(tmp));c.verify(out)
            with zipfile.ZipFile(out/'raw_bundle.zip') as raw:
                payloads={name:raw.read(name) for name in raw.namelist()}
                self.assertEqual(len(raw.namelist()),len(payloads))
            hashes=json.loads((out/'raw_response_manifest.json').read_text())
            self.assertEqual(hashes,{name:hashlib.sha256(blob).hexdigest() for name,blob in payloads.items()})
            with gzip.open(out/'options.jsonl.gz','rt') as stream:
                rows=[json.loads(line) for line in stream]
            return q,rows,payloads

    def test_listing_between_reads_resolves_after_one_refresh(self):
        new=instrument('BTC-NEW-P')
        q,rows,raw=self.collect([{'result':self.known},{'result':self.known+[new]}],
                                {'BTC':self.book+[summary('BTC-NEW-P')]})
        self.assertEqual(q['status'],'COMPLETE');self.assertEqual(q['contracts'],301)
        self.assertTrue(q['metadata_refreshed']);self.assertEqual(q['initial_unknown_metadata_count'],1)
        self.assertEqual(q['unknown_metadata'],[]);self.assertEqual(q['requests'],3)
        self.assertEqual(set(raw),{'instruments.json','instruments_refresh.json','BTC_summary.json'})
        self.assertNotEqual(raw['instruments.json'],raw['instruments_refresh.json'])
        self.assertTrue(all(row['greeks'] is None and not row['executable_quote'] for row in rows))

    def test_unresolved_listing_remains_partial_without_another_refresh(self):
        q,rows,_raw=self.collect([{'result':self.known}]*2,
                                 {'BTC':self.book+[summary('BTC-UNKNOWN-P')]})
        self.assertEqual(q['status'],'PARTIAL');self.assertEqual(q['contracts'],300)
        self.assertEqual(q['unknown_metadata'],['BTC-UNKNOWN-P']);self.assertEqual(q['requests'],3)
        self.assertNotIn('BTC-UNKNOWN-P',{row['instrument'] for row in rows})

    def test_refresh_fetches_new_currency_once_without_rereading_old_book(self):
        new=instrument('BTC-NEW-P');other=instrument('ETH-NEW-P','ETH')
        q,rows,raw=self.collect([{'result':self.known},{'result':self.known+[new,other]}],
                                {'BTC':self.book+[summary('BTC-NEW-P')],
                                 'ETH':[summary('ETH-NEW-P')]})
        self.assertEqual(q['status'],'COMPLETE');self.assertEqual(q['contracts'],302)
        self.assertEqual(q['requests'],4);self.assertEqual(q['settlement_currencies'],['BTC','ETH'])
        self.assertEqual(q['summary_counts'],{'BTC':301,'ETH':1})
        self.assertIn('ETH_summary.json',raw);self.assertIn('ETH-NEW-P',{row['instrument'] for row in rows})

    def test_ten_currencies_and_one_refresh_fit_request_budget(self):
        metadata=[instrument('C%d-%d-P'%(i,j),'C%d'%i) for i in range(10) for j in range(30)]
        books={'C%d'%i:[summary('C%d-%d-P'%(i,j)) for j in range(30)] for i in range(10)}
        books['C0'].append(summary('C0-NEW-P'))
        q,_rows,_raw=self.collect([{'result':metadata},{'result':metadata+[instrument('C0-NEW-P','C0')]}],books)
        self.assertEqual(q['status'],'COMPLETE');self.assertEqual(q['requests'],12)

    def test_malformed_refresh_is_preserved_and_cloud_fails_closed(self):
        malformed=b'{"result":"not a list"}'
        saved=[]
        def preserve(root,prefix,mode):
            self.assertEqual((prefix,mode),('crypto_options','publish'))
            batch=next((root/'batches').iterdir());c.verify(batch)
            quality=json.loads((batch/'quality.json').read_text())
            with zipfile.ZipFile(batch/'raw_bundle.zip') as raw:
                saved.append((quality,{name:raw.read(name) for name in raw.namelist()}))
        with patch.object(c,'HTTP',api([{'result':self.known},malformed],
                                      {'BTC':self.book+[summary('BTC-NEW-P')]})), \
             patch.object(p,'remote',return_value='private:'),patch.object(p,'rclone'), \
             patch('subprocess.check_output',return_value=b''),patch.object(b,'transfer',side_effect=preserve):
            with self.assertRaisesRegex(c.SourceError,'COLLECTION_FAILED_PRIVATE_EVIDENCE_PRESERVED'):
                b.cloud('crypto_options')
        self.assertEqual(len(saved),1);quality,raw=saved[0]
        self.assertEqual(quality['status'],'PARTIAL');self.assertEqual(quality['error_class'],'SourceError')
        self.assertEqual(raw['instruments_refresh.json'],malformed)
        self.assertIn('instruments.json',raw);self.assertIn('BTC_summary.json',raw)

    def test_fresh_metadata_source_error_does_not_fall_back_to_cache(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(c,'HTTP',api([c.SourceError('NETWORK')],{})):
            root=Path(tmp);cache=root/'instrument_cache.json';cache.write_text(json.dumps({'result':self.known}))
            before=cache.read_bytes()
            with self.assertRaisesRegex(c.SourceError,'NETWORK'):b.collect_options(root)
            self.assertEqual(cache.read_bytes(),before)
            self.assertFalse(list((root/'batches').glob('*/options.jsonl.gz')))

    def test_malformed_active_metadata_rejected_even_without_matching_summary(self):
        broken=instrument('BTC-BROKEN-P');del broken['expiration_timestamp']
        for metadata in (self.known+[broken],self.known+[self.known[0]]):
            with self.subTest(metadata_rows=len(metadata)),tempfile.TemporaryDirectory() as tmp, \
                 patch.object(c,'HTTP',api([{'result':metadata}],{'BTC':self.book})):
                root=Path(tmp)
                with self.assertRaisesRegex(c.SourceError,'INSTRUMENT_SCHEMA'):b.collect_options(root)
                self.assertFalse((root/'instrument_cache.json').exists())
                batch=next((root/'batches').iterdir())
                with zipfile.ZipFile(batch/'raw_bundle.zip') as raw:
                    self.assertEqual(json.loads(raw.read('instruments.json')),{'result':metadata})


if __name__=='__main__':unittest.main()
