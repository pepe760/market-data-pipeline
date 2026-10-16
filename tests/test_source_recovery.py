import datetime as dt
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
import pipeline as p

GOOD=dict(Open=10,High=11,Low=9,Close=10,**{'Adj Close':10,'Volume':5})
BAD=dict(GOOD,Close=float('nan'))
UNFINAL={**GOOD, 'Close':float('nan'), 'Adj Close':float('nan')}

class Frame:
    def __init__(self, rows):
        self.rows=rows
    def iterrows(self):
        for day, values in self.rows:
            yield dt.datetime.fromisoformat(day+'T00:00:00+00:00'), values

def collect_history(frames, tickers, sessions, archived=None):
    yf=Mock()
    yf.Ticker.return_value.history.side_effect=frames
    with tempfile.TemporaryDirectory() as temp, patch.dict('sys.modules', {'yfinance': yf}), \
         patch.object(p, 'closed_sessions', return_value=sessions), \
         patch.object(p.time, 'sleep'):
        status=p.collect({'prices':tickers,'events':[],'as_of':'2026-10-08'}, Path(temp), archived)
        quality=json.loads((Path(temp)/'quality.json').read_text())
        rows=[json.loads(line) for line in (Path(temp)/'prices.jsonl').read_text().splitlines() if line]
    return status, quality, rows, yf

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

    def test_nonfinite_session_is_refetched_once(self):
        frames=[
            Frame([('2026-10-07', GOOD), ('2026-10-08', BAD)]),
            Frame([('2026-10-07', GOOD), ('2026-10-08', GOOD)])]
        status, quality, rows, yf=collect_history(
            frames, ['HUBB'], ['2026-10-07', '2026-10-08'])
        self.assertEqual(status, 'COMPLETE')
        self.assertEqual(yf.Ticker.return_value.history.call_count, 2)
        self.assertEqual([row['session'] for row in rows], ['2026-10-07', '2026-10-08'])
        self.assertEqual(quality['checks'][0]['refetched_missing_sessions'], ['2026-10-08'])
        self.assertEqual(quality['checks'][0]['missing_sessions'], [])
        self.assertNotIn('vendor_defective_sessions', quality['checks'][0])
        self.assertEqual(quality['vendor_price_defects'], [])

    def test_persistent_nonfinite_bar_is_isolated_without_inventing_a_price(self):
        # Otherwise-good run: retired name, healthy name, one ticker non-finite on both reads.
        frames=[
            Frame([('2026-10-07', GOOD), ('2026-10-08', GOOD)]),
            Frame([('2026-10-07', GOOD), ('2026-10-08', BAD)]),
            Frame([('2026-10-07', GOOD), ('2026-10-08', BAD)])]
        status, quality, rows, yf=collect_history(
            frames, ['AVB', 'AAPL', 'HUBB'], ['2026-10-07', '2026-10-08'])
        self.assertEqual(status, 'COMPLETE_WITH_VENDOR_DEFECTS')
        self.assertEqual(yf.Ticker.return_value.history.call_count, 3)
        self.assertEqual([(row['ticker'], row['session']) for row in rows],
                         [('AAPL', '2026-10-07'), ('AAPL', '2026-10-08'), ('HUBB', '2026-10-07')])
        self.assertTrue(all(math.isfinite(value) for row in rows for value in row['values'].values()))
        hubb=[row for row in rows if row['ticker']=='HUBB']
        self.assertEqual([row['values']['Close'] for row in hubb], [10])
        checks={item['ticker']: item for item in quality['checks']}
        self.assertEqual(checks['AVB']['status'], 'NOT_EXPECTED_RETIRED')
        self.assertEqual(checks['AAPL']['status'], 'OK')
        self.assertEqual(checks['HUBB']['status'], 'VENDOR_DEFECT')
        self.assertEqual(checks['HUBB']['missing_sessions'], ['2026-10-08'])
        self.assertEqual(checks['HUBB']['vendor_defective_sessions'], ['2026-10-08'])
        self.assertEqual(checks['HUBB']['refetched_missing_sessions'], ['2026-10-08'])
        self.assertEqual(checks['HUBB']['rejected_details'][0]['reason'], 'Non-finite price')
        self.assertEqual(quality['vendor_price_defects'], [dict(
            ticker='HUBB', sessions=['2026-10-08'], reason='Non-finite price', check_status='VENDOR_DEFECT')])
        code, message=p.run_result(status)
        self.assertEqual(code, 0)
        self.assertIn('isolated vendor price defects', message)

    def test_absent_session_after_refetch_stays_partial(self):
        frames=[Frame([('2026-10-07', GOOD)]), Frame([('2026-10-07', GOOD)])]
        status, quality, rows, yf=collect_history(
            frames, ['HUBB'], ['2026-10-07', '2026-10-08'])
        self.assertEqual(status, 'PARTIAL')
        self.assertEqual(quality['checks'][0]['status'], 'PARTIAL')
        self.assertEqual(quality['checks'][0]['missing_sessions'], ['2026-10-08'])
        self.assertNotIn('vendor_defective_sessions', quality['checks'][0])
        self.assertEqual(quality['vendor_price_defects'], [])
        self.assertEqual([row['session'] for row in rows], ['2026-10-07'])
        self.assertEqual(p.run_result(status)[0], 2)

    def test_nonpositive_bar_after_refetch_stays_partial(self):
        nonpositive=dict(GOOD, Close=0)
        frames=[
            Frame([('2026-10-07', GOOD), ('2026-10-08', nonpositive)]),
            Frame([('2026-10-07', GOOD), ('2026-10-08', nonpositive)])]
        status, quality, _rows, _yf=collect_history(
            frames, ['HUBB'], ['2026-10-07', '2026-10-08'])
        self.assertEqual(status, 'PARTIAL')
        self.assertEqual(quality['checks'][0]['rejected_details'][0]['reason'], 'Non-positive price')
        self.assertNotIn('vendor_defective_sessions', quality['checks'][0])

    def test_nonfinite_then_absent_is_not_confirmed(self):
        frames=[
            Frame([('2026-10-07', GOOD), ('2026-10-08', BAD)]),
            Frame([('2026-10-07', GOOD)])]
        status, quality, _rows, _yf=collect_history(
            frames, ['HUBB'], ['2026-10-07', '2026-10-08'])
        self.assertEqual(status, 'PARTIAL')
        self.assertEqual(quality['checks'][0]['status'], 'PARTIAL')
        self.assertNotIn('vendor_defective_sessions', quality['checks'][0])

    def test_nonfinite_only_session_is_not_isolated(self):
        frames=[Frame([('2026-10-08', BAD)]), Frame([('2026-10-08', BAD)])]
        status, quality, rows, _yf=collect_history(frames, ['HUBB'], ['2026-10-08'])
        self.assertEqual(status, 'PARTIAL')
        self.assertEqual(quality['checks'][0]['status'], 'PARTIAL')
        self.assertEqual(quality['checks'][0]['vendor_defective_sessions'], ['2026-10-08'])
        self.assertEqual(rows, [])
        self.assertEqual(quality['vendor_price_defects'][0]['check_status'], 'PARTIAL')

    def test_two_nonfinite_sessions_on_one_ticker_stay_partial(self):
        frames=[
            Frame([('2026-10-06', GOOD), ('2026-10-07', BAD), ('2026-10-08', BAD)]),
            Frame([('2026-10-06', GOOD), ('2026-10-07', BAD), ('2026-10-08', BAD)])]
        status, quality, rows, _yf=collect_history(
            frames, ['HUBB'], ['2026-10-06', '2026-10-07', '2026-10-08'])
        self.assertEqual(status, 'PARTIAL')
        self.assertEqual(quality['checks'][0]['status'], 'PARTIAL')
        self.assertEqual(quality['checks'][0]['vendor_defective_sessions'], ['2026-10-07', '2026-10-08'])
        self.assertEqual([row['session'] for row in rows], ['2026-10-06'])

    def test_vendor_defect_cap_boundary(self):
        sessions=['2026-10-07', '2026-10-08']
        def frames_for(count):
            out=[]
            for _ in range(count):
                out.append(Frame([('2026-10-07', GOOD), ('2026-10-08', BAD)]))
                out.append(Frame([('2026-10-07', GOOD), ('2026-10-08', BAD)]))
            return out
        tickers=['T%d' % i for i in range(p.MAX_ISOLATED_VENDOR_DEFECT_TICKERS)]
        status, quality, rows, _yf=collect_history(frames_for(len(tickers)), tickers, sessions)
        self.assertEqual(status, 'COMPLETE_WITH_VENDOR_DEFECTS')
        self.assertEqual(len(quality['vendor_price_defects']), len(tickers))
        self.assertEqual(len(rows), len(tickers))
        over=tickers+['T%d' % len(tickers)]
        status, quality, _rows, _yf=collect_history(frames_for(len(over)), over, sessions)
        self.assertEqual(status, 'PARTIAL')
        self.assertEqual(sum(item['status']=='VENDOR_DEFECT' for item in quality['checks']), len(over))
        self.assertEqual(p.run_result(status)[0], 2)

    def test_true_gap_keeps_batch_partial_beside_a_vendor_defect(self):
        frames=[
            Frame([('2026-10-07', GOOD), ('2026-10-08', BAD)]),
            Frame([('2026-10-07', GOOD), ('2026-10-08', BAD)]),
            Frame([('2026-10-07', GOOD)]),
            Frame([('2026-10-07', GOOD)])]
        status, quality, _rows, yf=collect_history(
            frames, ['HUBB', 'AAPL'], ['2026-10-07', '2026-10-08'])
        # HUBB is refetched. AAPL's missing day is absent on both reads, so it is not a vendor defect.
        self.assertEqual(yf.Ticker.return_value.history.call_count, 4)
        self.assertEqual(status, 'PARTIAL')
        checks={item['ticker']: item for item in quality['checks']}
        self.assertEqual(checks['HUBB']['status'], 'VENDOR_DEFECT')
        self.assertEqual(checks['AAPL']['status'], 'PARTIAL')
        self.assertEqual(quality['vendor_price_defects'][0]['ticker'], 'HUBB')

    def test_batch_status_does_not_hide_other_failures(self):
        ok=dict(status='OK')
        retired=dict(status='NOT_EXPECTED_RETIRED')
        defect=dict(status='VENDOR_DEFECT')
        self.assertEqual(p.batch_status([ok, retired]), 'COMPLETE')
        self.assertEqual(p.batch_status([ok, retired, defect]), 'COMPLETE_WITH_VENDOR_DEFECTS')
        self.assertEqual(p.batch_status([ok, defect, dict(status='EMPTY_UNVERIFIED')]), 'PARTIAL')
        self.assertEqual(p.batch_status([ok, dict(status='ERROR')]), 'PARTIAL')
        self.assertEqual(p.batch_status([ok, dict(status='RATE_LIMIT')]), 'PARTIAL')
        self.assertEqual(p.batch_status([defect]*(p.MAX_ISOLATED_VENDOR_DEFECT_TICKERS+1)), 'PARTIAL')
        waiting=dict(status='CLOSE_UNFINALIZED')
        overdue=dict(status='CLOSE_OVERDUE')
        self.assertEqual(p.batch_status([ok, retired, waiting]), 'AWAITING_OFFICIAL_CLOSE')
        self.assertEqual(p.batch_status([ok, overdue]), 'CLOSE_OVERDUE')
        self.assertEqual(p.batch_status([waiting, defect]), 'PARTIAL')
        self.assertEqual(p.batch_status([waiting, overdue]), 'PARTIAL')
        self.assertEqual(p.batch_status([ok, waiting, dict(status='PARTIAL')]), 'PARTIAL')

    def test_run_result_leaves_option_failures_nonzero(self):
        self.assertEqual(p.run_result('COMPLETE', options=True), (0, 'Private pipeline finished.'))
        self.assertEqual(p.run_result('PARTIAL', options=True)[0], 2)
        self.assertEqual(p.run_result('COMPLETE_WITH_VENDOR_DEFECTS', options=True)[0], 2)
        self.assertEqual(p.run_result('AWAITING_OFFICIAL_CLOSE', options=True)[0], 2)
        self.assertEqual(p.run_result('CLOSE_OVERDUE', options=True)[0], 2)

    def test_official_close_deadline_boundary(self):
        close=dt.datetime(2026,10,15,20,0,tzinfo=dt.timezone.utc)
        self.assertTrue(p.official_close_pending(close, close+dt.timedelta(hours=5)))
        self.assertTrue(p.official_close_pending(close, close+p.OFFICIAL_CLOSE_DEADLINE))
        self.assertFalse(p.official_close_pending(close, close+p.OFFICIAL_CLOSE_DEADLINE+dt.timedelta(seconds=1)))
        self.assertEqual(p.OFFICIAL_CLOSE_DEADLINE, dt.timedelta(hours=16))
        self.assertFalse(p.latest_official_close_pending([], close))
        # Regular close 20:00 UTC; early close (day after Thanksgiving 2026) 18:00 UTC.
        self.assertTrue(p.latest_official_close_pending(['2026-10-15'], close+dt.timedelta(hours=12)))
        self.assertFalse(p.latest_official_close_pending(
            ['2026-10-15'], close+dt.timedelta(hours=16, seconds=1)))
        early=dt.datetime(2026,11,27,18,0,tzinfo=dt.timezone.utc)
        self.assertTrue(p.latest_official_close_pending(['2026-11-27'], early+dt.timedelta(hours=16)))
        self.assertFalse(p.latest_official_close_pending(
            ['2026-11-27'], early+dt.timedelta(hours=16, seconds=1)))

    def test_unpublished_official_close_is_not_a_vendor_defect_or_a_price(self):
        frames=[
            Frame([('2026-10-07', GOOD), ('2026-10-08', UNFINAL)]),
            Frame([('2026-10-07', GOOD), ('2026-10-08', UNFINAL)])]
        with patch.object(p, 'latest_official_close_pending', return_value=True):
            status, quality, rows, yf=collect_history(
                frames, ['ABBV'], ['2026-10-07', '2026-10-08'])
        self.assertEqual(status, 'AWAITING_OFFICIAL_CLOSE')
        self.assertEqual(yf.Ticker.return_value.history.call_count, 2)
        self.assertEqual([row['session'] for row in rows], ['2026-10-07'])
        self.assertTrue(all(math.isfinite(value) for row in rows for value in row['values'].values()))
        check=quality['checks'][0]
        self.assertEqual(check['status'], 'CLOSE_UNFINALIZED')
        self.assertEqual(check['missing_sessions'], ['2026-10-08'])
        self.assertEqual(check['unfinalized_close_sessions'], ['2026-10-08'])
        self.assertEqual(check['rejected_details'][0]['reason'], 'Unfinalized close')
        self.assertNotIn('vendor_defective_sessions', check)
        self.assertEqual(quality['vendor_price_defects'], [])
        self.assertEqual(quality['pending_official_closes'], [dict(
            ticker='ABBV', sessions=['2026-10-08'], reason='Unfinalized close',
            check_status='CLOSE_UNFINALIZED')])
        self.assertEqual(quality['overdue_official_closes'], [])
        self.assertEqual(quality['official_close_deadline_hours'], 16)
        code, message=p.run_result(status, pending_closes=1)
        self.assertEqual(code, 0)
        self.assertIn('1 price ticker', message)
        self.assertIn('not published yet', message)

    def test_many_unpublished_closes_do_not_use_the_vendor_defect_cap(self):
        sessions=['2026-10-07', '2026-10-08']
        tickers=['T%d' % i for i in range(p.MAX_ISOLATED_VENDOR_DEFECT_TICKERS+1)]
        frames=[]
        for _ in tickers:
            frames.append(Frame([('2026-10-07', GOOD), ('2026-10-08', UNFINAL)]))
            frames.append(Frame([('2026-10-07', GOOD), ('2026-10-08', UNFINAL)]))
        with patch.object(p, 'latest_official_close_pending', return_value=True):
            status, quality, rows, _yf=collect_history(frames, tickers, sessions)
        self.assertEqual(status, 'AWAITING_OFFICIAL_CLOSE')
        self.assertEqual(len(quality['pending_official_closes']), len(tickers))
        self.assertEqual(quality['vendor_price_defects'], [])
        self.assertEqual(len(rows), len(tickers))
        self.assertTrue(all(row['session']=='2026-10-07' for row in rows))
        code, message=p.run_result(status, pending_closes=len(tickers))
        self.assertEqual(code, 0)
        self.assertIn('%d price tickers' % len(tickers), message)

    def test_unpublished_close_past_the_deadline_fails(self):
        frames=[
            Frame([('2026-10-07', GOOD), ('2026-10-08', UNFINAL)]),
            Frame([('2026-10-07', GOOD), ('2026-10-08', UNFINAL)])]
        with patch.object(p, 'latest_official_close_pending', return_value=False):
            status, quality, rows, _yf=collect_history(
                frames, ['ABBV'], ['2026-10-07', '2026-10-08'])
        self.assertEqual(status, 'CLOSE_OVERDUE')
        self.assertEqual(quality['checks'][0]['status'], 'CLOSE_OVERDUE')
        self.assertEqual(quality['checks'][0]['unfinalized_close_sessions'], ['2026-10-08'])
        self.assertEqual(quality['pending_official_closes'], [])
        self.assertEqual(quality['overdue_official_closes'][0]['ticker'], 'ABBV')
        self.assertEqual(quality['vendor_price_defects'], [])
        self.assertEqual([row['session'] for row in rows], ['2026-10-07'])
        code, message=p.run_result(status, overdue_closes=1)
        self.assertEqual(code, 2)
        self.assertIn('deadline', message)

    def test_unpublished_close_on_an_older_session_is_overdue(self):
        frames=[
            Frame([('2026-10-07', UNFINAL), ('2026-10-08', GOOD)]),
            Frame([('2026-10-07', UNFINAL), ('2026-10-08', GOOD)])]
        with patch.object(p, 'latest_official_close_pending', return_value=True):
            status, quality, rows, _yf=collect_history(
                frames, ['ABBV'], ['2026-10-07', '2026-10-08'])
        self.assertEqual(status, 'CLOSE_OVERDUE')
        self.assertEqual(quality['checks'][0]['missing_sessions'], ['2026-10-07'])
        self.assertEqual([row['session'] for row in rows], ['2026-10-08'])

    def test_unpublished_close_does_not_excuse_an_older_missing_session(self):
        frames=[Frame([('2026-10-08', UNFINAL)]), Frame([('2026-10-08', UNFINAL)])]
        with patch.object(p, 'latest_official_close_pending', return_value=True):
            status, quality, rows, _yf=collect_history(
                frames, ['ABBV'], ['2026-10-07', '2026-10-08'])
        self.assertEqual(status, 'PARTIAL')
        self.assertEqual(quality['checks'][0]['status'], 'PARTIAL')
        self.assertEqual(quality['checks'][0]['missing_sessions'], ['2026-10-07', '2026-10-08'])
        self.assertEqual(quality['checks'][0]['unfinalized_close_sessions'], ['2026-10-08'])
        self.assertEqual(quality['pending_official_closes'], [])
        self.assertEqual(rows, [])
        self.assertEqual(p.run_result(status)[0], 2)

    def test_true_gap_beside_an_unpublished_close_stays_partial(self):
        frames=[
            Frame([('2026-10-07', GOOD), ('2026-10-08', UNFINAL)]),
            Frame([('2026-10-07', GOOD), ('2026-10-08', UNFINAL)]),
            Frame([('2026-10-07', GOOD)]),
            Frame([('2026-10-07', GOOD)])]
        with patch.object(p, 'latest_official_close_pending', return_value=True):
            status, quality, _rows, _yf=collect_history(
                frames, ['ABBV', 'AAPL'], ['2026-10-07', '2026-10-08'])
        self.assertEqual(status, 'PARTIAL')
        checks={item['ticker']: item for item in quality['checks']}
        self.assertEqual(checks['ABBV']['status'], 'CLOSE_UNFINALIZED')
        self.assertEqual(checks['AAPL']['status'], 'PARTIAL')
        self.assertEqual(p.run_result(status)[0], 2)

    def test_vendor_defect_beside_an_unpublished_close_stays_partial(self):
        frames=[
            Frame([('2026-10-07', GOOD), ('2026-10-08', UNFINAL)]),
            Frame([('2026-10-07', GOOD), ('2026-10-08', UNFINAL)]),
            Frame([('2026-10-07', GOOD), ('2026-10-08', BAD)]),
            Frame([('2026-10-07', GOOD), ('2026-10-08', BAD)])]
        with patch.object(p, 'latest_official_close_pending', return_value=True):
            status, quality, rows, _yf=collect_history(
                frames, ['ABBV', 'HUBB'], ['2026-10-07', '2026-10-08'])
        self.assertEqual(status, 'PARTIAL')
        checks={item['ticker']: item for item in quality['checks']}
        self.assertEqual(checks['ABBV']['status'], 'CLOSE_UNFINALIZED')
        self.assertEqual(checks['HUBB']['status'], 'VENDOR_DEFECT')
        self.assertEqual(quality['vendor_price_defects'][0]['ticker'], 'HUBB')
        self.assertTrue(all(math.isfinite(value) for row in rows for value in row['values'].values()))
        self.assertNotIn(('ABBV', '2026-10-08'), {(row['ticker'], row['session']) for row in rows})

    def test_unpublished_close_that_later_prints_is_stored(self):
        frames=[
            Frame([('2026-10-07', GOOD), ('2026-10-08', UNFINAL)]),
            Frame([('2026-10-07', GOOD), ('2026-10-08', GOOD)])]
        status, quality, rows, yf=collect_history(
            frames, ['ABBV'], ['2026-10-07', '2026-10-08'])
        self.assertEqual(status, 'COMPLETE')
        self.assertEqual(yf.Ticker.return_value.history.call_count, 2)
        self.assertEqual([row['session'] for row in rows], ['2026-10-07', '2026-10-08'])
        self.assertNotIn('unfinalized_close_sessions', quality['checks'][0])
        self.assertEqual(quality['pending_official_closes'], [])

    def test_close_only_nan_stays_a_nonfinite_vendor_defect(self):
        # Adj Close still finite: this is not the unpublished-close pattern.
        with self.assertRaises(ValueError) as raised:
            p.price_row('HUBB', '2026-10-08', BAD, '2026-10-08T00:00:00Z')
        self.assertEqual(str(raised.exception), 'Non-finite price')
        with self.assertRaises(ValueError) as raised:
            p.price_row('ABBV', '2026-10-08', UNFINAL, '2026-10-08T00:00:00Z')
        self.assertEqual(str(raised.exception), 'Unfinalized close')

    def test_complete_history_is_not_refetched(self):
        class One:
            def iterrows(self):
                yield dt.datetime.fromisoformat('2026-10-08T00:00:00+00:00'), GOOD
        yf=Mock()
        yf.Ticker.return_value.history.return_value=One()
        with tempfile.TemporaryDirectory() as temp, patch.dict('sys.modules', {'yfinance': yf}), \
             patch.object(p, 'closed_sessions', return_value=['2026-10-08']), \
             patch.object(p.time, 'sleep'):
            self.assertEqual(p.collect({'prices':['AAPL'],'events':[],'as_of':'2026-10-08'}, Path(temp)), 'COMPLETE')
        self.assertEqual(yf.Ticker.return_value.history.call_count, 1)
