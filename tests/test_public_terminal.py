from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import public_terminal as terminal


class TerminalTests(unittest.TestCase):
    def test_returns_sma_and_vol_are_calculated_from_daily_closes(self):
        series=[{"date":str(i),"close":float(i+1)} for i in range(250)]
        returns,sma,vol=terminal.analytics(series,252)
        self.assertAlmostEqual(returns["d20"],250/230-1)
        self.assertAlmostEqual(sma["d200"],150.5)
        self.assertGreater(vol,0)

    def test_short_history_leaves_long_window_metrics_unknown(self):
        returns,sma,vol=terminal.analytics([{"close":10},{"close":11}],252)
        self.assertAlmostEqual(returns["d1"],.1)
        self.assertIsNone(returns["d20"])
        self.assertIsNone(sma["d200"])
        self.assertIsNone(vol)

    def test_stock_chart_alias_and_missing_prices_remain_gaps(self):
        now=datetime(2026,11,4,15,tzinfo=timezone.utc)
        instrument={"symbol":"BRK.B","name":"Berkshire","market":"US","currency":"USD","sector":"Financials"}
        payload={"chart":{"result":[{"meta":{"symbol":"BRK-B","currency":"USD","regularMarketTime":now.timestamp(),
                   "regularMarketPrice":11,"exchangeTimezoneName":"America/New_York"},
                   "timestamp":[now.timestamp()-86400,now.timestamp()],
                   "indicators":{"quote":[{"close":[10,None],"volume":[1,None]}],
                                 "adjclose":[{"adjclose":[9,None]}]}}]}}
        with patch.object(terminal,"fetch",return_value=payload) as call,patch.object(terminal,"utc_now",return_value=now):
            result=terminal.yahoo_row(instrument,now)
        self.assertIn("BRK-B",call.call_args.args[0])
        self.assertEqual(result["series"],[{"date":"2026-11-03","close":9,"volume":1}])
        self.assertEqual(result["price"],11)
        self.assertEqual(result["received_at"],now.isoformat())
        payload["chart"]["result"][0]["meta"]["symbol"]="WRONG"
        with patch.object(terminal,"fetch",return_value=payload),patch.object(terminal,"utc_now",return_value=now):
            with self.assertRaisesRegex(terminal.SourceError,"CHART_SYMBOL_MISMATCH"):
                terminal.yahoo_row(instrument,now)

    def test_source_failure_retains_previous_price_clock_and_series(self):
        now=datetime(2026,11,4,15,tzinfo=timezone.utc)
        instrument={"symbol":"TEST","name":"Test","market":"US","currency":"USD","sector":"Other"}
        old=dict(terminal.empty_row(instrument),price=10,price_asof="2026-11-03T21:00:00+00:00",
                 received_at="2026-11-03T21:01:00+00:00",last_attempt="2026-11-03T21:01:00+00:00",
                 series=[{"date":"2026-11-03","close":10,"volume":100}])
        universe={"asof":now.isoformat(),"instruments":[instrument],"directory_us_available":5000,
                  "directory_errors":[]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(terminal,"fetch",return_value=[]), \
             patch.object(terminal,"assemble_universe",return_value=universe), \
             patch.object(terminal,"yahoo_row",side_effect=terminal.SourceError("HTTP_429")):
            result=terminal.collect({"markets":[old]},Path(tmp),max_requests=1,now=now)
        row=result["markets"][0]
        self.assertEqual(row["price_asof"],old["price_asof"])
        self.assertEqual(row["received_at"],old["received_at"])
        self.assertEqual(row["series"],old["series"])
        self.assertEqual(row["quality"],"SOURCE_ERROR_PREVIOUS_RETAINED")
        self.assertEqual(result["status"],"PARTIAL")

    def test_weekend_crypto_and_unobserved_stock_freshness(self):
        now=datetime(2026,11,7,15,tzinfo=timezone.utc)
        self.assertTrue(terminal.active_market("Crypto",now))
        self.assertFalse(terminal.active_market("US",now))
        self.assertFalse(terminal.freshness({"market":"US","price_asof":None},now))

    def test_crypto_directory_excludes_stale_delisted_ticker(self):
        now=datetime(2026,11,4,15,tzinfo=timezone.utc)
        rows=[{"symbol":"BTCUSDT","quoteVolume":"100","lastPrice":"10","closeTime":now.timestamp()*1000},
              {"symbol":"OLDUSDT","quoteVolume":"100","lastPrice":"10","closeTime":(now.timestamp()-86400)*1000}]
        coins=[{"id":"old-coin","symbol":"old","name":"OldCoin"},{"id":"bitcoin","symbol":"btc","name":"Bitcoin"}]
        with patch.object(terminal,"fetch",return_value=coins):
            selected=terminal.fresh_crypto_universe(1,rows,now)
        self.assertEqual([row["symbol"] for row in selected],["BTCUSDT"])
        self.assertEqual(selected[0]["identity_status"],"UNVERIFIED_SYMBOL_MATCH")

    def test_rs_uses_common_dates_and_never_unmatched_headlines(self):
        left=[{"date":str(i).zfill(3),"close":i+100} for i in range(25)]
        right=[{"date":str(i).zfill(3),"close":i+200} for i in range(30)]
        excess,window=terminal.paired_excess(left,right)
        self.assertEqual(window["end"],"024")
        self.assertAlmostEqual(excess,124/104-224/204)
        self.assertEqual(terminal.paired_excess(left[:10],right),(None,None))

    def test_future_daily_bar_and_missing_adjusted_bar_are_skipped(self):
        now=datetime(2026,11,4,15,tzinfo=timezone.utc)
        instrument={"symbol":"TEST","market":"US","currency":"USD"}
        payload={"chart":{"result":[{"meta":{"symbol":"TEST","regularMarketTime":now.timestamp(),
            "regularMarketPrice":11},"timestamp":[now.timestamp()-172800,now.timestamp()-86400,now.timestamp()+86400],
            "indicators":{"quote":[{"close":[10,11,12],"volume":[1,2,3]}],
                          "adjclose":[{"adjclose":[9,None,11]}]}}]}}
        with patch.object(terminal,"fetch",return_value=payload),patch.object(terminal,"utc_now",return_value=now):
            row=terminal.yahoo_row(instrument,now)
        self.assertEqual(row["series"],[{"date":"2026-11-02","close":9,"volume":1}])

    def test_short_new_history_overwrites_old_external_history(self):
        now=datetime(2026,11,4,15,tzinfo=timezone.utc)
        instrument={"symbol":"TEST","name":"Test","market":"US","currency":"USD","sector":"Other"}
        fresh=dict(terminal.empty_row(instrument),price=11,price_asof=now.isoformat(),
                   received_at=now.isoformat(),analytics_asof="2026-11-04",
                   series=[{"date":"2026-11-04","close":11,"volume":10}])
        universe={"asof":now.isoformat(),"instruments":[instrument],"directory_us_available":5000,
                  "directory_errors":[]}
        with tempfile.TemporaryDirectory() as tmp,patch.object(terminal,"fetch",return_value=[]), \
             patch.object(terminal,"assemble_universe",return_value=universe), \
             patch.object(terminal,"yahoo_row",return_value=fresh),patch.object(terminal,"utc_now",return_value=now):
            root=Path(tmp)
            filename=terminal.hashlib.sha256(b"TEST").hexdigest()[:20]+".json"
            (root/"series").mkdir()
            terminal.write_json(root/"series"/filename,{"symbol":"TEST","analytics_asof":"2026-11-03",
                "series":[{"date":"2026-11-03","close":10,"volume":1}]*100})
            result=terminal.collect({},root,max_requests=1,now=now)
            external=json.loads((root/"series"/filename).read_text())
            self.assertEqual(external["series"],fresh["series"])
            self.assertEqual(external["analytics_asof"],"2026-11-04")
            self.assertIsNotNone(result["markets"][0]["series_path"])


if __name__=="__main__":
    unittest.main()
