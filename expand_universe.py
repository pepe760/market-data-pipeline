"""Private universe comparison/discovery; never writes licensed lists to Git."""
import argparse,datetime as dt,json,re,urllib.parse,xml.etree.ElementTree as ET
from pathlib import Path
import intraday15m as core

def archive_prefixes(blob):
    root=ET.fromstring(blob);ns={'s':'http://s3.amazonaws.com/doc/2006-03-01/'}
    prefixes=[x.text for x in root.findall('s:CommonPrefixes/s:Prefix',ns)]
    return prefixes,root.findtext('s:IsTruncated','false',ns)=='true',root.findtext('s:NextMarker',None,ns)

def eligible_crypto(symbol):
    if not re.fullmatch('[A-Z0-9]{2,30}USDT',symbol):return False
    base=symbol[:-4]
    return base not in {'USDC','USDP','TUSD','BUSD','DAI','FDUSD','USD1','USDE','EUR','GBP','AUD','BRL','TRY','RUB','BIDR','IDRT','NGN','UAH','ZAR','VAI'} and not base.endswith(('UP','DOWN','BULL','BEAR'))

def compare(sharadar,listings,old):
    active=sharadar[(sharadar.isdelisted=='N') & sharadar.category.fillna('').str.contains('Common Stock') &
                    sharadar.exchange.isin(['NASDAQ','NYSE','NYSEMKT']) & (sharadar.currency=='USD')]
    historical=set(sharadar.ticker.dropna());candidate=set(active.ticker.dropna())
    current=set(listings);matched=sorted(candidate & current);missing=sorted(candidate-current)
    return {'source_rows':len(sharadar),'historical_symbols':len(historical),'snapshot_active_common':len(candidate),
      'matched_current_listing':len(matched),'old_price_symbols':len(old),'old_matched':len(set(old)&set(matched)),
      'new_matched':len(set(matched)-set(old)),'stocks':matched,'snapshot_active_not_current':missing,
      'old_not_matched':sorted(set(old)-set(matched)),
      'qualification':'CURRENT_DIRECTORY_INTERSECTION_WITH_STALE_SHARADAR_METADATA_NOT_HISTORICAL_PIT; no alias/successor splice'}

def discover(out):
    out.mkdir(parents=True,exist_ok=False);http=core.HTTP(20,16000000)
    symbols=set();records=[]
    for name,kind in [('nasdaqlisted.txt','us_nasdaq'),('otherlisted.txt','us_other')]:
        b=http.get('https://www.nasdaqtrader.com/dynamic/SymDir/'+name);(out/name).write_bytes(b)
        rows=core.listing_symbols(b,kind);records+=rows
        symbols.update(r['symbol'] for r in rows if r.get('etf')!='Y')
    import pandas as pd
    p=Path('/Users/wongmingwa/projects/data/sources/sharadar/shdr_tickers.parquet')
    base=Path('/Users/wongmingwa/projects/data/market_data_pipeline/batches')
    latest=max(base.glob('*/prices.jsonl'),key=lambda p:p.stat().st_mtime)
    old={json.loads(x)['ticker'] for x in latest.open()}
    comparison=compare(pd.read_parquet(p),symbols,old)
    comparison.update(observed_at=core.now().isoformat(),sharadar_file=str(p),sharadar_sha256=core.sha(p.read_bytes()),
                      sharadar_snapshot='2026-06-20 export; not refreshed by discovery',directory_records=records)
    core.save(out/'stock_comparison.json',comparison)
    marker='';all_symbols=set();complete=False
    for page in range(10):
        query={'delimiter':'/','prefix':'data/spot/daily/klines/','max-keys':1000}
        if marker:query['marker']=marker
        b=http.get('https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?'+urllib.parse.urlencode(query))
        (out/f'crypto_directory_{page}.xml').write_bytes(b)
        prefixes,truncated,next_marker=archive_prefixes(b)
        all_symbols.update(x.rstrip('/').rsplit('/',1)[-1] for x in prefixes)
        if not truncated:complete=True;break
        if not prefixes:raise ValueError('truncated empty listing')
        next_marker=next_marker or prefixes[-1]
        if next_marker<=marker:raise ValueError('non-advancing listing')
        marker=next_marker
    if not complete:raise ValueError('directory limit; incomplete universe')
    crypto=sorted(s for s in all_symbols if eligible_crypto(s))
    core.save(out/'candidate_universe.json',{'schema_version':1,'observed_at':core.now().isoformat(),
      'stocks':comparison['stocks'],'crypto_candidates':crypto,'crypto_target':300,
      'qualification':'ARCHIVE_DIRECTORY_NOT_CURRENT_TRADING_OR_HISTORICAL_PIT; qualify recent full hourly file before selection',
      'raw_directory_complete':complete,'no_credentials':True})
    print(json.dumps({k:comparison[k] for k in ['source_rows','snapshot_active_common','matched_current_listing','old_price_symbols','old_matched','new_matched']}))
    print(json.dumps({'archive_symbols':len(all_symbols),'eligible_usdt_candidates':len(crypto),'directory_complete':complete}))

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--out',type=Path,required=True);a=ap.parse_args();discover(a.out)
