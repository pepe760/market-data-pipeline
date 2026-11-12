"""Collect a bounded public-market terminal snapshot from fresh public sources."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import statistics
import tempfile
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from pipeline import closed_sessions

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent
USER_AGENT = "WilliamResearchTerminal/1.0 (personal public market research)"
MAX_BODY = 12 * 1024 * 1024
RETURN_PERIODS = (1, 3, 5, 10, 20, 60, 120)
SMA_PERIODS = (20, 50, 200)
STABLE_SYMBOLS = {"USDT", "USDC", "BUSD", "FDUSD", "TUSD", "USDP", "DAI", "USDD", "USDE",
                  "USD1", "USDS", "GUSD", "PYUSD", "EURC", "EURI", "PAXG", "XAUT"}
EXCLUDED_CRYPTO = re.compile(r"tokenized|wrapped|bridged|staked|synthetic|stablecoin|liquid staking|leveraged",
                             re.IGNORECASE)


class SourceError(ValueError):
    """A redacted, provider-level response fault."""


def utc_now():
    return datetime.now(UTC)


def iso(stamp):
    return stamp.astimezone(UTC).isoformat()


def number(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError, OverflowError):
        return None


def fetch(url, *, json_data=True):
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,text/html"})
    try:
        with urlopen(request, timeout=12) as response:
            payload = response.read(MAX_BODY + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"HTTP_{exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__) from None
    if len(payload) > MAX_BODY:
        raise SourceError("RESPONSE_TOO_LARGE")
    try:
        return json.loads(payload) if json_data else payload.decode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise SourceError("INVALID_RESPONSE") from None


class Sp500Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.depth = 0
        self.cell = None
        self.row = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if dict(attrs).get("id") == "constituents":
                self.inside = True
            if self.inside:
                self.depth += 1
        if self.inside and self.depth == 1:
            if tag == "tr":
                self.row = []
            if tag in {"td", "th"}:
                self.cell = ""

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if self.inside and self.depth == 1 and tag in {"td", "th"} and self.cell is not None:
            self.row.append(self.cell.strip())
            self.cell = None
        if self.inside and tag == "tr" and len(self.row) >= 4:
            self.rows.append(self.row)
        if self.inside and tag == "table":
            self.depth -= 1
            if self.depth == 0:
                self.inside = False


def fresh_us_directory():
    listed = {}
    sources = (
        ("nasdaqlisted.txt", "Symbol", "Security Name", "Test Issue"),
        ("otherlisted.txt", "ACT Symbol", "Security Name", "Test Issue"),
    )
    for file, symbol_key, name_key, test_key in sources:
        text = fetch("https://www.nasdaqtrader.com/dynamic/SymDir/" + file, json_data=False)
        lines = text.splitlines()
        header = lines[0].split("|")
        for line in lines[1:]:
            row = dict(zip(header, line.split("|")))
            symbol = row.get(symbol_key, "").strip()
            name = row.get(name_key, "")
            if (row.get(test_key) != "N" or row.get("ETF") != "N"
                    or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,8}", symbol)
                    or re.search(r"warrant|depositary share|preferred|rights|units", name, re.IGNORECASE)):
                continue
            listed[symbol] = {"symbol": symbol, "name": name, "sector": "未分類"}
    if len(listed) < 2000:
        raise SourceError("US_DIRECTORY_INCOMPLETE")
    return listed


def fresh_crypto_universe(target, tickers, now):
    market = {row["symbol"][:-4]: row for row in tickers
              if isinstance(row.get("symbol"), str) and re.fullmatch(r"[A-Z0-9]{2,20}USDT", row["symbol"])
              and number(row.get("quoteVolume")) and number(row.get("lastPrice"))
              and number(row.get("closeTime")) is not None
              and now.timestamp()-3600 <= float(row["closeTime"])/1000 <= now.timestamp()+30}
    selected, used = [], set()
    for page in range(1, 7):
        coins = fetch("https://api.coingecko.com/api/v3/coins/markets?" + urlencode({
            "vs_currency": "usd", "order": "market_cap_desc", "per_page": 250,
            "page": page, "sparkline": "false"
        }))
        if not isinstance(coins, list):
            raise SourceError("INVALID_CRYPTO_DIRECTORY")
        for coin in coins:
            symbol, name = str(coin.get("symbol", "")).upper(), str(coin.get("name", ""))
            identifier = coin.get("id")
            if (symbol in used or symbol not in market or symbol in STABLE_SYMBOLS
                    or EXCLUDED_CRYPTO.search(name) or not isinstance(identifier, str)
                    or not re.fullmatch(r"[a-z0-9-]{1,100}", identifier)):
                continue
            selected.append({"symbol": symbol + "USDT", "name": name, "market": "Crypto", "sector": "加密貨幣",
                             "currency": "USDT", "candidate_asset_id": identifier, "provider": "Binance",
                             "identity_status": "UNVERIFIED_SYMBOL_MATCH"})
            used.add(symbol)
            if len(selected) == target:
                return selected
    return selected


def assemble_universe(config, previous, now, tickers, refresh=False):
    stored = previous.get("universe", {})
    if stored and not refresh and str(stored.get("asof", ""))[:10] == now.date().isoformat():
        configured_hk = {symbol for symbol, _ in config["hk"]}
        instruments = [row for row in stored["instruments"]
                       if row["market"] != "HK" or row["symbol"] in configured_hk]
        return dict(stored, instruments=instruments)
    errors = []
    directory = {}
    try:
        directory = fresh_us_directory()
    except SourceError as exc:
        errors.append({"source": "Nasdaq Trader directory", "error": str(exc)})
    stock_rows = {}
    for symbol in config["us_core"]:
        if symbol in directory or not directory:
            stock_rows[symbol] = dict(directory.get(symbol, {"symbol": symbol, "name": symbol, "sector": "未分類"}))
    try:
        parser = Sp500Parser()
        parser.feed(fetch("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", json_data=False))
        eligible = [r for r in parser.rows[1:] if len(r) >= 4 and re.fullmatch(r"[A-Z][A-Z.]{0,8}", r[0])]
        if len(eligible) < 450:
            raise SourceError("SP500_DIRECTORY_INCOMPLETE")
        for row in eligible:
            if row[0] in directory or not directory:
                stock_rows[row[0]] = {"symbol": row[0], "name": row[1], "sector": row[2]}
    except SourceError as exc:
        errors.append({"source": "Wikipedia S&P 500 directory", "error": str(exc)})
        for row in stored.get("instruments", []):
            if row.get("market") == "US":
                stock_rows.setdefault(row["symbol"], {k:row[k] for k in ("symbol", "name", "sector")})
    for symbol in sorted(directory):
        if len(stock_rows) >= config["us_target"]:
            break
        stock_rows.setdefault(symbol, directory[symbol])
    instruments = [dict(row, market="US", currency="USD", provider="Yahoo Finance")
                   for row in list(stock_rows.values())[:config["us_target"]]]
    instruments += [{"symbol": s, "name": n, "sector": "港股", "market": "HK", "currency": "HKD",
                     "provider": "Yahoo Finance"} for s,n in config["hk"]]
    instruments += [{"symbol": s, "name": n, "sector": "外匯", "market": "FX", "currency": "報價貨幣",
                     "provider": "Yahoo Finance"} for s,n in config["fx"]]
    instruments += [{"symbol": s, "name": n, "sector": sector, "market": "Commodity" if sector=="商品" else "ETF", "currency": "USD",
                     "instrument_kind": "ETF_PROXY" if sector=="商品" else "ETF",
                     "provider": "Yahoo Finance"} for s,n,sector in config["etfs"]]
    try:
        crypto = fresh_crypto_universe(config["crypto_target"], tickers, now)
        if len(crypto) < config["crypto_target"]:
            errors.append({"source": "CoinGecko × Binance", "error": "CRYPTO_TARGET_NOT_REACHED"})
    except SourceError as exc:
        errors.append({"source": "CoinGecko × Binance", "error": str(exc)})
        crypto = [row for row in stored.get("instruments", []) if row["market"] == "Crypto"]
    instruments += crypto
    # ETF entry wins the display group for a duplicate US-core ETF symbol.
    unique = {row["symbol"]: row for row in instruments}
    return {
        "asof": iso(now), "instruments": list(unique.values()), "directory_us_available": len(directory),
        "priced_us_target": config["us_target"], "crypto_target": config["crypto_target"],
        "directory_errors": errors,
        "directory_sources": [
            "https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs",
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            "https://www.coingecko.com/en/api/documentation",
            "https://data-api.binance.vision",
        ],
    }


def analytics(series, annual_days):
    closes = [row["close"] for row in series]
    latest = closes[-1] if closes else None
    returns = {f"d{n}": latest / closes[-n-1] - 1 if len(closes) > n else None for n in RETURN_PERIODS}
    sma = {f"d{n}": sum(closes[-n:]) / n if len(closes) >= n else None for n in SMA_PERIODS}
    changes = [math.log(right / left) for left,right in zip(closes[-21:-1], closes[-20:])]
    vol20 = statistics.stdev(changes) * math.sqrt(annual_days) if len(changes) == 20 else None
    return returns, sma, vol20


def paired_excess(left,right,periods=20):
    first={row["date"]:row["close"] for row in left}
    second={row["date"]:row["close"] for row in right}
    common=sorted(first.keys() & second.keys())
    if len(common)<periods+1:
        return None,None
    start,end=common[-periods-1],common[-1]
    return first[end]/first[start]-second[end]/second[start],{
        "start":start,"end":end,"common_dates":periods+1,
        "clock_qualification":"DIFFERENT_VENUE_CLOSES_DESCRIPTIVE_ONLY"
    }


def yahoo_row(instrument, now):
    symbol = instrument["symbol"]
    alias = {"BRK.B":"BRK-B", "BF.B":"BF-B"}.get(symbol,symbol)
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(alias, safe="") + "?range=2y&interval=1d"
    payload = fetch(url)
    received = utc_now()
    try:
        result, = payload["chart"]["result"]
        meta = result["meta"]
        if meta.get("symbol") != alias:
            raise SourceError("CHART_SYMBOL_MISMATCH")
        bars = result["indicators"]["quote"][0]
        adjusted = result["indicators"].get("adjclose", [{}])[0].get("adjclose", [])
        use_adjusted = isinstance(adjusted,list) and any(number(value) is not None and number(value)>0 for value in adjusted)
        zone = ZoneInfo(meta.get("exchangeTimezoneName", "UTC"))
        series = []
        for i, epoch in enumerate(result.get("timestamp", [])):
            if number(epoch) is None or epoch > received.timestamp()+30:
                continue
            close = number(adjusted[i]) if use_adjusted and i<len(adjusted) else number(bars["close"][i]) if not use_adjusted else None
            if close is None or close <= 0:
                continue
            day = datetime.fromtimestamp(epoch, UTC).astimezone(zone).date().isoformat()
            series.append({"date": day, "close": round(close, 6), "volume": number(bars.get("volume", [None]*len(result["timestamp"]))[i])})
        # Latest corrected vendor row wins a duplicate daily date.
        series = sorted({row["date"]: row for row in series}.values(), key=lambda row: row["date"])[-400:]
        stamp = number(meta.get("regularMarketTime"))
        price = number(meta.get("regularMarketPrice"))
        if not series or stamp is None or price is None or price <= 0 or stamp > received.timestamp()+30:
            raise SourceError("PRICE_OR_CLOCK_UNAVAILABLE")
        price_asof = datetime.fromtimestamp(stamp, UTC)
        if received - price_asof > timedelta(days=8):
            raise SourceError("PRICE_MORE_THAN_8_DAYS_OLD")
        partial = series[-1]["date"] == received.astimezone(zone).date().isoformat()
        close_at = meta.get("currentTradingPeriod",{}).get("regular",{}).get("end")
        if close_at and received.timestamp() > close_at + 900:
            partial = False
        returns, sma, vol = analytics(series, 252)
        return dict(instrument, currency=meta.get("currency", instrument["currency"]), price=price,
                    price_asof=iso(price_asof), received_at=iso(received), last_attempt=iso(received),
                    returns=returns, sma=sma, vol20=vol, analysis_price=series[-1]["close"],
                    analytics_asof=series[-1]["date"], analytics_partial=partial, analytics_observed_at=iso(received),
                    analytics_basis="Yahoo vendor adjusted daily close" if use_adjusted else "Yahoo vendor daily close (unadjusted fallback for entire series)",
                    series=series, source_url=url, quality="OBSERVED", errors=[])
    except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, SourceError):
            raise
        raise SourceError("INVALID_CHART_SCHEMA") from None


def nasdaq_official_close(row, session):
    import exchange_calendars as xc

    symbol = row["symbol"]
    url = f"https://api.nasdaq.com/api/company/{symbol}/historical-nocp?timeframe=d5"
    day = datetime.fromisoformat(session).strftime("%m/%d/%Y")
    try:
        payload = fetch(url)
        if payload["status"]["rCode"] != 200:
            raise SourceError("NASDAQ_NOCP_RESPONSE")
        table = payload["data"]["nocp"]["nocpTable"]
        prices = [number(point["price"].removeprefix("$").replace(",", ""))
                  for point in table if point["date"] == day]
        price, = prices
        if price is None or price <= 0:
            raise SourceError("NASDAQ_NOCP_PRICE")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SourceError("NASDAQ_NOCP_UNAVAILABLE") from None
    session_close = xc.get_calendar("XNYS").session_close(session).to_pydatetime()
    observed = iso(utc_now())
    return dict(row, price=price, price_asof=iso(session_close), received_at=observed,
                last_attempt=observed, quality="OFFICIAL_CLOSE", errors=[],
                source_url=f"https://www.nasdaq.com/market-activity/stocks/{symbol.lower()}/historical-nocp",
                official_close={"date": session, "price": price, "source_url": url})


def binance_row(instrument, now, ticker, *, history=True):
    pair = instrument["symbol"]
    if not isinstance(ticker,dict) or ticker.get("symbol") != pair:
        raise SourceError("BINANCE_TICKER_MISSING")
    price, stamp = number(ticker.get("lastPrice")), number(ticker.get("closeTime"))
    if price is None or price <= 0 or stamp is None:
        raise SourceError("BINANCE_PRICE_OR_CLOCK_MISSING")
    clock = datetime.fromtimestamp(stamp/1000, UTC)
    if not timedelta(seconds=-30) <= utc_now()-clock <= timedelta(hours=1):
        raise SourceError("BINANCE_TICKER_STALE")
    series = []
    if history:
        rows = fetch("https://data-api.binance.vision/api/v3/klines?" + urlencode({
            "symbol": pair, "interval": "1d", "limit": 401
        }))
        if not isinstance(rows, list):
            raise SourceError("BINANCE_DAILY_SCHEMA")
        for row in rows:
            if not isinstance(row,list) or len(row)<7 or number(row[0]) is None or row[0]/1000>utc_now().timestamp()+30:
                continue
            close = number(row[4]) if len(row)>6 else None
            if close is None or close<=0:
                continue
            series.append({"date":datetime.fromtimestamp(row[0]/1000,UTC).date().isoformat(),
                           "close":close, "volume":number(row[5])})
        series=series[-400:]
    if not series:
        raise SourceError("BINANCE_DAILY_EMPTY")
    returns,sma,vol = analytics(series,365)
    received=utc_now()
    return dict(instrument, price=price, price_asof=iso(clock), received_at=iso(received), last_attempt=iso(received),
                returns=returns, sma=sma, vol20=vol, analysis_price=series[-1]["close"],
                analytics_asof=series[-1]["date"], analytics_partial=series[-1]["date"]==received.date().isoformat(),
                analytics_observed_at=iso(received),
                analytics_basis="Binance USDT spot daily close, current UTC day partial",
                series=series, source_url="https://data-api.binance.vision/api/v3/klines",
                quality="OBSERVED", errors=[])


def active_market(market, now):
    if market == "Crypto":
        return True
    zone = ZoneInfo("Asia/Hong_Kong" if market == "HK" else "America/New_York")
    local = now.astimezone(zone)
    if local.weekday() >= 5:
        return market == "FX" and local.weekday() == 6 and local.hour >= 17
    minute = local.hour*60+local.minute
    if market == "HK":
        return 570 <= minute <= 735 or 780 <= minute <= 975
    if market == "FX":
        return True
    return 570 <= minute <= 975


def empty_row(instrument):
    return dict(instrument,price=None,price_asof=None,received_at=None,quality="AWAITING_FIRST_OBSERVATION",
                returns={f"d{n}":None for n in RETURN_PERIODS},sma={f"d{n}":None for n in SMA_PERIODS},
                vol20=None,analysis_price=None,series=[],rs={"spy":None,"btc":None,"peer":None},errors=[])


def missing_cash_close(row, latest_closed_session):
    if not latest_closed_session:
        return False
    zone = ZoneInfo("Asia/Hong_Kong" if row["market"] == "HK" else "America/New_York")
    try:
        quote_day = datetime.fromisoformat(row.get("price_asof") or "").astimezone(zone).date().isoformat()
    except (ValueError, TypeError):
        return True
    if quote_day < latest_closed_session:
        return True
    official = row.get("official_close") or {}
    if official.get("date") == latest_closed_session and (number(official.get("price")) or 0) > 0:
        return False
    day = row.get("analytics_asof") or ""
    return (day < latest_closed_session
            or (day == latest_closed_session and row.get("analytics_partial", False))
            or not any(point["date"] == latest_closed_session and (number(point["close"]) or 0) > 0
                       for point in row.get("series", [])))


def freshness(row, now, latest_closed_session=None):
    if row.get("price_asof") is None:
        return False
    if missing_cash_close(row, latest_closed_session):
        return False
    try:
        age=now-datetime.fromisoformat(row["price_asof"].replace("Z","+00:00"))
        if age < timedelta(seconds=-30):
            return False
        return age <= timedelta(minutes=40) if active_market(row["market"],now) else age <= timedelta(days=4)
    except (ValueError,TypeError):
        return False


def collect(previous, output_dir, *, max_requests=180, workers=6, force=False,
            refresh_universe=False, intraday_budget=24, now=None):
    now=now or utc_now()
    closed_us_sessions = closed_sessions(now)
    latest_us_close = closed_us_sessions[-1] if closed_us_sessions else None
    closed_hk_sessions = closed_sessions(now, "XHKG")
    latest_hk_close = closed_hk_sessions[-1] if closed_hk_sessions else None
    latest_close = {"US": latest_us_close, "ETF": latest_us_close,
                    "Commodity": latest_us_close, "HK": latest_hk_close}
    config=json.loads((ROOT/"terminal_universe.json").read_text())
    errors=[]
    try:
        ticker_rows=fetch("https://data-api.binance.vision/api/v3/ticker/24hr?type=MINI")
        if not isinstance(ticker_rows,list):
            raise SourceError("INVALID_BINANCE_TICKERS")
    except SourceError as exc:
        ticker_rows=[]
        errors.append({"source":"Binance","error":str(exc)})
    universe=assemble_universe(config,previous,now,ticker_rows,refresh=refresh_universe)
    instruments=universe["instruments"]
    old={row["symbol"]:row for row in previous.get("markets",[])}
    tickers={row["symbol"]:row for row in ticker_rows if isinstance(row,dict) and "symbol" in row}
    priority={"SPY":0,"BTCUSDT":1,"QQQ":2,"SOXX":3,"GLD":4,"TLT":5,"2800.HK":6}
    core_us = set(config["us_core"])
    rows={i["symbol"]:dict(old.get(i["symbol"],empty_row(i)),**i) for i in instruments}
    for row in rows.values():
        path=output_dir/"series"/(hashlib.sha256(row["symbol"].encode()).hexdigest()[:20]+".json")
        if path.exists():
            saved=json.loads(path.read_text())
            if (saved.get("symbol")==row["symbol"] and saved.get("analytics_asof")==row.get("analytics_asof")
                    and isinstance(saved.get("series"),list) and len(saved["series"])>len(row["series"])):
                row["series"]=saved["series"]
    # Price-only crypto refresh is one bulk call. Histories rotate under the request budget.
    for instrument in instruments:
        if instrument["market"]!="Crypto":
            continue
        row=rows[instrument["symbol"]]
        ticker=tickers.get(instrument["symbol"])
        if ticker:
            price,clock=number(ticker.get("lastPrice")),number(ticker.get("closeTime"))
            if price and clock and now.timestamp()-3600 <= clock/1000 <= now.timestamp()+30:
                row.update(price=price,price_asof=iso(datetime.fromtimestamp(clock/1000,UTC)),
                           received_at=iso(now),price_only_refreshed_at=iso(now))
    candidates=[]
    catch_up=set()
    for instrument in instruments:
        symbol=instrument["symbol"]
        prior=old.get(symbol)
        if prior and missing_cash_close(rows[symbol], latest_close.get(instrument["market"])):
            catch_up.add(symbol)
        due=force or not prior or active_market(instrument["market"],now) or symbol in catch_up
        if due:
            candidates.append(instrument)
    candidates.sort(key=lambda i:(priority.get(i["symbol"],9),
                                 i["symbol"] not in catch_up,
                                 not (i["symbol"] in catch_up and i["symbol"] in core_us),
                                 old.get(i["symbol"],{}).get("last_attempt",""),i["symbol"]))
    intraday_budget=min(intraday_budget,max(0,max_requests-1))
    daily_budget=max_requests-intraday_budget
    chosen=candidates[:daily_budget]
    limited=threading.Event()
    def one(instrument):
        symbol = instrument["symbol"]
        if limited.is_set():
            return symbol,None,"NOT_ATTEMPTED_RATE_LIMIT"
        try:
            previous_row = rows[symbol]
            if (symbol == "ACAA" and symbol in catch_up
                    and any(error.get("error") == "US_CLOSE_NOT_AVAILABLE" for error in previous_row.get("errors", []))):
                return symbol, nasdaq_official_close(previous_row, latest_us_close), None
            result=(binance_row(instrument,now,tickers.get(instrument["symbol"]))
                    if instrument["market"]=="Crypto" else yahoo_row(instrument,now))
            market = instrument["market"]
            if symbol in catch_up and missing_cash_close(result, latest_close.get(market)):
                raise SourceError("HK_CLOSE_NOT_AVAILABLE" if market == "HK" else "US_CLOSE_NOT_AVAILABLE")
            if previous_row.get("official_close"):
                if result["price_asof"] <= previous_row["price_asof"]:
                    return symbol, dict(previous_row, last_attempt=result["last_attempt"]), None
                result["official_close"] = previous_row["official_close"]
            return symbol,result,None
        except (SourceError,ValueError,TypeError,OverflowError) as exc:
            if isinstance(exc,SourceError) and str(exc)=="HTTP_429":
                limited.set()
            return symbol,None,str(exc) if isinstance(exc,SourceError) else type(exc).__name__
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for symbol,row,error in pool.map(one,chosen):
            if error is None:
                rows[symbol]=row
            else:
                rows[symbol].update(quality="SOURCE_ERROR_PREVIOUS_RETAINED" if symbol in old else "SOURCE_ERROR",
                                    errors=[{"asof":iso(now),"error":error}])
                if error!="NOT_ATTEMPTED_RATE_LIMIT":
                    rows[symbol]["last_attempt"]=iso(now)
                errors.append({"symbol":symbol,"error":error})
    from terminal_intraday import enrich
    intraday_coverage=enrich(
        rows,old,instruments,output_dir,budget=intraday_budget,workers=workers,force=force,
        clock=utc_now,fetch=fetch,write_json=write_json,active_market=active_market,limited=limited,
    )
    markets=list(rows.values())
    finished=utc_now()
    benchmarks={s:rows.get(s,{}) for s in ("SPY","BTCUSDT","2800.HK")}
    for row in markets:
        row["fresh"]=freshness(row,finished,latest_close.get(row["market"]))
        peer=benchmarks["2800.HK" if row["market"]=="HK" else "BTCUSDT" if row["market"]=="Crypto" else "SPY"]
        row["rs"]={}
        row["rs_window"]={}
        for key,bench in (("spy",benchmarks["SPY"]),("btc",benchmarks["BTCUSDT"]),("peer",peer)):
            excess,window=paired_excess(row["series"],bench.get("series",[]))
            row["rs"][key]=excess
            row["rs_window"][key]=window
        for period in (3,10):
            closes=[item["close"] for item in row["series"]]
            row["returns"][f"d{period}"]=closes[-1]/closes[-period-1]-1 if len(closes)>period else None
        row["rs_daily"]={}
        row["rs_daily_windows"]={}
        for period in (3,10):
            relative,window=paired_excess(row["series"],peer.get("series",[]),period)
            row["rs_daily"][f"d{period}"]=relative
            row["rs_daily_windows"][f"d{period}"]=window
        row["above_sma200"]=(row.get("analysis_price")>row["sma"]["d200"]
                             if row.get("analysis_price") is not None and row["sma"].get("d200") is not None else None)
    groups=[]
    chosen_symbols={item["symbol"] for item in chosen}
    current_errors={item["symbol"]:item["error"] for item in errors if "symbol" in item}
    for market in ("US","HK","Crypto","FX","ETF","Commodity"):
        subset=[row for row in markets if row["market"]==market]
        groups.append({"market":market,"target":config["crypto_target"] if market=="Crypto" else len(subset),
                       "stored":sum(r["price"] is not None for r in subset),"fresh":sum(r["fresh"] for r in subset),
                       "stale":sum(r["price"] is not None and not r["fresh"] for r in subset),
                       "failed":sum(r["symbol"] in current_errors and current_errors[r["symbol"]]!="NOT_ATTEMPTED_RATE_LIMIT" for r in subset),
                       "retained_source_errors":sum(bool(r.get("errors")) for r in subset),
                       "unattempted":sum(r["symbol"] not in chosen_symbols or current_errors.get(r["symbol"])=="NOT_ATTEMPTED_RATE_LIMIT" for r in subset),
                       "updated":sum(r["symbol"] in chosen_symbols and r["symbol"] not in current_errors for r in subset),
                       "bulk_price_updated":sum(r.get("price_only_refreshed_at")==iso(now) for r in subset)})
    eligible=[r for r in markets if r["market"]=="US" and r["above_sma200"] is not None]
    breadth={"us_above_sma200":sum(r["above_sma200"] for r in eligible)/len(eligible) if eligible else None,
             "us_breadth_n":len(eligible),"us_breadth_population":"priced public terminal universe, rotating and source-qualified"}
    output_dir.mkdir(parents=True,exist_ok=True)
    (output_dir/"series").mkdir(exist_ok=True)
    for row in markets:
        safe=hashlib.sha256(row["symbol"].encode()).hexdigest()[:20]+".json"
        full=row.get("series",[])
        path=output_dir/"series"/safe
        if full and (len(full)>60 or path.exists()):
            write_json(path,{"symbol":row["symbol"],"analytics_asof":row.get("analytics_asof"),"series":full})
        row["series_path"]="data/series/"+safe if full and path.exists() else None
        row["series"]=full[-60:]
    snapshot={
        "schema_version":1,"generated_at":iso(finished),"status":"PARTIAL" if errors or intraday_coverage["failed"] or universe["directory_errors"] or any(not r["fresh"] for r in markets) else "COMPLETE",
        "coverage":{"groups":groups,"attempted":len(chosen)+intraday_coverage["attempted"],
                    "daily_attempted":len(chosen),"daily_request_budget":daily_budget,
                    "request_budget":max_requests,"source_errors":errors,"intraday":intraday_coverage,
                    "directory_us_available":universe["directory_us_available"],"directory_errors":universe["directory_errors"],
                    "priced_us_target":config["us_target"],"crypto_target":config["crypto_target"]},
        "markets":markets,"universe":universe,"context":{"breadth":breadth,"source_context_file":"data/context.json"},
        "methodology":{
            "returns":"decimal adjusted daily-close returns for Yahoo where supplied, price returns for Binance",
            "rs":"difference of returns across21 common daily dates, heterogeneous venue close clocks; descriptive comparison",
            "volatility":"20 daily log-return sample SD annualized 252 stock/ETF/FX or365 crypto",
            "trend":"moving averages derived from available vendor daily closes; current session can be partial",
            "cadence":"15-minute target, observed timestamps and rotating histories; GitHub cron best effort",
            "intraday":"60-name core, closed15m bars; exact contiguous UTCwindows forh1/h4; no overnight/lunch/gap filling",
            "market_gate":"cash window plus bounded US and Hong Kong close catch-up after each official session and 90-minute publication allowance",
            "data_scope":"new public-source market facts and computed descriptive analytics",
        },
    }
    write_json(output_dir/"latest.json",snapshot)
    return snapshot


def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile("w",dir=path.parent,delete=False,encoding="utf-8") as handle:
        json.dump(value,handle,ensure_ascii=False,separators=(",",":"),allow_nan=False)
        temporary=handle.name
    os.replace(temporary,path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",type=Path,default=Path("public_data"))
    parser.add_argument("--max-requests",type=int,default=180)
    parser.add_argument("--workers",type=int,default=6)
    parser.add_argument("--intraday-budget",type=int,default=24)
    parser.add_argument("--force",action="store_true")
    parser.add_argument("--refresh-universe",action="store_true")
    args=parser.parse_args()
    if not 1<=args.max_requests<=1860 or not 1<=args.workers<=8 or not 0<=args.intraday_budget<=60:
        parser.error("total history budget must be1..1860, workers1..8 and intraday budget0..60")
    prior=args.output_dir/"latest.json"
    previous=json.loads(prior.read_text()) if prior.exists() else {}
    snapshot=collect(previous,args.output_dir,max_requests=args.max_requests,workers=args.workers,
                     force=args.force,refresh_universe=args.refresh_universe,intraday_budget=args.intraday_budget)
    print(json.dumps({"status":snapshot["status"],"generated_at":snapshot["generated_at"],
                      "coverage":snapshot["coverage"],"bytes":prior.stat().st_size},ensure_ascii=False))
    return 0 if any(r["price"] is not None for r in snapshot["markets"]) else 2


if __name__=="__main__":
    raise SystemExit(main())
