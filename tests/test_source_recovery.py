import datetime as dt
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
import pipeline as p

class RecoveryTests(unittest.TestCase):
    def test_news_search_fallback_keeps_only_metadata(self):
        yf=Mock(); yf.Ticker.return_value.get_news.return_value=[]
        yf.Search.return_value.news=[{'title':'Headline','link':'https://example.org/story',
            'publisher':'Publisher','providerPublishTime':123,'fullText':'must not be stored'}]
        payload,source=p.news_payload(yf,'AAPL')
        self.assertEqual(source,'Yahoo/search')
        self.assertEqual(set(payload[0]),{'title','url','publisher','published_at'})
        self.assertEqual(payload[0]['url'],'https://example.org/story')

    def test_delisted_history_recovery_preserves_observation_clock(self):
        yf=Mock(); yf.Ticker.return_value.history.side_effect=ValueError('provider unavailable')
        saved=p.price_row('WBD','2026-10-05',dict(Open=30,High=31,Low=29,Close=30,
            **{'Adj Close':30,'Volume':100}),'2026-10-05T22:00:00Z')
        saved['recovery_batch']='20261006T000000Z-0123456789ab'
        with tempfile.TemporaryDirectory() as temp,patch.dict('sys.modules',{'yfinance':yf}),\
             patch.object(p,'closed_sessions',return_value=['2026-10-05','2026-10-06','2026-10-07']),\
             patch.object(p.time,'sleep'):
            self.assertEqual(p.collect({'prices':['WBD'],'events':[],'as_of':'2026-10-08'},Path(temp),
                {('WBD','2026-10-05'):saved}),'COMPLETE')
            row=json.loads((Path(temp)/'prices.jsonl').read_text())
            self.assertEqual(row['available_at'],saved['available_at'])
            self.assertEqual(row['session'],'2026-10-05')
            self.assertEqual(row['recovery'],'VERIFIED_PRIOR_BATCH')

    def test_skyd_mapping_and_no_post_delisting_expectation(self):
        self.assertEqual(p.yahoo_symbol('PSKY','2026-09-17'),'SKYD')
        self.assertEqual(p.RETIRED_AFTER['WBD'],'2026-10-05')
