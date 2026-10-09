import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import options_collector as o
import pipeline as p


NOW = dt.datetime(2026, 10, 3, 9, tzinfo=dt.timezone.utc)
BASE = dict(contractSymbol='TEST261009C00100000', strike=100, bid=1, ask=2,
            lastPrice=1.5, lastTradeDate=NOW.isoformat(), impliedVolatility=0.3,
            volume=10, openInterest=20, currency='USD', contractSize='REGULAR')
SPOT = dict(symbol='TEST', regularMarketPrice=100, regularMarketTime=int(NOW.timestamp()))


class Frame:
    def __init__(self, rows):
        self.rows = rows
    def iterrows(self):
        return enumerate(self.rows)


class FakeTicker:
    options = ['2026-10-09']
    def option_chain(self, expiry):
        return SimpleNamespace(calls=Frame([BASE]), puts=Frame([dict(BASE, contractSymbol='TEST261009P00100000')]), underlying=SPOT)


class OptionTests(unittest.TestCase):
    def test_expiry_selection_dedup_and_past(self):
        self.assertEqual(o.select_expiries(['2026-10-02','2026-10-09','2026-10-16'],
            NOW.date(), [7, 8, 14]), ['2026-10-09','2026-10-16'])

    def test_zero_bid_not_last_price_fallback(self):
        row = o.normalize('TEST','2026-10-09','call',dict(BASE,bid=0),SPOT,NOW,NOW)
        self.assertIsNone(row['midpoint'])
        self.assertIn('ZERO_BID', row['flags'])
        self.assertFalse(row['executable_quote'])

    def test_crossed_nan(self):
        for change, flag in [(dict(bid=3), 'INVALID_OR_CROSSED_MARKET'),
                              (dict(ask=float('nan')), 'MISSING_BID_ASK')]:
            row=o.normalize('TEST','2026-10-09','call',dict(BASE,**change),SPOT,NOW,NOW)
            self.assertIn(flag,row['flags'])
            self.assertIsNone(row['midpoint'])
            json.dumps(row,allow_nan=False)

    def test_underlying_not_same_clock(self):
        row=o.normalize('TEST','2026-10-09','call',BASE,SPOT,NOW,NOW)
        self.assertEqual(row['underlying']['price'],100)
        self.assertIsNone(row['option_quote_at'])
        self.assertIsNone(row['multiplier'])
        self.assertEqual(row['pit_status'],'NOT_CERTIFIED')

    def test_missing_stale_underlying(self):
        row=o.normalize('TEST','2026-10-09','call',BASE,{},NOW,NOW)
        self.assertIn('UNDERLYING_PRICE_MISSING',row['flags'])
        spot=dict(SPOT,regularMarketTime=int((NOW-dt.timedelta(days=4)).timestamp()))
        self.assertIn('UNDERLYING_TIME_OUTSIDE_36H',o.normalize('TEST','2026-10-09','call',BASE,spot,NOW,NOW)['flags'])

    def test_collect_seal_import_schema2(self):
        with tempfile.TemporaryDirectory() as root:
            dest=Path(root)/'20261003T090000Z-0123456789ab';dest.mkdir()
            status=o.collect({'tickers':['TEST'],'target_dtes':[7,14]},dest,
                             ticker_factory=lambda t:FakeTicker(),clock=lambda:NOW,pause=lambda t:None)
            self.assertEqual(status,'COMPLETE')
            with patch('pipeline.importlib.metadata.version',return_value='test'):
                p.seal(dest)
            self.assertEqual(p.verify(dest)['schema'],2)
            db=Path(root)/'test.sqlite'
            self.assertTrue(p.import_batch(dest,db))
            self.assertFalse(p.import_batch(dest,db))
            with sqlite3.connect(db) as c:
                self.assertEqual(c.execute("SELECT count(*) FROM observations WHERE kind='options'").fetchone()[0],2)
            (dest/'options.jsonl').write_text('tampered')
            with self.assertRaises(ValueError):p.verify(dest)

    def test_empty_chain_partial(self):
        with tempfile.TemporaryDirectory() as root:
            status=o.collect({'tickers':['TEST'],'target_dtes':[7]},Path(root),
                ticker_factory=lambda t:SimpleNamespace(options=[]),clock=lambda:NOW,pause=lambda t:None)
            self.assertEqual(status,'PARTIAL')

    def test_opposite_side_retries_are_merged(self):
        state={'n':0}
        class Flip:
            options=['2026-10-21']
            def option_chain(self, expiry):
                state['n']+=1
                if state['n']==1:
                    return SimpleNamespace(calls=Frame([dict(BASE, bid=None, ask=None)]),
                                           puts=Frame([]), underlying=SPOT)
                return SimpleNamespace(calls=Frame([]),
                                       puts=Frame([dict(BASE, contractSymbol='TEST261021P00100000', bid=None, ask=None)]),
                                       underlying=SPOT)
        with tempfile.TemporaryDirectory() as root:
            status=o.collect({'tickers':['TEST'],'target_dtes':[14]}, Path(root),
                             ticker_factory=lambda t: Flip(), clock=lambda: NOW, pause=lambda t: None)
            sides=sorted({json.loads(line)['side'] for line in (Path(root)/'options.jsonl').read_text().splitlines()})
        self.assertEqual(status,'COMPLETE')
        self.assertEqual(sides, ['call','put'])

    def test_confirmed_one_sided_quote_is_vendor_coverage(self):
        calls=[]
        class OneSided:
            options=['2026-10-21']
            def option_chain(self, expiry):
                calls.append(expiry)
                return SimpleNamespace(calls=Frame([BASE]), puts=Frame([]), underlying=SPOT)
        with tempfile.TemporaryDirectory() as root:
            status=o.collect({'tickers':['TEST'],'target_dtes':[14]}, Path(root),
                             ticker_factory=lambda t: OneSided(), clock=lambda: NOW, pause=lambda t: None)
            quality=json.loads((Path(root)/'quality.json').read_text())
        self.assertEqual(status,'COMPLETE')
        self.assertEqual(calls, ['2026-10-21','2026-10-21'])
        self.assertEqual(quality['checks'][0]['vendor_empty_sides'], ['put'])
        self.assertEqual(quality['checks'][0]['rows'], {'call':1,'put':0})

    def test_quote_stripped_expiry_replaced_when_absent_on_confirm(self):
        class Listed:
            def __init__(self, options):
                self.options=options
            def option_chain(self, expiry):
                if expiry=='2026-10-21':
                    return SimpleNamespace(calls=Frame([dict(BASE, bid=None, ask=None)]),
                                           puts=Frame([]), underlying=SPOT)
                return SimpleNamespace(calls=Frame([BASE]),
                                       puts=Frame([dict(BASE, contractSymbol='TEST261023P00100000')]),
                                       underlying=SPOT)
        made=[]
        def factory(ticker):
            made.append(ticker)
            return Listed(['2026-10-21','2026-10-23'] if len(made)==1 else ['2026-10-23'])
        with tempfile.TemporaryDirectory() as root:
            status=o.collect({'tickers':['TEST'],'target_dtes':[14]}, Path(root),
                             ticker_factory=factory, clock=lambda: NOW, pause=lambda t: None)
            rows=[json.loads(line) for line in (Path(root)/'options.jsonl').read_text().splitlines()]
            quality=json.loads((Path(root)/'quality.json').read_text())
        self.assertEqual(status,'COMPLETE')
        self.assertEqual(sorted({row['expiry'] for row in rows}), ['2026-10-23'])
        self.assertEqual(quality['checks'][0]['expiry'], '2026-10-23')
        self.assertEqual(quality['checks'][0]['discarded_expiries'][0]['expiry'], '2026-10-21')
        self.assertEqual(quality['checks'][0]['discarded_expiries'][0]['reason'], 'NOT_LISTED_ON_CONFIRM')

    def test_quote_stripped_still_listed_uses_next_expiry(self):
        class Listed:
            options=['2026-10-21','2026-10-23']
            def option_chain(self, expiry):
                if expiry=='2026-10-21':
                    return SimpleNamespace(calls=Frame([dict(BASE, bid=None, ask=None)]),
                                           puts=Frame([]), underlying=SPOT)
                return SimpleNamespace(calls=Frame([BASE]),
                                       puts=Frame([dict(BASE, contractSymbol='TEST261023P00100000')]),
                                       underlying=SPOT)
        with tempfile.TemporaryDirectory() as root:
            status=o.collect({'tickers':['TEST'],'target_dtes':[14]}, Path(root),
                             ticker_factory=lambda t: Listed(), clock=lambda: NOW, pause=lambda t: None)
            quality=json.loads((Path(root)/'quality.json').read_text())
            expiries=sorted({json.loads(line)['expiry'] for line in (Path(root)/'options.jsonl').read_text().splitlines()})
        self.assertEqual(status,'COMPLETE')
        self.assertEqual(expiries, ['2026-10-23'])
        self.assertEqual(quality['checks'][0]['discarded_expiries'][0]['reason'], 'QUOTE_STRIPPED_ONE_SIDED')

    def test_quote_stripped_without_replacement_stays_partial(self):
        class OnlyBad:
            options=['2026-10-21']
            def option_chain(self, expiry):
                return SimpleNamespace(calls=Frame([dict(BASE, bid=None, ask=None)]),
                                       puts=Frame([]), underlying=SPOT)
        with tempfile.TemporaryDirectory() as root:
            status=o.collect({'tickers':['TEST'],'target_dtes':[14]}, Path(root),
                             ticker_factory=lambda t: OnlyBad(), clock=lambda: NOW, pause=lambda t: None)
            quality=json.loads((Path(root)/'quality.json').read_text())
        self.assertEqual(status,'PARTIAL')
        self.assertEqual(quality['checks'][0]['reason'], 'QUOTE_STRIPPED_ONE_SIDED')
        self.assertEqual(quality['checks'][0]['attempts'], 2)

    def test_rejected_contract_stays_partial(self):
        class Rejected:
            options=['2026-10-09']
            def option_chain(self, expiry):
                return SimpleNamespace(calls=Frame([BASE, dict(BASE, contractSymbol='')]),
                                       puts=Frame([dict(BASE, contractSymbol='TEST261009P00100000')]),
                                       underlying=SPOT)
        with tempfile.TemporaryDirectory() as root:
            status=o.collect({'tickers':['TEST'],'target_dtes':[7]}, Path(root),
                             ticker_factory=lambda t: Rejected(), clock=lambda: NOW, pause=lambda t: None)
            quality=json.loads((Path(root)/'quality.json').read_text())
        self.assertEqual(status,'PARTIAL')
        self.assertEqual(quality['checks'][0]['reason'], 'REJECTED_CONTRACTS')
        self.assertGreater(quality['checks'][0]['rejected_rows'], 0)

    def test_rate_limit_no_further_requests(self):
        class RateLimitError(Exception):pass
        calls=[]
        def fail(t):
            calls.append(t);raise RateLimitError()
        with tempfile.TemporaryDirectory() as root:
            o.collect({'tickers':['TEST','OTHER'],'target_dtes':[7]},Path(root),
                      ticker_factory=fail,clock=lambda:NOW,pause=lambda t:None)
            self.assertEqual(calls,['TEST'])
            q=json.loads((Path(root)/'quality.json').read_text())
            self.assertEqual(q['checks'][1]['status'],'NOT_ATTEMPTED_RATE_LIMIT')


if __name__ == '__main__':unittest.main()
