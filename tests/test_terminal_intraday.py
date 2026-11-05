from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import public_terminal as terminal
import terminal_intraday as intra


UTC = timezone.utc


def sample_bars(end, count):
    return [{"start": intra.stamp(end - (count-index)*900),
             "end": intra.stamp(end - (count-index-1)*900),
             "open": 100+index, "high": 101+index, "low": 99+index,
             "close": 100+index, "volume": 10} for index in range(count)]


def yahoo_packet(symbol, starts, end):
    count=len(starts)
    return {"chart":{"result":[{
        "meta":{"symbol":symbol,"tradingPeriods":[[{"start":starts[0],"end":end}]]},
        "timestamp":starts,"indicators":{"quote":[{
            "open":[100]*count,"high":[101]*count,"low":[99]*count,"close":[100]*count,"volume":[10]*count,
        }]},
    }]}}


class IntradayTests(unittest.TestCase):
    def test_yahoo_drops_unaligned_partial_and_post_close_points(self):
        start=1793889000
        received=datetime.fromtimestamp(start+1900,UTC)
        raw=yahoo_packet("SPY",[start,start+900,start+1800,start+1900,start+2700],start+1800)
        bars,rejected=intra.normalize_yahoo(raw,"SPY",received)
        self.assertEqual([row["end"] for row in bars],[intra.stamp(start+900),intra.stamp(start+1800)])
        self.assertEqual(rejected["unaligned"],1)
        self.assertEqual(rejected["incomplete_or_future"],2)
        with self.assertRaisesRegex(intra.IntradayError,"SYMBOL"):
            intra.normalize_yahoo(raw,"QQQ",received)
        raw["chart"]["result"][0]["meta"]["tradingPeriods"]=[]
        with self.assertRaisesRegex(intra.IntradayError,"SESSION_METADATA"):
            intra.normalize_yahoo(raw,"SPY",received)

    def test_binance_requires_exact_closed_interval(self):
        start=1793889000
        def row(offset):
            a=(start+offset*900)*1000
            return [a,"100","101","99","100","5",a+899999]
        raw=[row(0),row(1),row(2)]
        bad=row(-1);bad[6]-=1;raw.append(bad)
        bars,rejected=intra.normalize_binance(raw,datetime.fromtimestamp(start+1900,UTC))
        self.assertEqual(len(bars),2)
        self.assertEqual(rejected["incomplete_or_future"],1)
        self.assertEqual(rejected["invalid"],1)

    def test_intraday_windows_require_contiguous_complete_endpoints(self):
        end=1793912400
        bars=sample_bars(end,20)
        ret,rs,windows,rs_windows=intra.calculate(bars,bars)
        self.assertAlmostEqual(ret["h1"],119/115-1)
        self.assertAlmostEqual(ret["h4"],119/103-1)
        self.assertEqual(rs,{"h1":0,"h4":0})
        self.assertEqual(windows,rs_windows)
        missing=bars[:-3]+bars[-2:]
        ret,rs,windows,_=intra.calculate(missing,bars)
        self.assertIsNone(ret["h1"])
        self.assertIsNone(ret["h4"])
        self.assertIsNone(rs["h1"])
        self.assertIsNone(windows["h4"])

    def test_relative_uses_asset_end_even_when_benchmark_is_newer(self):
        end=1793912400
        bars=sample_bars(end,20)
        newer=sample_bars(end+900,21)
        ret,rs,windows,paired=intra.calculate(bars,newer)
        self.assertEqual(windows["h1"][-1],intra.stamp(end))
        self.assertIsNotNone(rs["h1"])
        self.assertEqual(windows["h1"],paired["h1"])
        # A benchmark shifted off the15-minute grid cannot be interpolated.
        shifted=sample_bars(end+60,20)
        self.assertIsNone(intra.calculate(bars,shifted)[1]["h1"])

    def test_overnight_gap_has_no_synthetic_four_hour_return(self):
        end=1793912400
        prior=sample_bars(end-86400,20)
        current=sample_bars(end,4)
        ret,_,_,_=intra.calculate(prior+current,prior+current)
        self.assertIsNone(ret["h1"])
        self.assertIsNone(ret["h4"])

    def test_budget_prioritizes_benchmarks_and_rotates_other_core_names(self):
        config=json.loads((intra.ROOT/"terminal_intraday_core.json").read_text())
        core=[symbol for group in config["groups"].values() for symbol in group]
        instruments=[{"symbol":symbol,"market":"US"} for symbol in core]
        rows={symbol:{} for symbol in core}
        now=datetime.fromtimestamp(1793912400,UTC)
        calls=[]
        def fetch(url):
            symbol=unquote(urlsplit(url).path.rsplit("/",1)[-1])
            calls.append(symbol)
            return yahoo_packet(symbol,[1793909700,1793910600,1793911500],1793912400)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            result=intra.enrich(rows,{},instruments,root,budget=24,workers=1,force=False,clock=lambda:now,
                fetch=fetch,write_json=terminal.write_json,active_market=lambda *_:True,limited=threading.Event())
            self.assertEqual(result["attempted"],24)
            self.assertEqual(len(calls),24)
            self.assertEqual(calls[:3],config["benchmarks"])
            old=json.loads(json.dumps(rows))
            for metadata in old.values():
                if "intraday" in metadata and metadata["intraday"].get("last_attempt"):
                    metadata["intraday"]["last_attempt"]="2026-11-05T21:00:00+00:00"
            calls.clear()
            intra.enrich(rows,old,instruments,root,budget=24,workers=1,force=False,clock=lambda:now,
                fetch=fetch,write_json=terminal.write_json,active_market=lambda *_:True,limited=threading.Event())
            self.assertEqual(calls[:3],config["benchmarks"])
            self.assertTrue(any(symbol not in old or not old[symbol].get("intraday",{}).get("asof") for symbol in calls[3:]))

    def test_source_failure_preserves_good_bar_clock_and_archive(self):
        now=datetime.fromtimestamp(1793912400,UTC)
        bars=sample_bars(now.timestamp(),20)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            metadata=intra.default_metadata("SPY")
            metadata.update(asof=bars[-1]["end"],received_at="2026-11-05T21:01:00+00:00",bar_count=20)
            path=intra.file_path(root,"SPY")
            terminal.write_json(path,{"symbol":"SPY","asof":metadata["asof"],"bars":bars})
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            rows={"SPY":{}}
            result=intra.enrich(rows,{"SPY":{"intraday":metadata}},[{"symbol":"SPY","market":"ETF"}],
                root,budget=1,workers=1,force=False,clock=lambda:now,
                fetch=lambda _:(_ for _ in ()).throw(terminal.SourceError("HTTP_503")),
                write_json=terminal.write_json,active_market=lambda *_:True,limited=threading.Event())
            self.assertEqual(rows["SPY"]["intraday"]["asof"],metadata["asof"])
            self.assertEqual(rows["SPY"]["intraday"]["received_at"],metadata["received_at"])
            self.assertEqual(rows["SPY"]["intraday"]["quality"],"SOURCE_ERROR_PREVIOUS_RETAINED")
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)
            self.assertEqual(result["failed"],1)

    def test_daily_three_and_ten_day_returns_cost_no_new_source(self):
        returns,_,_=terminal.analytics([{"close":100+i} for i in range(30)],252)
        self.assertAlmostEqual(returns["d3"],129/126-1)
        self.assertAlmostEqual(returns["d10"],129/119-1)

    def test_total_ordinary_history_budget_includes_intraday_benchmarks(self):
        now=datetime.fromtimestamp(1793912400,UTC)
        config=json.loads((intra.ROOT/"terminal_intraday_core.json").read_text())
        core=[symbol for group in config["groups"].values() for symbol in group]
        symbols=core+[f"TEST{i:03}" for i in range(200)]
        instruments=[{"symbol":s,"name":s,"market":"US","sector":"Test","currency":"USD"} for s in symbols]
        universe={"asof":now.isoformat(),"instruments":instruments,"directory_us_available":6000,"directory_errors":[]}
        daily_calls,intraday_calls=[],[]
        def daily(instrument,_):
            daily_calls.append(instrument["symbol"])
            return dict(terminal.empty_row(instrument),price=100,price_asof=now.isoformat(),
                        received_at=now.isoformat(),analysis_price=100,analytics_asof="2026-11-05",
                        series=[{"date":"2026-11-05","close":100,"volume":10}],quality="OBSERVED")
        def fetch(url):
            if "/ticker/" in url:
                return []
            symbol=unquote(urlsplit(url).path.rsplit("/",1)[-1])
            intraday_calls.append(symbol)
            return yahoo_packet(symbol,[1793909700,1793910600,1793911500],1793912400)
        with tempfile.TemporaryDirectory() as tmp,patch.object(terminal,"assemble_universe",return_value=universe), \
             patch.object(terminal,"yahoo_row",side_effect=daily),patch.object(terminal,"fetch",side_effect=fetch), \
             patch.object(terminal,"utc_now",return_value=now):
            result=terminal.collect({},Path(tmp),max_requests=180,intraday_budget=24,workers=1,now=now)
        self.assertEqual(len(daily_calls),156)
        self.assertEqual(len(intraday_calls),24)
        self.assertEqual(result["coverage"]["attempted"],180)
        self.assertEqual(intraday_calls[:3],config["benchmarks"])


if __name__=="__main__":
    unittest.main()
