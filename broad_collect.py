"""Private broad-universe collector. No subscription, trade or public raw data."""
import argparse,datetime as dt,fcntl,gzip,io,json,math,os,random,shutil,tempfile,urllib.parse,uuid,zipfile
from pathlib import Path
import intraday15m as core
import crypto_lifecycle as lifecycle
import stock_capture

def new_batch(root):
    out=root/'batches'/(core.now().strftime('%Y%m%dT%H%M%SZ-')+uuid.uuid4().hex[:12])
    out.mkdir(parents=True)
    shutil.copyfile(__file__,out/'collector_source.py')
    shutil.copyfile(core.__file__,out/'parser_source.py')
    for module in (lifecycle,stock_capture):
        shutil.copyfile(module.__file__,out/Path(module.__file__).name)
    return out

def finite(value):return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)

def validate(config):
    if config.get('schema_version')!=1 or set(config)-{'schema_version','stocks','crypto','crypto_candidates','crypto_target','observed_at','qualification','raw_directory_complete','no_credentials','crypto_asset_ids','universe_source_observed_at','crypto_identity_observed_at','crypto_identity_evidence'}:
        raise ValueError('config schema')
    for key in ('stocks','crypto','crypto_candidates'):
        xs=config.get(key,[])
        if not isinstance(xs,list) or len(xs)>20000 or len(xs)!=len(set(xs)) or any(not isinstance(x,str) or not core.SYMBOL.fullmatch(x) for x in xs):raise ValueError('universe')
    if 'crypto_asset_ids' in config:
        ids=config['crypto_asset_ids']
        if not isinstance(ids,dict) or set(ids)!=set(config.get('crypto',[])) or any(not isinstance(x,str) or not __import__('re').fullmatch('[a-z0-9-]{1,150}',x) for x in ids.values()) or len(set(ids.values()))!=len(ids):raise ValueError('asset identity mapping')
    dates=config.get('crypto_identity_observed_at',{})
    if not isinstance(dates,dict) or set(dates)-set(config.get('crypto',[])):raise ValueError('identity observation dates')
    for value in [*dates.values(),*([config['universe_source_observed_at']] if 'universe_source_observed_at' in config else [])]:
        if not isinstance(value,str) or dt.datetime.fromisoformat(value).tzinfo is None:raise ValueError('aware identity observation date')
    evidence=config.get('crypto_identity_evidence',{})
    if not isinstance(evidence,dict) or set(evidence)-set(config.get('crypto',[])):raise ValueError('identity evidence')
    for record in evidence.values():
        if not isinstance(record,dict) or set(record)!={'url','sha256'} or not isinstance(record['sha256'],str) or not __import__('re').fullmatch('[a-f0-9]{64}',record['sha256']) or not isinstance(record['url'],str):raise ValueError('identity evidence')
        parsed=urllib.parse.urlparse(record['url'])
        if parsed.scheme!='https' or parsed.hostname not in {'api.coingecko.com','api.binance.com','data-api.binance.vision','www.binance.com'} or parsed.username or parsed.password:raise ValueError('public identity evidence')
    return config

def archive_task(task,archive,http,stock_capture_policy='strict'):
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp)
        try:
            try:return core.fetch_task(task,p,http)
            except core.SourceError as exc:
                if task['kind']=='stocks' and stock_capture_policy=='observed-source' and (p/'raw.json').exists():
                    result=stock_capture.observe_empty_history_response((p/'raw.json').read_bytes(),task,core.now().isoformat(),str(exc))
                    if result is not None:return result
                raise
        finally:
            key=core.sha(json.dumps(task,sort_keys=True).encode())
            archive.writestr(key+'/task.json',json.dumps(task))
            for f in p.iterdir():archive.write(f,key+'/'+f.name)

