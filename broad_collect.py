"""Private broad-universe collector. No subscription, trade or public raw data."""
import argparse,datetime as dt,fcntl,gzip,io,json,math,os,random,shutil,tempfile,urllib.parse,uuid,zipfile
from pathlib import Path
import intraday15m as core

def new_batch(root):
    out=root/'batches'/(core.now().strftime('%Y%m%dT%H%M%SZ-')+uuid.uuid4().hex[:12])
    out.mkdir(parents=True)
    shutil.copyfile(__file__,out/'collector_source.py')
    shutil.copyfile(core.__file__,out/'parser_source.py')
    return out

def finite(value):return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)

def validate(config):
    if config.get('schema_version')!=1 or set(config)-{'schema_version','stocks','crypto','crypto_candidates','crypto_target','observed_at','qualification','raw_directory_complete','no_credentials','crypto_asset_ids'}:
        raise ValueError('config schema')
    for key in ('stocks','crypto','crypto_candidates'):
        xs=config.get(key,[])
        if not isinstance(xs,list) or len(xs)>20000 or len(xs)!=len(set(xs)) or any(not isinstance(x,str) or not core.SYMBOL.fullmatch(x) for x in xs):raise ValueError('universe')
    if 'crypto_asset_ids' in config:
        ids=config['crypto_asset_ids']
        if not isinstance(ids,dict) or set(ids)!=set(config.get('crypto',[])) or any(not isinstance(x,str) or not __import__('re').fullmatch('[a-z0-9-]{1,150}',x) for x in ids.values()) or len(set(ids.values()))!=len(ids):raise ValueError('asset identity mapping')
    return config

def archive_task(task,archive,http):
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp)
        try:return core.fetch_task(task,p,http)
        finally:
            key=core.sha(json.dumps(task,sort_keys=True).encode())
            archive.writestr(key+'/task.json',json.dumps(task))
            for f in p.iterdir():archive.write(f,key+'/'+f.name)

