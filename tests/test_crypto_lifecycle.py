import datetime as dt
import unittest

import crypto_lifecycle as c


def task(**changes):
    result = dict(kind='crypto', market='spot', symbol='EPICUSDT',
                  date='2026-10-28', interval='1h')
    result.update(changes)
    return result


class Tests(unittest.TestCase):
    def setUp(self):
        self.cutoff = int(c.EPIC_SPOT_RETIRED_AT.timestamp())
        self.raw_open = (self.cutoff - 3600) * 1_000_000
        self.raw_close = self.cutoff * 1_000_000 - 1000

    def test_retirement_day_has_three_expected_hourly_starts(self):
        policy = c.lifecycle_policy(task())
        self.assertEqual(policy['expected_slots'],
                         [self.cutoff - x * 3600 for x in (3, 2, 1)])
        self.assertEqual(policy['expected'], 3)
        self.assertEqual(len(policy['retired_not_expected_slots']), 21)
        self.assertTrue(policy['should_fetch'])
        self.assertEqual(policy['status'], 'LIFECYCLE_LIMITED_EXPECTED')
        self.assertNotIn(self.cutoff, policy['expected_slots'])

    def test_fully_retired_date_requires_no_request_and_is_not_full_day_complete(self):
        for date in ('2026-10-29', '2026-11-01'):
            policy = c.lifecycle_policy(task(date=date))
            self.assertEqual(policy['status'], 'RETIRED_NOT_EXPECTED')
            self.assertEqual(policy['expected_slots'], [])
            self.assertEqual(policy['expected'], 0)
            self.assertEqual(len(policy['retired_not_expected_slots']), 24)
            self.assertFalse(policy['should_fetch'])

    def test_before_retirement_and_other_symbol_or_market_keep_full_grid(self):
        for changes in ({'date': '2026-10-27'}, {'symbol': 'BTCUSDT'},
                        {'market': 'futures'}, {'market': 'um'}):
            policy = c.lifecycle_policy(task(**changes))
            self.assertEqual(policy['expected'], 24)
            self.assertEqual(policy['retired_not_expected_slots'], [])
            self.assertTrue(policy['should_fetch'])
            self.assertEqual(policy['status'], 'FULL_DAY_EXPECTED')

    def test_fifteen_minute_expectations_follow_cutoff_but_have_no_precision_exception(self):
        policy = c.lifecycle_policy(task(interval='15m'))
        self.assertEqual(policy['expected'], 12)
        self.assertEqual(len(policy['retired_not_expected_slots']), 84)
        raw_open = (self.cutoff - 900) * 1_000_000
        self.assertFalse(c.close_clock_policy(task(interval='15m'), raw_open,
                                             self.raw_close, 1_000_000)['allowed'])

    def test_exact_terminal_label_allowed_and_original_integers_preserved(self):
        result = c.close_clock_policy(task(), self.raw_open, self.raw_close, 1_000_000)
        self.assertTrue(result['allowed'])
        self.assertTrue(result['close_clock_exception'])
        self.assertEqual(result['raw_open_timestamp'], self.raw_open)
        self.assertEqual(result['raw_close_timestamp'], self.raw_close)
        self.assertEqual(result['raw_timestamp_divisor'], 1_000_000)
        self.assertEqual(result['precision_qualification'], c.TERMINAL_PRECISION_QUALIFICATION)
        self.assertNotEqual(result['raw_close_timestamp'], self.cutoff * 1_000_000 - 1)

    def test_exception_is_exact_symbol_market_date_interval_and_bucket(self):
        for changes in ({'symbol': 'BTCUSDT'}, {'market': 'futures'},
                        {'date': '2026-10-29'}, {'interval': '15m'}):
            with self.subTest(changes=changes):
                self.assertFalse(c.close_clock_policy(task(**changes), self.raw_open,
                                                     self.raw_close, 1_000_000)['allowed'])
        earlier_open = self.raw_open - 3600 * 1_000_000
        earlier_close = self.raw_close - 3600 * 1_000_000
        self.assertFalse(c.close_clock_policy(task(), earlier_open, earlier_close,
                                             1_000_000)['allowed'])

    def test_nearby_and_materially_short_close_labels_are_rejected_without_rounding(self):
        for close in (self.raw_close - 1, self.raw_close + 1,
                      self.cutoff * 1_000_000 - 10_000,
                      self.cutoff * 1_000_000,
                      self.cutoff * 1_000_000 - 3600 * 1_000_000):
            with self.subTest(close=close):
                result = c.close_clock_policy(task(), self.raw_open, close, 1_000_000)
                self.assertFalse(result['allowed'])
                self.assertEqual(result['raw_close_timestamp'], close)

    def test_normal_exact_ms_and_us_close_labels_stay_strictly_supported(self):
        for symbol in ('EPICUSDT', 'BTCUSDT'):
            for divisor in (1000, 1_000_000):
                raw_open = (self.cutoff - 3600) * divisor
                result = c.close_clock_policy(task(symbol=symbol), raw_open,
                                             raw_open + 3600 * divisor - 1, divisor)
                self.assertTrue(result['allowed'])
                self.assertFalse(result['close_clock_exception'])

    def test_invalid_types_and_off_grid_open_cannot_receive_exception(self):
        for raw_open, raw_close, divisor in (
                (True, self.raw_close, 1_000_000),
                (self.raw_open, str(self.raw_close), 1_000_000),
                (self.raw_open, self.raw_close, True),
                (self.raw_open, self.raw_close, 1),
                (self.raw_open + 1, self.raw_close, 1_000_000)):
            with self.subTest(values=(raw_open, raw_close, divisor)):
                self.assertFalse(c.close_clock_policy(task(), raw_open, raw_close, divisor)['allowed'])

    def test_expected_grid_does_not_hide_other_missing_or_invalid_rows(self):
        expected = set(c.lifecycle_policy(task(symbol='BTCUSDT'))['expected_slots'])
        observed = expected - {min(expected)}
        self.assertEqual(len(expected - observed), 1)
        self.assertFalse(c.close_clock_policy(task(symbol='BTCUSDT'), self.raw_open,
                                             self.raw_close, 1_000_000)['allowed'])
        epic_expected = set(c.lifecycle_policy(task())['expected_slots'])
        self.assertEqual(len(epic_expected - {min(epic_expected)}), 2)

    def test_bad_task_clock_does_not_silently_broaden_scope(self):
        for changes in ({'date': '20261028'}, {'interval': '1m'}, {'kind': 'stocks'}):
            with self.assertRaises(ValueError):
                c.lifecycle_policy(task(**changes))


if __name__ == '__main__':
    unittest.main()