def collect(config,root,lane,date=None,shard=0,shards=24,qualify=False,max_requests=1500,stock_capture_policy='strict'):
    import pandas as pd
    validate(config);root.mkdir(parents=True,exist_ok=True)
    if lane not in ('crypto1h','stocks15m'):raise ValueError('lane')
    if stock_capture_policy not in ('strict','observed-source'):raise ValueError('stock capture policy')
    if not 0<=shard<shards<=64:raise ValueError('shard')
    http=core.HTTP(max_requests,128000000);asof=core.now()
    date=date or str(asof.date()-dt.timedelta(days=1));dt.date.fromisoformat(date)
    if dt.date.fromisoformat(date)>=asof.date():raise ValueError('unfinished UTC date')
    with (root/'.writer.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state_path=root/'checkpoint.json';state=json.loads(state_path.read_text()) if state_path.exists() else {'completed':[]}
        if not isinstance(state.get('completed'),list) or any(not isinstance(x,str) or not __import__('re').fullmatch('[a-f0-9]{64}',x) for x in state['completed']):raise ValueError('checkpoint')
        out=new_batch(root);core.save(out/'config.json',config)
        symbols=(config.get('crypto_candidates',[]) if qualify else config.get('crypto',[])) if lane=='crypto1h' else config['stocks'][shard::shards]
        if lane=='crypto1h' and not 1<=len(symbols)<=1000:raise ValueError('crypto scope')
        grid=core.stock_grid(asof,5) if lane=='stocks15m' else None
        if lane=='stocks15m' and not grid:raise ValueError('no closed stock sessions')
        rows=[];checks=[];selected=[];new=[];already=0;retired=0
        with zipfile.ZipFile(out/'raw_bundle.zip','w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as raw:
            for symbol in symbols:
                task=({'kind':'crypto','market':'spot','symbol':symbol,'date':date,'interval':'1h'} if lane=='crypto1h' else {'kind':'stocks','symbol':symbol,'grid':grid})
                if lane=='stocks15m':
                    task.update(capture_policy_version='OBSERVED_SOURCE_CAPTURE_V1_STRICT_SCHEMA',capture_contract=stock_capture_policy)
                if lane=='crypto1h' and not qualify and 'crypto_asset_ids' in config:
                    identity=config['crypto_asset_ids'][symbol]
                    if symbol=='EPICUSDT' and identity!='epic-chain':
                        raise ValueError('EPIC_IDENTITY_CORRECTION_REQUIRED')
                    task.update(asset_id=identity,asset_classification_version=3 if symbol=='EPICUSDT' or symbol in config.get('crypto_identity_observed_at',{}) else 2)
                    if symbol in config.get('crypto_identity_observed_at',{}):task['asset_identity_available_at']=config['crypto_identity_observed_at'][symbol]
                    if symbol in config.get('crypto_identity_evidence',{}):task['asset_identity_evidence_sha256']=config['crypto_identity_evidence'][symbol]['sha256']
                if lane=='crypto1h':
                    policy=lifecycle.lifecycle_policy(task)
                    if policy['source_url']:
                        task['lifecycle_policy_version']=lifecycle.POLICY_VERSION
                    if not policy['should_fetch']:
                        retired+=1
                        checks.append({'symbol':symbol,'status':'RETIRED_NOT_EXPECTED','expected':0,'saved':0,
                          'missing':[],'rejected':[],'retired_at_utc':policy['retired_at_utc'],
                          'retired_not_expected_slots':len(policy['retired_not_expected_slots']),
                          'lifecycle_source':policy['source_url'],'lifecycle_policy_version':lifecycle.POLICY_VERSION})
                        raw.writestr(core.sha(json.dumps(task,sort_keys=True).encode())+'/task.json',json.dumps(task))
                        continue
                key=core.sha(json.dumps(task,sort_keys=True).encode())
                if key in state['completed'] and not qualify:already+=1;continue
                try:
                    bars,q=archive_task(task,raw,http,stock_capture_policy)
                    if 'asset_id' in task:
                        for row in bars:row.update(asset_id=task['asset_id'],asset_classification_available_at=config.get('crypto_identity_observed_at',{}).get(symbol,config.get('universe_source_observed_at',config['observed_at'])),
                            asset_classification='DATED_MANUAL_IDENTITY_EVIDENCE_NOT_HISTORICAL_PIT_CERTIFIED' if task['asset_classification_version']==3 else 'CURRENT_PUBLIC_METADATA_CRYPTO_V2_NOT_PIT_CERTIFIED')
                        if 'asset_identity_evidence_sha256' in task:
                            for row in bars:row['asset_identity_evidence_sha256']=task['asset_identity_evidence_sha256']
                    if q['status']=='COMPLETE':
                        rows+=bars;selected.append(symbol);new.append(key)
                    else:rows+=bars
                    checks.append({'symbol':symbol,**q})
                except core.BudgetStop as exc:
                    reason=str(exc) if str(exc) in {'HTTP_451_REGION_BLOCK','HTTP_429_RETRIES_EXHAUSTED','HTTP_429_RETRY_AFTER_EXCEEDS_BOUND','REQUEST_BUDGET_EXHAUSTED','BYTE_BUDGET_EXHAUSTED','RESPONSE_BYTES_EXCEEDED'} else 'SOURCE_OR_BUDGET_STOP'
                    checks.append({'symbol':symbol,'status':'DEFERRED_SOURCE_OR_BUDGET','reason':reason});break
                except (core.SourceError,ValueError,KeyError,zipfile.BadZipFile) as exc:
                    checks.append({'symbol':symbol,'status':'SOURCE_FAILED','reason':str(exc) if isinstance(exc,core.SourceError) else type(exc).__name__})
                if qualify and len(selected)>=300:break
                if len(checks)%25==0:print(json.dumps({'progress_attempted':len(checks),'complete_symbols':len(selected)}),flush=True)
        if rows:pd.DataFrame(rows).to_parquet(out/'bars.parquet',index=False,compression='zstd')
        complete=(len(selected)>=300) if qualify else (len(new)+already+retired==len(symbols))
        q={'schema_version':1,'lane':lane,'created_at':core.now().isoformat(),'date':date,
           'status':'COMPLETE' if complete else 'PARTIAL','qualified_symbols':len(selected),
           'already_complete':already,'attempted':len(checks),'configured_symbols':len(symbols),
           'bars':len(rows),'checks':checks,'requests':http.used_calls,'bytes':http.used_bytes,
           'qualification':'RESEARCH_ONLY_UNVERIFIED','independent_crosscheck':'PENDING',
           'selection':'first300 complete recent hourly archive symbols, alphabetical candidates; not marketcap ranking or historical PIT' if qualify else 'fixed private universe',
           'option_rows':0,'expected_bars_per_crypto_full_utc_day':24}
        q['retired_not_expected_symbols']=retired
        q['active_symbols_at_day_end']=sum(bool(lifecycle.lifecycle_policy({'kind':'crypto','market':'spot','symbol':s,'date':date,'interval':'1h'})['expected_slots'] and
            lifecycle.lifecycle_policy({'kind':'crypto','market':'spot','symbol':s,'date':date,'interval':'1h'})['status']=='FULL_DAY_EXPECTED') for s in symbols) if lane=='crypto1h' else None
        q['http_retry_events']=getattr(http,'retry_events',[])
        if lane=='stocks15m':
            q['capture']=stock_capture.summarize_stock_capture(checks,len(symbols),len(symbols)-len(checks)-already,already)
            if not rows and not already:
                q['capture']['capture_status']='CAPTURE_FAILED'
                q['capture']['failure_reason']='NO_ACCEPTED_BARS_IN_SHARD'
        core.save(out/'quality.json',q)
        rng=random.Random(20261026)
        core.save(out/'crosscheck_sample.json',{'status':'PENDING_INDEPENDENT_VENDOR','rows':rng.sample(rows,min(30,len(rows))),'rule':'same instrument, venue, denomination and time; checksum is not price accuracy'})
        core.seal(out);core.verify(out)
        state['completed']=sorted(set(state['completed'])|set(new));core.save(state_path,state)
        if qualify:
            core.save(root/'selected_universe.json',{'schema_version':1,'stocks':config.get('stocks',[]),'crypto':selected,
              'observed_at':core.now().isoformat(),'qualification':q['selection']})
        core.import_batch(out,root/'catalog.sqlite')
        print(json.dumps({k:q[k] for k in ('status','lane','qualified_symbols','already_complete','attempted','bars','requests')}),flush=True)
        return out,q

def option_rows(metadata,summaries,observed):
    instruments={r['instrument_name']:r for r in metadata if r.get('kind')=='option' and r.get('is_active')}
    rows=[];seen=set();unknown=[]
    for item in summaries:
        name=item['instrument_name']
        if name in seen:continue
        seen.add(name);inst=instruments.get(name)
        if inst is None:unknown.append(name);continue
        if inst['expiration_timestamp']/1000<=dt.datetime.fromisoformat(observed).timestamp():continue
        bid,ask=item.get('bid_price'),item.get('ask_price');flags=[]
        if bid is None or ask is None:flags.append('ONE_SIDED_OR_EMPTY_BOOK')
        elif not finite(bid) or not finite(ask) or bid<0 or ask<bid:flags.append('INVALID_BBO')
        elif bid==0:flags.append('ZERO_BID')
        stamp=item.get('creation_timestamp')
        if not finite(stamp) or stamp/1000>dt.datetime.fromisoformat(observed).timestamp()+5:flags.append('SUMMARY_CLOCK_INVALID')
        rows.append({'source':'Deribit/public','instrument':name,'observed_at':observed,
          'summary_timestamp_ms':stamp,'summary_clock_is_quote_clock':False,
          'underlying_asset':inst['base_currency'],'quote_currency':inst['quote_currency'],
          'settlement_currency':inst.get('settlement_currency'),'premium_currency':inst.get('settlement_currency'),
          'strike_currency':inst.get('counter_currency'),'instrument_type':inst.get('instrument_type'),
          'strike':inst['strike'],
          'expiry_timestamp_ms':inst['expiration_timestamp'],'option_type':inst['option_type'],
          'contract_size':inst.get('contract_size'),'bid':bid,'ask':ask,
          'underlying_price':item.get('underlying_price'),'underlying_index':item.get('underlying_index'),
          'underlying_match':'SAME_SUMMARY_NOT_SYNCHRONOUS_EXECUTABLE_SPOT',
          'mark_price':item.get('mark_price'),'mark_iv_vendor_percent':item.get('mark_iv'),
          'volume_24h':item.get('volume'),'open_interest':item.get('open_interest'),
          'greeks':None,'bid_ask_size':None,'flags':flags,'executable_quote':False,
          'pit_status':'OBSERVED_FORWARD_NOT_HISTORICAL_PIT_CERTIFIED'})
    return rows,unknown

def collect_options(root):
    root.mkdir(parents=True,exist_ok=True);http=core.HTTP(15,20000000)
    with (root/'.writer.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);out=new_batch(root)
        cache=root/'instrument_cache.json'
        raw_responses={}
        def stash(name,blob):
            raw_responses[name]=blob
            with zipfile.ZipFile(out/'raw_bundle.zip','a',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
                archive.writestr(name,blob)
            core.save(out/'raw_response_manifest.json',{n:core.sha(v) for n,v in raw_responses.items()})
        def instruments(name):
            # File timestamps can describe transport, not metadata observation.
            # Always query this snapshot; the cache remains an output for compatibility.
            blob=http.get('https://www.deribit.com/api/v2/public/get_instruments?currency=any&kind=option&expired=false')
            stash(name,blob);obj=json.loads(blob)
            if not isinstance(obj,dict) or 'error' in obj or not isinstance(obj.get('result'),list):raise core.SourceError('INSTRUMENT_SCHEMA')
            metadata=obj['result']
            if any(not isinstance(r,dict) for r in metadata):raise core.SourceError('INSTRUMENT_SCHEMA')
            active=[r for r in metadata if r.get('kind')=='option' and r.get('is_active')]
            names=set()
            for row in active:
                name=row.get('instrument_name')
                if (not isinstance(name,str) or not name or name in names or
                    not finite(row.get('expiration_timestamp')) or row['expiration_timestamp']<=0 or
                    not finite(row.get('strike')) or row['strike']<=0 or row.get('option_type') not in ('call','put') or
                    any(not isinstance(row.get(key),str) or not row[key] for key in ('base_currency','quote_currency','settlement_currency'))):
                    raise core.SourceError('INSTRUMENT_SCHEMA')
                names.add(name)
            currencies=sorted({r['settlement_currency'] for r in active})
            if not 1<=len(currencies)<=10:raise ValueError('unexpected settlement universe')
            if any(not isinstance(currency,str) or not __import__('re').fullmatch('[A-Z0-9]{2,15}',currency) for currency in currencies):raise ValueError('currency')
            cache.write_bytes(blob)
            return metadata,currencies
        summaries=[];counts={}
        def fetch_summaries(currencies):
            # At most ten distinct currency reads plus two metadata reads (<=12).
            if len(set(counts)|set(currencies))>10:raise ValueError('unexpected settlement universe')
            for currency in currencies:
                if currency in counts:continue
                blob=http.get('https://www.deribit.com/api/v2/public/get_book_summary_by_currency?'+urllib.parse.urlencode({'currency':currency,'kind':'option'}))
                stash(currency+'_summary.json',blob);obj=json.loads(blob)
                if not isinstance(obj,dict) or 'error' in obj or not isinstance(obj.get('result'),list):raise core.SourceError('SUMMARY_SCHEMA')
                summaries.extend(obj['result']);counts[currency]=len(obj['result'])
        metadata,currencies=instruments('instruments.json')
        fetch_summaries(currencies)
        observed=core.now().isoformat();rows,unknown=option_rows(metadata,summaries,observed)
        initial_unknown=len(unknown);refreshed=bool(unknown)
        if refreshed:
            # Listings can change between the first metadata and summary reads.
            # Reconcile once, retaining both raw metadata observations as evidence.
            metadata,currencies=instruments('instruments_refresh.json')
            fetch_summaries(currencies)
            observed=core.now().isoformat();rows,unknown=option_rows(metadata,summaries,observed)
        if not rows:raise core.SourceError('NO_OPTION_ROWS')
        with gzip.open(out/'options.jsonl.gz','wt') as f:
            for r in rows:f.write(json.dumps(r,allow_nan=False)+'\n')
        q={'schema_version':1,'lane':'crypto_options','created_at':observed,'status':'COMPLETE' if len(rows)>=300 and not unknown else 'PARTIAL',
          'contracts':len(rows),'underlying_assets':sorted({r['underlying_asset'] for r in rows}),
          'settlement_currencies':sorted(counts),'summary_counts':counts,'unknown_metadata':unknown,
          'metadata_policy':'FRESH_EACH_SNAPSHOT_REFRESH_ON_UNKNOWN_ONCE_NO_CACHE_FALLBACK',
          'metadata_refreshed':refreshed,'initial_unknown_metadata_count':initial_unknown,
          'bbo_flagged':sum(bool(r['flags']) for r in rows),'greek_rows':0,
          'qualification':'CURRENT_SUMMARY_NOT_HOURLY_HISTORY_NOT_EXECUTABLE_QUOTES; MARK_IV_NOT_GREEKS; 300 contracts is not300 coins',
          'independent_crosscheck':'PENDING','requests':http.used_calls}
        core.save(out/'quality.json',q);core.seal(out);core.verify(out);core.import_batch(out,root/'catalog.sqlite')
        # No instrument IDs, ranking rows or error payloads in public logs.
        print(json.dumps({k:q[k] for k in ('status','lane','contracts','underlying_assets','greek_rows','requests')}),flush=True);return out,q

def transfer(root,prefix,mode):
    import pipeline as p
    import subprocess
    if prefix not in {'crypto1h','crypto_options'} and not __import__('re').fullmatch('stocks15m/part[0-9]{2}',prefix):raise ValueError('prefix')
    target=p.remote(local=True)+'broad/'+prefix+'/'
    with (root/'.writer.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if mode=='publish':
            for batch in sorted((root/'batches').iterdir()):
                core.verify(batch);dest=target+'batches/'+batch.name
                p.rclone('copy',batch,dest,'--exclude','COMMITTED.json','--immutable')
                back=root/'readback'/batch.name;p.rclone('copy',dest,back)
                (back/'COMMITTED.json').write_bytes((batch/'COMMITTED.json').read_bytes());core.verify(back)
                p.rclone('copyto',batch/'COMMITTED.json',dest+'/COMMITTED.json','--immutable')
                p.rclone('copyto',dest+'/COMMITTED.json',back/'COMMITTED.json');core.verify(back)
            for name in ('checkpoint.json','instrument_cache.json'):
                if (root/name).exists():
                    p.rclone('copyto',root/name,target+name);p.rclone('copyto',target+name,root/(name+'.readback'))
                    if core.sha((root/name).read_bytes())!=core.sha((root/(name+'.readback')).read_bytes()):raise ValueError('state readback')
        else:
            p.rclone('copy',target+'batches',root/'batches','--immutable')
            imported=incomplete=0
            for batch in sorted((root/'batches').iterdir()):
                if not (batch/'COMMITTED.json').exists():incomplete+=1;continue
                imported+=core.import_batch(batch,root/'catalog.sqlite')
            core.save(root/'SYNC_STATUS.json',{'checked_at':core.now().isoformat(),'imported':imported,'incomplete':incomplete,'lane':prefix})

def run_exit_code(quality,stock_capture_policy='strict'):
    if stock_capture_policy not in ('strict','observed-source'):raise ValueError('stock capture policy')
    if quality.get('lane')=='stocks15m' and stock_capture_policy=='observed-source':
        capture=quality.get('capture',{})
        bars,already=quality.get('bars',0),quality.get('already_complete',0)
        usable=type(bars) is int and type(already) is int and bars>=0 and already>=0 and (bars>0 or already>0)
        coherent=capture.get('data_status')==quality.get('status') and capture.get('configured_symbols')==quality.get('configured_symbols')
        return 0 if usable and coherent and capture.get('capture_status') in ('CAPTURED_COMPLETE','CAPTURED_WITH_DATA_GAPS') else 2
    return 0 if quality.get('status')=='COMPLETE' else 2

def report_cloud_quality(quality,stock_capture_policy):
    result={'status':quality['status'],'lane':quality['lane'],'remote_hash_readback':'PASS'}
    if quality['lane']=='stocks15m':
        result.update(data_status=quality['status'],capture_status=quality['capture']['capture_status'],
            capture_policy=stock_capture_policy,bars=quality['bars'],
            complete_symbol_windows=quality['qualified_symbols']+quality['already_complete'],
            configured_symbols=quality['configured_symbols'],unattempted=quality['capture']['unattempted'],
            missing_slots=quality['capture']['missing_slots'],no_valid_bars=quality['capture']['no_valid_bars'],
            rejected_rows=quality['capture']['rejected_rows'],null_ohlcv_rows=quality['capture']['rejection_counts'].get('NULL_OHLCV',0),
            unaligned_timestamps_excluded=quality['capture']['rejection_counts'].get('UNALIGNED_SOURCE_TIMESTAMP_EXCLUDED',0),
            empty_history_response_count=quality['capture']['empty_history_response_count'])
        if quality['status']!='COMPLETE':
            print('::warning title=DATA_PARTIAL::Stock observations saved, but requested price coverage is incomplete. No null bars were filled; this is not a full-universe price/PIT certification.',flush=True)
        summary=os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary,'a') as stream:
                stream.write('## Stock source capture and data coverage\n\n')
                stream.write('**DATA '+quality['status']+'** — **'+quality['capture']['capture_status']+'**\n\n')
                stream.write('Capture success does not mean complete or independently verified price data.\n\n')
                for key in ('bars','complete_symbol_windows','configured_symbols','unattempted','missing_slots','rejected_rows','null_ohlcv_rows','unaligned_timestamps_excluded','empty_history_response_count','no_valid_bars'):
                    stream.write('- '+key+': '+str(result[key])+'\n')
                stream.write('\nMissing and rejected counts overlap; do not add them. Null values do not establish no trading. Raw observations and precise per-symbol quality are retained privately. True source/schema/price faults and unattempted tasks still fail; partial windows never enter the complete checkpoint.\n')
    print(json.dumps(result),flush=True)

def cloud(lane,shard=0,shards=24,date=None,stock_capture_policy='strict'):
    import pipeline as p
    import subprocess
    if lane not in ('crypto1h','stocks15m','crypto_options') or not 0<=shard<shards<=64:raise ValueError('scope')
    prefix='stocks15m/part%02d'%shard if lane=='stocks15m' else lane
    remote=p.remote()
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);p.rclone('mkdir',remote+'broad/'+prefix)
        listing=subprocess.check_output(['rclone','lsf',remote+'broad/'+prefix],timeout=120).decode().splitlines()
        for name in ('checkpoint.json','instrument_cache.json'):
            if name in listing:p.rclone('copyto',remote+'broad/'+prefix+'/'+name,root/name)
        try:
            if lane=='crypto_options':out,q=collect_options(root)
            else:
                p.rclone('copyto',remote+'config/broad_universe.json',root/'config.json')
                config=json.loads((root/'config.json').read_text())
                if lane=='crypto1h' and len(config.get('crypto',[]))<300:raise ValueError('requires at least300 qualified crypto')
                if lane=='crypto1h':
                    p.rclone('copyto',remote+'config/broad_crypto_asset_ids.json',root/'asset_ids.json')
                    identities=json.loads((root/'asset_ids.json').read_text())
                    config['crypto_asset_ids']=identities['asset_ids']
                    config['universe_source_observed_at']=identities.get('source_universe_observed_at',config['observed_at'])
                    config['crypto_identity_observed_at']=identities.get('identity_observed_at',{})
                    config['crypto_identity_evidence']=identities.get('identity_evidence',{})
                    validate(config)
                    if identities.get('classification_version')!=2:raise ValueError('native crypto classification required')
                # Revisit yesterday and the preceding UTC day for one-day catch-up.
                dates=[date] if date else [str(core.now().date()-dt.timedelta(days=age)) for age in ((2,1) if lane=='crypto1h' else (1,))]
                outcomes=[]
                for day in dates:
                    if lane=='crypto1h':
                        active=sum(lifecycle.lifecycle_policy({'kind':'crypto','market':'spot','symbol':s,'date':day,'interval':'1h'})['status']=='FULL_DAY_EXPECTED' for s in config['crypto'])
                        if active<300:raise ValueError('at least300 active crypto targets required after known retirement')
                    out,q=collect(config,root,lane,date=day,shard=shard,shards=shards,stock_capture_policy=stock_capture_policy)
                    outcomes.append(q['status'])
                q['status']='COMPLETE' if all(x=='COMPLETE' for x in outcomes) else 'PARTIAL'
        except Exception as exc:
            # Preserve received bytes privately even when the source fails mid-run.
            for batch in (root/'batches').glob('*') if (root/'batches').exists() else []:
                if not (batch/'COMMITTED.json').exists():
                    core.save(batch/'quality.json',{'schema_version':1,'lane':lane,'status':'PARTIAL','error_class':type(exc).__name__,'research_gate':'UNVERIFIED'})
                    core.seal(batch);core.verify(batch)
            if (root/'batches').exists():transfer(root,prefix,'publish')
            raise core.SourceError('COLLECTION_FAILED_PRIVATE_EVIDENCE_PRESERVED') from None
        transfer(root,prefix,'publish')
        report_cloud_quality(q,stock_capture_policy)
        return q

if __name__=='__main__':
    os.umask(0o077);ap=argparse.ArgumentParser();ap.add_argument('mode',choices=['qualify-crypto','stocks','crypto','options','cloud','sync'])
    ap.add_argument('--root',type=Path);ap.add_argument('--lane',choices=['stocks15m','crypto1h','crypto_options']);ap.add_argument('--config',type=Path);ap.add_argument('--date');ap.add_argument('--shard',type=int,default=0);ap.add_argument('--shards',type=int,default=24)
    ap.add_argument('--stock-capture-policy',choices=['strict','observed-source'],default='strict')
    a=ap.parse_args()
    if a.mode=='cloud':
        q=cloud(a.lane,a.shard,a.shards,a.date or None,a.stock_capture_policy);raise SystemExit(run_exit_code(q,a.stock_capture_policy))
    if a.root is None or not a.root.is_absolute():raise ValueError('private absolute root')
    if a.mode=='sync':
        a.root.mkdir(parents=True,exist_ok=True);prefix='stocks15m/part%02d'%a.shard if a.lane=='stocks15m' else a.lane
        transfer(a.root,prefix,'sync');print('Private broad sync completed; inspect SYNC_STATUS for incomplete batches');raise SystemExit(0)
    if a.mode=='options':out,q=collect_options(a.root)
    else:out,q=collect(json.loads(a.config.read_text()),a.root,'stocks15m' if a.mode=='stocks' else 'crypto1h',a.date,a.shard,a.shards,a.mode=='qualify-crypto')
    raise SystemExit(0 if q['status']=='COMPLETE' else 2)
