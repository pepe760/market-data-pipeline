import unittest
from datetime import datetime
from unittest.mock import patch

import exchange_calendars as xc
import pandas as pd

import xly_ab_tracker as tracker


def when(iso):
    value = datetime.fromisoformat(iso)
    return lambda: value


class XlyTrackerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = xc.get_calendar("XNYS")

    def test_first_signal_clock_including_holiday_turn(self):
        november = tracker.month_window(self.calendar, pd.Period("2026-11"))
        self.assertEqual((november.key, str(november.entry.date()), str(november.exit.date())),
                         ("2026-11-30", "2026-12-01", "2027-03-01"))
        december = tracker.month_window(self.calendar, pd.Period("2026-12"))
        self.assertEqual(str(december.entry.date()), "2027-01-04")
        with patch.object(tracker, "feature_row") as feature:
            state = tracker.settle({}, self.calendar, when("2026-11-30T20:59:00+00:00"))
        feature.assert_not_called()
        self.assertEqual((state["status"], state["next_signal_date"], state["forecasts"]),
                         ("waiting_for_signal", "2026-11-30", []))

    def test_pending_retry_and_missed_signal_never_backfilled(self):
        requests = []

        def missing(now, forecast_due):
            requests.append(forecast_due)
            raise tracker.DataUnavailable("upstream delay")

        state = tracker.settle({}, self.calendar, when("2026-11-30T21:16:00+00:00"), load_inputs=missing)
        self.assertEqual(state["status"], "pending_data")
        same = tracker.settle(state, self.calendar, when("2026-11-30T21:30:00+00:00"), load_inputs=missing)
        self.assertEqual(same, state)
        missed = tracker.settle(same, self.calendar, when("2026-12-01T21:01:00+00:00"), load_inputs=missing)
        self.assertEqual((missed["status"], missed["missed_signals"], missed["forecasts"]),
                         ("missed_signal", ["2026-11-30"], []))
        self.assertEqual(requests, [True])

    def test_append_once_and_score_at_true_maturity(self):
        features = dict(rs3=-.02, rs20=.01, risk_off=0, yield_fall=.003)
        rates = []
        inputs = tracker.Inputs({}, {}, {}, {})

        def load(now, forecast_due):
            rates.append(forecast_due)
            return inputs

        with patch.object(tracker, "feature_row", return_value=(features, 1.2, {})):
            state = tracker.settle({}, self.calendar, when("2026-11-30T22:00:00+00:00"), load_inputs=load)
        self.assertEqual((state["status"], len(state["forecasts"])), ("tracking", 1))
        record = state["forecasts"][0]
        self.assertTrue(record["quarter_phase"])
        self.assertEqual(record["training_rows"], 112)
        self.assertNotEqual(record["a"], record["b"])
        score = dict(signal=record["signal"], exit=record["exit"], actual=.01,
                     squared_error_a=.003, squared_error_b=.001, squared_error_mean=.002)
        with patch.object(tracker, "score_forecast", return_value=score) as settle_score:
            state = tracker.settle(state, self.calendar, when("2027-03-01T21:16:00+00:00"), load_inputs=load)
            settle_score.assert_called_once()
            same = tracker.settle(state, self.calendar, when("2027-03-01T21:32:00+00:00"), load_inputs=load)
            settle_score.assert_called_once()
        self.assertEqual(state["forecasts"], [record])
        self.assertEqual(same, state)
        self.assertEqual(state["validation"]["quarterly"]["n"], 1)
        self.assertAlmostEqual(state["validation"]["monthly"]["improvement_vs_a"], 2/3)
        self.assertEqual(rates, [True, False])


if __name__ == "__main__":
    unittest.main()
