"""Public economic context from official endpoints, with dated units and missing coverage."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import csv
from hashlib import sha256
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "William-Market-Data-Terminal/1.0 (https://github.com/pepe760/market-data-pipeline)"
MAX_RESPONSE = 1_000_000
MAX_OUTPUT = 250_000
SERIES = (
    ("WALCL", "聯儲局總資產", "liquidity", "million_usd", "weekly_wednesday", 14),
    ("WDTGAL", "財政部一般帳戶 TGA", "liquidity", "million_usd", "weekly_wednesday", 14),
    ("RRPONTSYD", "隔夜逆回購 ON RRP", "liquidity", "billion_usd", "daily", 7),
    ("WRESBAL", "銀行準備金餘額（週平均）", "liquidity", "million_usd", "weekly_ending_wednesday", 14),
    ("RPONTTLD", "Fed 隔夜回購操作總額", "liquidity", "billion_usd", "daily", 7),
    ("DGS2", "兩年期美債收益率", "rates", "percent", "daily", 7),
    ("DGS10", "十年期美債收益率", "rates", "percent", "daily", 7),
    ("DGS3MO", "三個月美債收益率", "rates", "percent", "daily", 7),
    ("SOFR", "隔夜有抵押融資利率 SOFR", "rates", "percent", "daily", 7),
    ("IORB", "準備金利率 IORB", "rates", "percent", "daily", 7),
    ("CPIAUCSL", "消費物價 CPI", "inflation", "index_1982_84_100_sa", "monthly", 75),
    ("CPILFESL", "核心消費物價 CPI", "inflation", "index_1982_84_100_sa", "monthly", 75),
    ("UNRATE", "美國失業率", "jobs", "percent_sa", "monthly", 75),
    ("PAYEMS", "非農業職位總數", "jobs", "thousand_persons_sa", "monthly", 75),
    ("ICSA", "首次申領失業救濟", "jobs", "persons_sa", "weekly_ending_saturday", 14),
)
MEASUREMENT_BASIS = {
    # FRED series notes and Fed H.4.1 distinguish these Wednesday levels
    # from WTREGEN, which is the weekly average ending Wednesday.
    "WALCL": "WEDNESDAY_LEVEL",
    "WDTGAL": "WEDNESDAY_LEVEL",
    "RRPONTSYD": "DAILY_OPERATION_AMOUNT",
    "WRESBAL": "WEEK_AVERAGE_ENDING_WEDNESDAY",
    "RPONTTLD": "DAILY_TOTAL_OVERNIGHT_REPO_OPERATION_AMOUNT",
    "IORB": "ADMINISTERED_RATE_EFFECTIVE_DATE",
}
LINK_ONLY = (
    ("VIXCLS", "VIX 波幅指數", "sentiment", "Cboe 指數再發布權待核實"),
    ("VXVCLS", "三個月 VIX", "sentiment", "Cboe 指數再發布權待核實"),
    ("BAMLH0A0HYM2", "高收益債信用利差", "credit", "ICE Data 指數再發布權待核實"),
    ("BAA10Y", "Baa 公司債相對美債利差", "credit", "Moody's 條款要求再發布同意"),
)
FUNDS = (
    ("0001067983", "Berkshire Hathaway"),
    ("0001336528", "Pershing Square Capital Management"),
    ("0001350694", "Bridgewater Associates"),
)
NOTES = [
    "圖表日期係經濟觀察期，擷取時間另列。FRED 現值可以修訂，呢份公開快照無歷史發布 vintage。",
    "每條 series 保留原生頻率同單位。跨頻率計算只使用明示嘅共同日期或之前已觀測值。",
    "流動性算式係指定三項資產負債表數字嘅描述性代理，並無股市因果或交易訊號驗證。",
    "13F 係延後發布嘅機構申報。申報期末持倉同今日持倉可以不同，亦唔涵蓋全部投資組合。",
    "WRESBAL 係截至星期三嘅週平均準備金，與星期三單日水平分開。RPONTTLD 係 Fed 隔夜回購操作總額。",
]


class SourceError(Exception):
    """Allowlisted source failures, without raw response bodies."""


def utc(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware UTC timestamp is required")
    return value.astimezone(timezone.utc)


def get_bytes(url, user_agent=USER_AGENT):
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json,text/csv"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            content = response.read(MAX_RESPONSE + 1)
            if len(content) > MAX_RESPONSE:
                raise SourceError("RESPONSE_TOO_LARGE")
            return content
    except urllib.error.HTTPError as exc:
        raise SourceError("HTTP_" + str(exc.code)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SourceError("SOURCE_UNAVAILABLE") from None


def number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Expected a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Expected a finite number")
    return result


def parse_fred(content, series_id, as_of):
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    if reader.fieldnames != ["observation_date", series_id]:
        raise ValueError("Unexpected FRED CSV header")
    observations = {}
    for row in reader:
        day = date.fromisoformat(row["observation_date"])
        if day > as_of or row[series_id] in {"", "."}:
            continue
        value = number(row[series_id])
        key = day.isoformat()
        if key in observations and observations[key] != value:
            raise ValueError("Conflicting economic observations")
        observations[key] = value
    return [{"date": day, "value": observations[day]} for day in sorted(observations)]


def _series(spec, clock, fetch):
    series_id, label, category, units, frequency, max_age = spec
    source_url = "https://fred.stlouisfed.org/series/" + series_id
    query = urllib.parse.urlencode({
        "id": series_id, "cosd": str(clock.date() - timedelta(days=800)), "coed": str(clock.date())
    })
    download = "https://fred.stlouisfed.org/graph/fredgraph.csv?" + query
    result = {"id": series_id, "label": label, "category": category, "source": "FRED",
              "source_url": source_url, "units": units, "frequency": frequency,
              "measurement_basis": MEASUREMENT_BASIS.get(series_id, frequency),
              "fetched_at_utc": clock.isoformat(), "publication_timestamp": None,
              "vintage": "LATEST_REVISED_NOT_PIT", "observations": [], "latest": None,
              "status": "SOURCE_UNAVAILABLE"}
    try:
        content = fetch(download)
        points = parse_fred(content, series_id, clock.date())
        if MEASUREMENT_BASIS.get(series_id) == "WEDNESDAY_LEVEL" and any(
                date.fromisoformat(point["date"]).weekday() != 2 for point in points):
            raise ValueError("Unexpected observation day for a Wednesday-level series")
        result["response_sha256"] = sha256(content).hexdigest()
        if not points:
            result["status"] = "NO_OBSERVATIONS"
            return result
        retained = 36 if frequency == "monthly" else 104 if frequency.startswith("weekly") else 180
        result["observations"] = points[-retained:]
        result["latest"] = points[-1]
        age = (clock.date() - date.fromisoformat(points[-1]["date"])).days
        result.update(status="STALE" if age > max_age else "OK", observation_age_days=age,
                      stale_after_days=max_age)
    except SourceError as exc:
        result["error"] = str(exc)
    except (ValueError, UnicodeError, KeyError, TypeError, csv.Error):
        result["error"] = "INVALID_SOURCE_SCHEMA"
    return result


def _derived(series):
    metadata = {item["id"]: item for item in series}
    by_id = {item["id"]: {point["date"]: point["value"] for point in item["observations"]}
             for item in series}
    items = []
    yields = sorted(set(by_id["DGS10"]) & set(by_id["DGS2"]))
    if yields:
        day = yields[-1]
        items.append({"id": "DGS10_MINUS_DGS2", "label": "十年減兩年美債收益率",
                      "units": "percentage_points", "date": day,
                      "value": round(by_id["DGS10"][day] - by_id["DGS2"][day], 6),
                      "formula": "DGS10 - DGS2 at the same observation date",
                      "inputs": {"DGS10": day, "DGS2": day}})
    sofr_iorb_days = sorted(set(by_id["SOFR"]) & set(by_id["IORB"]))
    if (sofr_iorb_days and metadata["SOFR"].get("units") == "percent"
            and metadata["IORB"].get("units") == "percent"):
        day = sofr_iorb_days[-1]
        items.append({"id": "SOFR_MINUS_IORB", "label": "SOFR 減 IORB",
                      "units": "basis_points", "date": day,
                      "value": round(100 * (by_id["SOFR"][day] - by_id["IORB"][day]), 6),
                      "formula": "100 * (SOFR - IORB) at the same observation date",
                      "inputs": {"SOFR": day, "IORB": day},
                      "qualification": "DESCRIPTIVE_RATE_DIFFERENCE_NOT_PIT"})
    liquid_basis_valid = all(
        metadata[series_id].get("units") == units
        and metadata[series_id].get("measurement_basis") == basis
        for series_id, units, basis in (
            ("WALCL", "million_usd", "WEDNESDAY_LEVEL"),
            ("WDTGAL", "million_usd", "WEDNESDAY_LEVEL"),
            ("RRPONTSYD", "billion_usd", "DAILY_OPERATION_AMOUNT"),
        )
    )
    liquid_days = sorted(set(by_id["WALCL"]) & set(by_id["WDTGAL"]) & set(by_id["RRPONTSYD"]))
    liquid_days = [day for day in liquid_days if date.fromisoformat(day).weekday() == 2]
    if liquid_days and liquid_basis_valid:
        day = liquid_days[-1]
        value = by_id["WALCL"][day]/1000 - by_id["WDTGAL"][day]/1000 - by_id["RRPONTSYD"][day]
        items.append({"id": "FED_THREE_ITEM_PROXY", "label": "Fed 總資產減 TGA 減 ON RRP",
                      "units": "billion_usd", "date": day, "value": round(value, 6),
                      "formula": "WALCL/1000 - WDTGAL/1000 - RRPONTSYD at the same Wednesday date",
                      "measurement_basis": "WEDNESDAY_LEVELS_AND_SAME_DAY_ON_RRP",
                      "inputs": {"WALCL": day, "WDTGAL": day, "RRPONTSYD": day},
                      "qualification": "DESCRIPTIVE_BALANCE_SHEET_PROXY_NOT_PIT"})
    for series_id, label in (("CPIAUCSL", "CPI 按年變化"), ("CPILFESL", "核心 CPI 按年變化")):
        values = by_id[series_id]
        if not values:
            continue
        day = max(values)
        current = date.fromisoformat(day)
        previous = current.replace(year=current.year-1).isoformat()
        if previous in values and values[previous] > 0:
            items.append({"id": series_id + "_YOY", "label": label, "units": "percent",
                          "date": day, "value": round(100*(values[day]/values[previous]-1), 6),
                          "formula": "100 * (monthly index / same month one year earlier - 1)",
                          "inputs": {series_id: [previous, day]}, "seasonal_adjustment": "SA"})
    return items


def _crypto(clock, fetch):
    endpoints = {
        "funding": "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
        "open_interest": "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT",
    }
    result = {"symbol": "BTCUSDT", "venue": "Binance USD-M", "market": "perpetual",
              "fetched_at_utc": clock.isoformat(), "status": "SOURCE_UNAVAILABLE",
              "funding": None, "open_interest": None,
              "source_url": "https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api"}
    errors = {}
    for kind, endpoint in endpoints.items():
        try:
            data = json.loads(fetch(endpoint))
            if data["symbol"] != "BTCUSDT":
                raise ValueError("Wrong derivatives contract")
            timestamp = number(data["time"]) / 1000
            if timestamp > clock.timestamp() + 300 or timestamp < clock.timestamp() - 3600:
                raise ValueError("Derivatives observation clock outside snapshot window")
            observed = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
            if kind == "funding":
                funding = number(data["lastFundingRate"])
                next_time = number(data["nextFundingTime"]) / 1000
                result[kind] = {
                    "rate_decimal": funding, "rate_percent": round(funding*100, 8),
                    "mark_price_usdt": number(data["markPrice"]), "observed_at_utc": observed,
                    "next_funding_at_utc": datetime.fromtimestamp(next_time, timezone.utc).isoformat(),
                    "definition": "Provider lastFundingRate; variable contract funding interval, no annualization",
                }
            else:
                result[kind] = {"quantity": number(data["openInterest"]), "units": "exchange_contract_quantity",
                                "observed_at_utc": observed}
        except SourceError as exc:
            errors[kind] = str(exc)
        except (ValueError, TypeError, KeyError, OverflowError):
            errors[kind] = "INVALID_SOURCE_SCHEMA_OR_CLOCK"
    result["errors"] = errors
    result["status"] = "OK" if not errors else "PARTIAL" if len(errors) == 1 else "SOURCE_UNAVAILABLE"
    return result


def _sec(clock, fetch, user_agent):
    result = {"status": "SEC_CONTACT_NOT_CONFIGURED", "source": "SEC EDGAR", "filings": [],
              "holdings_status": "NOT_PARSED", "watchlist": [], "fetched_at_utc": clock.isoformat(),
              "note": "只讀最近 13F 申報元資料。完整持倉及季度變化待資訊表核實。"}
    for cik, label in FUNDS:
        result["watchlist"].append({"cik": cik, "label": label,
                                   "source_url": f"https://www.sec.gov/edgar/browse/?CIK={int(cik)}&owner=exclude"})
    if not user_agent or len(user_agent) > 240 or not re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", user_agent):
        return result
    failures = []
    for cik, label in FUNDS:
        try:
            endpoint = f"https://data.sec.gov/submissions/CIK{cik}.json"
            payload = get_bytes(endpoint, user_agent) if fetch is get_bytes else fetch(endpoint)
            data = json.loads(payload)
            if str(data["cik"]).zfill(10) != cik:
                raise ValueError("Wrong SEC entity")
            recent = data["filings"]["recent"]
            forms = recent["form"]
            for key in ("filingDate", "accessionNumber", "primaryDocument", "reportDate"):
                if len(recent[key]) != len(forms):
                    raise ValueError("SEC recent columns misaligned")
            filings = []
            for i, form in enumerate(forms):
                if form not in {"13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A"}:
                    continue
                filed = date.fromisoformat(recent["filingDate"][i])
                if filed > clock.date():
                    continue
                accession = recent["accessionNumber"][i]
                if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
                    raise ValueError("Invalid SEC accession")
                filings.append({
                    "cik": cik, "manager": label, "form": form, "filed_date": filed.isoformat(),
                    "report_period": recent["reportDate"][i], "accession": accession,
                    "source_url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}-index.html",
                    "coverage": "FILING_METADATA_ONLY",
                })
                if len(filings) == 2:
                    break
            result["filings"].extend(filings)
        except SourceError as exc:
            failures.append({"cik": cik, "error": str(exc)})
        except (ValueError, TypeError, KeyError):
            failures.append({"cik": cik, "error": "INVALID_SOURCE_SCHEMA"})
        time.sleep(0.15)
    result.update(status="PARTIAL" if failures else "OK", errors=failures)
    return result


def collect_context(now=None, fetch=get_bytes, sec_user_agent=None):
    clock = utc(now)
    with ThreadPoolExecutor(max_workers=3) as executor:
        series = list(executor.map(lambda spec: _series(spec, clock, fetch), SERIES))
    linked = [{"id": i, "label": label, "category": category, "status": "LINK_ONLY_SOURCE_RESTRICTION",
               "source_url": "https://fred.stlouisfed.org/series/" + i,
               "reason": reason, "observations": [], "latest": None}
              for i, label, category, reason in LINK_ONLY]
    derived = _derived(series)
    states = {s["id"]: s["status"] for s in series}
    for item in derived:
        item["status"] = "OK" if all(states[key] == "OK" for key in item["inputs"]) else "STALE_OR_MISSING_INPUT"
    crypto = _crypto(clock, fetch)
    sec = _sec(clock, fetch, sec_user_agent or os.environ.get("SEC_USER_AGENT"))
    return {
        "schema_version": 1, "generated_at_utc": clock.isoformat(),
        "status": "OK" if all(s["status"] == "OK" for s in series)
                  and crypto["status"] == "OK" and sec["status"] == "OK" else "PARTIAL",
        "macro": {"series": series + linked, "derived": derived},
        "crypto_derivatives": crypto,
        "sec_13f": sec,
        "limitations": NOTES,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true", help="refresh even when today's context already exists")
    args = parser.parse_args()
    now = utc()
    if args.output.is_file() and not args.force:
        try:
            old = json.loads(args.output.read_text())
            generated = datetime.fromisoformat(old["generated_at_utc"])
            if old["schema_version"] == 1 and utc(generated).date() == now.date():
                print("Context cache already refreshed for this UTC date")
                return
        except (ValueError, KeyError, TypeError):
            pass
    result = collect_context(now)
    body = (json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()
    if len(body) > MAX_OUTPUT:
        raise ValueError("Context output exceeds bounded size")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=args.output.parent, prefix=".context-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(body)
        os.replace(temporary, args.output)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    print(json.dumps({"status": result["status"], "bytes": len(body),
                      "economic_series_ok": sum(s["status"] == "OK" for s in result["macro"]["series"]),
                      "crypto": result["crypto_derivatives"]["status"], "sec": result["sec_13f"]["status"]}))


if __name__ == "__main__":
    main()
