import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import terminal_context as context

NOW = dt.datetime(2026, 11, 5, 12, tzinfo=dt.timezone.utc)

def fixture_series(values):
    return [{
        "id": spec[0], "units": spec[3], "measurement_basis": context.MEASUREMENT_BASIS.get(spec[0], spec[4]),
        "observations": [{"date": day, "value": value} for day, value in values.get(spec[0], {}).items()],
    } for spec in context.SERIES]


class ContextTests(unittest.TestCase):
    def test_fred_csv_missing_future_and_nonfinite(self):
        raw = b"observation_date,DGS2\n2026-11-03,4.2\n2026-11-04,.\n2026-11-06,9\n"
        self.assertEqual(context.parse_fred(raw, "DGS2", NOW.date()), [{"date": "2026-11-03", "value": 4.2}])
        with self.assertRaises(ValueError):
            context.parse_fred(b"observation_date,DGS2\n2026-11-04,NaN\n", "DGS2", NOW.date())
        with self.assertRaises(ValueError):
            context.parse_fred(b"DATE,DGS2\n2026-11-04,4\n", "DGS2", NOW.date())

    def test_formula_units_and_causal_date_alignment(self):
        values = {"WALCL": {"2026-10-28": 6745635}, "WDTGAL": {"2026-10-28": 1015632},
                  "RRPONTSYD": {"2026-10-27": .4, "2026-10-28": .176, "2026-10-29": 9999},
                  "DGS10": {"2026-11-03": 4.95}, "DGS2": {"2026-11-03": 4.6, "2026-11-04": 50},
                  "CPIAUCSL": {"2025-09-01": 300, "2026-09-01": 330}, "CPILFESL": {}}
        series = fixture_series(values)
        derived = {item["id"]: item for item in context._derived(series)}
        self.assertAlmostEqual(derived["FED_THREE_ITEM_PROXY"]["value"], 5729.827)
        self.assertEqual(set(derived["FED_THREE_ITEM_PROXY"]["inputs"].values()), {"2026-10-28"})
        self.assertAlmostEqual(derived["DGS10_MINUS_DGS2"]["value"], .35)
        self.assertEqual(derived["CPIAUCSL_YOY"]["value"], 10)

    def test_previous_day_rrp_does_not_replace_missing_wednesday(self):
        data = {"WALCL": {"2026-10-28": 6000000},
                "WDTGAL": {"2026-10-28": 1000000},
                "RRPONTSYD": {"2026-10-27": .1}}
        result = context._derived(fixture_series(data))
        self.assertNotIn("FED_THREE_ITEM_PROXY", [r["id"] for r in result])

    def test_week_average_substitution_or_unit_drift_cannot_make_liquidity(self):
        data = {"WALCL": {"2026-10-28": 6000000},
                "WDTGAL": {"2026-10-28": 1000000},
                "RRPONTSYD": {"2026-10-28": .1}}
        for field, wrong in (("measurement_basis", "WEEK_AVERAGE"), ("units", "billion_usd")):
            with self.subTest(field=field):
                series = fixture_series(data)
                next(s for s in series if s["id"] == "WDTGAL")[field] = wrong
                self.assertNotIn("FED_THREE_ITEM_PROXY", [r["id"] for r in context._derived(series)])

    def test_source_selection_and_wednesday_basis(self):
        configured = {s[0]: s for s in context.SERIES}
        self.assertEqual(configured["WDTGAL"][3:5], ("million_usd", "weekly_wednesday"))
        self.assertNotIn("WTREGEN", configured)
        raw = b"observation_date,WDTGAL\n2026-10-28,1015632\n"
        result = context._series(configured["WDTGAL"], NOW, lambda _: raw)
        self.assertEqual(result["measurement_basis"], "WEDNESDAY_LEVEL")
        self.assertEqual(result["latest"]["value"], 1015632)
        wrong_day = b"observation_date,WDTGAL\n2026-10-29,1015632\n"
        result = context._series(configured["WDTGAL"], NOW, lambda _: wrong_day)
        self.assertEqual(result["status"], "SOURCE_UNAVAILABLE")
        self.assertEqual(result["observations"], [])

    def test_reserve_average_and_repo_operation_units_are_distinct(self):
        specs = {spec[0]: spec for spec in context.SERIES}
        self.assertEqual(specs["WRESBAL"][3:5], ("million_usd", "weekly_ending_wednesday"))
        self.assertEqual(context.MEASUREMENT_BASIS["WRESBAL"], "WEEK_AVERAGE_ENDING_WEDNESDAY")
        self.assertEqual(context.MEASUREMENT_BASIS["WDTGAL"], "WEDNESDAY_LEVEL")
        self.assertEqual(specs["RPONTTLD"][3:5], ("billion_usd", "daily"))
        self.assertEqual(context.MEASUREMENT_BASIS["RPONTTLD"], "DAILY_TOTAL_OVERNIGHT_REPO_OPERATION_AMOUNT")
        zero = b"observation_date,RPONTTLD\n2026-11-04,0.000\n"
        result = context._series(specs["RPONTTLD"], NOW, lambda _: zero)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["latest"]["value"], 0)

    def test_sofr_iorb_exact_date_and_basis_points(self):
        values = {"SOFR": {"2026-11-03": 4.00, "2026-11-04": 3.94},
                  "IORB": {"2026-11-03": 3.90, "2026-11-05": 3.00}}
        series = fixture_series(values)
        derived = {r["id"]: r for r in context._derived(series)}
        difference = derived["SOFR_MINUS_IORB"]
        self.assertEqual(difference["value"], 10)
        self.assertEqual(difference["units"], "basis_points")
        self.assertEqual(set(difference["inputs"].values()), {"2026-11-03"})
        series = fixture_series({"SOFR": {"2026-11-04": 3.94}, "IORB": {"2026-11-03": 3.90}})
        self.assertNotIn("SOFR_MINUS_IORB", [r["id"] for r in context._derived(series)])
        series = fixture_series(values)
        next(s for s in series if s["id"] == "IORB")["units"] = "decimal_rate"
        self.assertNotIn("SOFR_MINUS_IORB", [r["id"] for r in context._derived(series)])

    def test_unavailable_sources_remain_empty(self):
        def fail(url):
            raise context.SourceError("HTTP_403")
        with patch.dict("os.environ", {}, clear=True):
            result = context.collect_context(NOW, fetch=fail)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertTrue(all(s["latest"] is None for s in result["macro"]["series"]))
        self.assertEqual(result["crypto_derivatives"]["status"], "SOURCE_UNAVAILABLE")
        self.assertEqual(result["sec_13f"]["status"], "SEC_CONTACT_NOT_CONFIGURED")
        self.assertEqual(result["sec_13f"]["filings"], [])
        self.assertEqual(result["macro"]["derived"], [])
        json.dumps(result, allow_nan=False)

    def test_crypto_clock_and_units(self):
        epoch = int(NOW.timestamp()*1000)
        def fetch(url):
            if "premiumIndex" in url:
                return json.dumps({"symbol":"BTCUSDT","markPrice":"79000","lastFundingRate":"0.0001",
                                   "time":epoch,"nextFundingTime":epoch+3600000}).encode()
            return json.dumps({"symbol":"BTCUSDT","openInterest":"123","time":epoch}).encode()
        data = context._crypto(NOW, fetch)
        self.assertEqual(data["status"], "OK")
        self.assertEqual(data["funding"]["rate_percent"], .01)
        self.assertEqual(data["open_interest"]["quantity"], 123)
        self.assertNotIn("annualized", data["funding"])
        data = context._crypto(NOW+dt.timedelta(hours=2), fetch)
        self.assertEqual(data["status"], "SOURCE_UNAVAILABLE")
        def old(url):
            return json.dumps({"symbol":"BTCUSDT","openInterest":"123","time":epoch}).encode()
        self.assertEqual(context._crypto(NOW+dt.timedelta(hours=2), old)["status"], "SOURCE_UNAVAILABLE")

    def test_sec_metadata_requires_identity_and_keeps_amendments(self):
        def fetch(url):
            cik = str(int(url.rsplit("CIK",1)[1].split(".")[0]))
            return json.dumps({"cik":cik,"filings":{"recent":{
                "form":["13F-HR/A","13F-HR","10-Q"],"filingDate":["2026-11-04","2026-08-14","2026-11-05"],
                "accessionNumber":["0001067983-26-000001","0001067983-26-000002","0001067983-26-000003"],
                "primaryDocument":["a.xml","b.xml","c.html"],
                "reportDate":["2026-06-30","2026-06-30","2026-09-30"]}}}).encode()
        with patch.object(context.time, "sleep"):
            result = context._sec(NOW, fetch, "Terminal test@example.org")
        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(result["filings"]),6)
        self.assertEqual(result["filings"][0]["form"],"13F-HR/A")
        self.assertTrue(all(f["coverage"]=="FILING_METADATA_ONLY" for f in result["filings"]))


if __name__ == "__main__":
    unittest.main()