def collect(config,root,lane,date=None,shard=0,shards=24,qualify=False,max_requests=1500):
    import pandas as pd
    validate(config);root.mkdir(parents=True,exist_ok=True)
    if lane not in ('crypto1h','stocks15m'):raise ValueError('lane')
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
        rows=[];checks=[];selected=[];new=[];already=0
        with zipfile.ZipFile(out/'raw_bundle.zip','w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as raw:
            for symbol in symbols:
                task=({'kind':'crypto','market':'spot','symbol':symbol,'date':date,'interval':'1h'} if lane=='crypto1h' else {'kind':'stocks','symbol':symbol,'grid':grid})
                if lane=='crypto1h' and not qualify and 'crypto_asset_ids' in config:
                    task.update(asset_id=config['crypto_asset_ids'][symbol],asset_classification_version=2)
                key=core.sha(json.dumps(task,sort_keys=True).encode())
                if key in state['completed'] and not qualify:already+=1;continue
                try:
                    bars,q=archive_task(task,raw,http)
                    if 'asset_id' in task:
                        for row in bars:row.update(asset_id=task['asset_id'],asset_classification_available_at=config['observed_at'],asset_classification='CURRENT_PUBLIC_METADATA_CRYPTO_V2_NOT_PIT_CERTIFIED')
                    if q['status']=='COMPLETE':
                        rows+=bars;selected.append(symbol);new.append(key)
                    else:rows+=bars
                    checks.append({'symbol':symbol,**q})
                except core.BudgetStop:
                    checks.append({'symbol':symbol,'status':'DEFERRED_SOURCE_OR_BUDGET'});break
                except (core.SourceError,ValueError,KeyError,zipfile.BadZipFile) as exc:
                    checks.append({'symbol':symbol,'status':'SOURCE_FAILED','reason':str(exc) if isinstance(exc,core.SourceError) else type(exc).__name__})
                if qualify and len(selected)>=300:break
                if len(checks)%25==0:print(json.dumps({'progress_attempted':len(checks),'complete_symbols':len(selected)}),flush=True)
        if rows:pd.DataFrame(rows).to_parquet(out/'bars.parquet',index=False,compression='zstd')
        complete=(len(selected)>=300) if qualify else (len(new)+already==len(symbols))
        q={'schema_version':1,'lane':lane,'created_at':core.now().isoformat(),'date':date,
           'status':'COMPLETE' if complete else 'PARTIAL','qualified_symbols':len(selected),
           'already_complete':already,'attempted':len(checks),'configured_symbols':len(symbols),
           'bars':len(rows),'checks':checks,'requests':http.used_calls,'bytes':http.used_bytes,
           'qualification':'RESEARCH_ONLY_UNVERIFIED','independent_crosscheck':'PENDING',
           'selection':'first300 complete recent hourly archive symbols, alphabetical candidates; not marketcap ranking or historical PIT' if qualify else 'fixed private universe',
           'option_rows':0,'expected_bars_per_crypto_full_utc_day':24}
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
        cache_fresh=cache.exists() and core.now().timestamp()-cache.stat().st_mtime<86400
        if cache_fresh:
            b=cache.read_bytes()
        else:
            b=http.get('https://www.deribit.com/api/v2/public/get_instruments?currency=any&kind=option&expired=false')
        raw_responses={}
        def stash(name,blob):
            raw_responses[name]=blob
            with zipfile.ZipFile(out/'raw_bundle.zip','a',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
                archive.writestr(name,blob)
            core.save(out/'raw_response_manifest.json',{n:core.sha(v) for n,v in raw_responses.items()})
        stash('instruments.json',b);obj=json.loads(b)
        if 'error' in obj or not isinstance(obj.get('result'),list):raise core.SourceError('INSTRUMENT_SCHEMA')
        if not cache_fresh:cache.write_bytes(b)
        metadata=obj['result'];currencies=sorted({r['settlement_currency'] for r in metadata if r.get('kind')=='option' and r.get('is_active')})
        if not 1<=len(currencies)<=10:raise ValueError('unexpected settlement universe')
        summaries=[];counts={}
        for currency in currencies:
            if not __import__('re').fullmatch('[A-Z0-9]{2,15}',currency):raise ValueError('currency')
            b=http.get('https://www.deribit.com/api/v2/public/get_book_summary_by_currency?'+urllib.parse.urlencode({'currency':currency,'kind':'option'}))
            stash(currency+'_summary.json',b);d=json.loads(b)
            if 'error' in d or not isinstance(d.get('result'),list):raise core.SourceError('SUMMARY_SCHEMA')
            summaries+=d['result'];counts[currency]=len(d['result'])
        observed=core.now().isoformat();rows,unknown=option_rows(metadata,summaries,observed)
        if not rows:raise core.SourceError('NO_OPTION_ROWS')
        with gzip.open(out/'options.jsonl.gz','wt') as f:
            for r in rows:f.write(json.dumps(r,allow_nan=False)+'\n')
        q={'schema_version':1,'lane':'crypto_options','created_at':observed,'status':'COMPLETE' if len(rows)>=300 and not unknown else 'PARTIAL',
          'contracts':len(rows),'underlying_assets':sorted({r['underlying_asset'] for r in rows}),
          'settlement_currencies':currencies,'summary_counts':counts,'unknown_metadata':unknown,
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

def cloud(lane,shard=0,shards=24,date=None):
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
                    validate(config)
                    if identities.get('classification_version')!=2:raise ValueError('native crypto classification required')
                # Revisit yesterday and the preceding UTC day for one-day catch-up.
                dates=[date] if date else [str(core.now().date()-dt.timedelta(days=age)) for age in ((2,1) if lane=='crypto1h' else (1,))]
                outcomes=[]
                for day in dates:
                    out,q=collect(config,root,lane,date=day,shard=shard,shards=shards)
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
        print(json.dumps({'status':q['status'],'lane':lane,'remote_hash_readback':'PASS'}))
        return q

if __name__=='__main__':
    os.umask(0o077);ap=argparse.ArgumentParser();ap.add_argument('mode',choices=['qualify-crypto','stocks','crypto','options','cloud','sync'])
    ap.add_argument('--root',type=Path);ap.add_argument('--lane',choices=['stocks15m','crypto1h','crypto_options']);ap.add_argument('--config',type=Path);ap.add_argument('--date');ap.add_argument('--shard',type=int,default=0);ap.add_argument('--shards',type=int,default=24)
    a=ap.parse_args()
    if a.mode=='cloud':
        q=cloud(a.lane,a.shard,a.shards,a.date or None);raise SystemExit(0 if q['status']=='COMPLETE' else 2)
    if a.root is None or not a.root.is_absolute():raise ValueError('private absolute root')
    if a.mode=='sync':
        a.root.mkdir(parents=True,exist_ok=True);prefix='stocks15m/part%02d'%a.shard if a.lane=='stocks15m' else a.lane
        transfer(a.root,prefix,'sync');print('Private broad sync completed; inspect SYNC_STATUS for incomplete batches');raise SystemExit(0)
    if a.mode=='options':out,q=collect_options(a.root)
    else:out,q=collect(json.loads(a.config.read_text()),a.root,'stocks15m' if a.mode=='stocks' else 'crypto1h',a.date,a.shard,a.shards,a.mode=='qualify-crypto')
    raise SystemExit(0 if q['status']=='COMPLETE' else 2)
