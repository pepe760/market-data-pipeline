import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

import pipeline as p


class YFRateLimitError(Exception):
    pass


RSS = b'''<rss><channel><item><title>Headline</title>
<link>https://example.org/story</link><source>Publisher</source><pubDate>Today</pubDate>
<description>FULL_ARTICLE_MUST_NOT_BE_SAVED</description></item></channel></rss>'''
GOOD = dict(Open=10, High=11, Low=9, Close=10, **{'Adj Close':10, 'Volume':5})


class Yahoo:
    def __init__(self, calendar_limit=False, price_limit=False):
        self.calendar_limit = calendar_limit
        self.price_limit = price_limit
        self.calendars, self.news, self.searches = [], [], []

    def Ticker(self, ticker):
        outer = self
        class Ticker:
            @property
            def calendar(self):
                outer.calendars.append(ticker)
                if outer.calendar_limit:
                    raise urllib.error.HTTPError('https://example.org', 429, 'limited', {}, None)
                return {'Earnings Date':['2026-12-01']}

            def get_news(self, **kwargs):
                outer.news.append(ticker)
                raise YFRateLimitError('limited')

            def history(self, **kwargs):
                if outer.price_limit:
                    raise YFRateLimitError('limited')
                class Frame:
                    def iterrows(self):
                        yield dt.datetime(2026,11,27,tzinfo=dt.timezone.utc), GOOD
                return Frame()
        return Ticker()

    def Search(self, *args, **kwargs):
        self.searches.append(args)
        raise AssertionError('Yahoo search must stop after news rate limit')


class NewsCircuitTests(unittest.TestCase):
    def collect(self, yf, rss=None):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict('sys.modules', {'yfinance':yf}), \
                patch.object(p, 'closed_sessions', return_value=['2026-11-27']), \
                patch.object(p.time, 'sleep'), \
                patch.object(p.urllib.request, 'urlopen', side_effect=rss or (lambda *a, **k:io.BytesIO(RSS))):
            dest = Path(temp)
            status = p.collect({'prices':['PRICE'], 'events':['AAPL','MSFT','GOOGL'],
                                'as_of':'2026-11-28'}, dest)
            quality = json.loads((dest/'quality.json').read_text())
            events = list(map(json.loads, (dest/'events.jsonl').read_text().splitlines()))
            return status, quality, events

    def test_news_429_falls_back_once_and_remaining_news_skips_yahoo(self):
        yf = Yahoo()
        status, quality, events = self.collect(yf)
        self.assertEqual(status, 'COMPLETE')
        self.assertEqual(yf.news, ['AAPL'])
        self.assertEqual(yf.searches, [])
        self.assertEqual(yf.calendars, ['AAPL','MSFT','GOOGL'])
        news = [e for e in events if e['kind'] == 'news']
        self.assertEqual(len(news), 3)
        self.assertTrue(all(e['source'] == 'GoogleNews/RSS' for e in news))
        self.assertTrue(all(set(e['payload'][0]) == {'title','url','publisher','published_at'} for e in news))
        self.assertNotIn('FULL_ARTICLE_MUST_NOT_BE_SAVED', json.dumps(events))
        self.assertTrue(all(c['status'] == 'OK' for c in quality['checks']))

    def test_calendar_429_does_not_skip_rss_news_or_hide_missing_calendar(self):
        yf = Yahoo(calendar_limit=True)
        status, quality, events = self.collect(yf)
        self.assertEqual(status, 'PARTIAL')
        self.assertEqual(p.run_result(status)[0], 2)
        self.assertEqual(yf.calendars, ['AAPL'])
        self.assertEqual([c['status'] for c in quality['checks'] if c['kind'] == 'calendar'],
                         ['RATE_LIMIT','NOT_ATTEMPTED_RATE_LIMIT','NOT_ATTEMPTED_RATE_LIMIT'])
        self.assertEqual([e['ticker'] for e in events if e['kind'] == 'news'], ['AAPL','MSFT','GOOGL'])
        self.assertTrue(all(e['source'] == 'GoogleNews/RSS' for e in events))

    def test_price_429_has_its_own_circuit_and_events_are_still_attempted(self):
        yf = Yahoo(price_limit=True)
        status, quality, events = self.collect(yf)
        self.assertEqual(status, 'PARTIAL')
        self.assertEqual(quality['checks'][0]['status'], 'RATE_LIMIT')
        self.assertEqual(yf.calendars, ['AAPL','MSFT','GOOGL'])
        self.assertEqual(len(events), 6)

    def test_rss_429_stops_only_news_and_keeps_calendars(self):
        yf = Yahoo()
        rss = Mock(side_effect=urllib.error.HTTPError('https://example.org', 429, 'limited', {}, None))
        status, quality, events = self.collect(yf, rss)
        self.assertEqual(status, 'PARTIAL')
        self.assertEqual(rss.call_count, 1)
        self.assertEqual(yf.news, ['AAPL'])
        self.assertEqual(yf.calendars, ['AAPL','MSFT','GOOGL'])
        self.assertEqual([c['status'] for c in quality['checks'] if c['kind'] == 'news'],
                         ['RATE_LIMIT','NOT_ATTEMPTED_RATE_LIMIT','NOT_ATTEMPTED_RATE_LIMIT'])
        self.assertEqual(len(events), 3)


if __name__ == '__main__':
    unittest.main()
