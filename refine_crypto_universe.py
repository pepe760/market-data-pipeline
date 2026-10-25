"""Correct asset-type classification; preserve old candidates and reuse raw bytes."""
import argparse,json,re,zipfile
from pathlib import Path
import intraday15m as c
import broad_collect as b
import expand_universe as u

NON_CRYPTO=re.compile(r'tokenized|bstocks|xstock|\bstock\b|\betf\b|treasury|gold|silver|nasdaq|s&p',re.I)
PRIORITY=['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','LINKUSDT','LTCUSDT','AVAXUSDT','HYPEUSDT','TRXUSDT']

def classify(markets,symbols):
    # Current public symbol/name match, not an exchange-contract identity proof.
    ranked=sorted(markets,key=lambda x:(x.get('market_cap_rank') or 10**9,x['id']))
    by={}
    for row in ranked:
        if not NON_CRYPTO.search(row['name']+' '+row['id']):by.setdefault(row['symbol'].upper(),row)
    found={}
    for symbol in symbols:
        if not u.eligible_crypto(symbol) and symbol!='JUPUSDT':continue
        base=symbol[:-4];row=by.get(base)
        if row:found[symbol]={'asset_id':row['id'],'name':row['name'],'rank':row.get('market_cap_rank'),
                             'confidence':'CURRENT_PUBLIC_SYMBOL_NAME_MATCH_NOT_EXCHANGE_CONTRACT_CERTIFIED'}
    missing=set(PRIORITY)-set(found)
    if missing:raise ValueError('Required major/option-underlying pair missing: '+','.join(sorted(missing)))
    ordered=PRIORITY+sorted(set(found)-set(PRIORITY),key=lambda s:(found[s]['rank'] or 10**9,s))
    return ordered,found

def refine(root,seed,catalog,directory,date):
    import pandas as pd
    root.mkdir(parents=True,exist_ok=True)
    if (root/'batches').exists():raise ValueError('new immutable refinement root required')
    markets=[];sources={}
    for p in sorted(catalog.glob('coingecko_markets_*.json')):
        markets+=json.loads(p.read_text());sources[p.name]=c.sha(p.read_bytes())
    symbols=set()
    for p in directory.glob('crypto_directory_*.xml'):
        prefixes,_,_=u.archive_prefixes(p.read_bytes())
        symbols.update(x.rstrip('/').rsplit('/',1)[-1] for x in prefixes)
    ordered,identities=classify(markets,symbols)
    seed_batch=next((seed/'batches').iterdir());c.verify(seed_batch)
    old_frame=pd.read_parquet(seed_batch/'bars.parquet')
    seed_config=json.loads((seed/'selected_universe.json').read_text())
    checkpoint=root/'checkpoint.json'
    state=json.loads(checkpoint.read_text()) if checkpoint.exists() else {'completed':[]}
    out=b.new_batch(root);http=c.HTTP(1000,128000000);checks=[];rows=[];selected=[];used_ids=set();new_ids=[]
    observed=c.now().isoformat();reused=0
    with zipfile.ZipFile(seed_batch/'raw_bundle.zip') as old,zipfile.ZipFile(out/'raw_bundle.zip','w',compression=zipfile.ZIP_DEFLATED) as raw:
        for symbol in ordered:
            identity=identities[symbol]
            if identity['asset_id'] in used_ids:continue
            task={'kind':'crypto','market':'spot','symbol':symbol,'date':date,'interval':'1h'}
            key=c.sha(json.dumps(task,sort_keys=True).encode())
            old_rows=old_frame[old_frame.symbol==symbol]
            try:
                if len(old_rows)==24 and key+'/raw.zip' in old.namelist():
                    blob=old.read(key+'/raw.zip');checksum=old.read(key+'/raw.CHECKSUM')
                    if c.sha(blob)!=checksum.decode().split()[0]:raise ValueError('seed checksum')
                    parsed,q=c.parse_crypto(blob,task,old_rows.available_at.iloc[0])
                    assert parsed==old_rows.to_dict('records')
                    for suffix in ('task.json','raw.zip','raw.CHECKSUM'):raw.writestr(key+'/'+suffix,old.read(key+'/'+suffix))
                    q['status']='COMPLETE';reused+=1
                else:parsed,q=b.archive_task(task,raw,http)
                checks.append({'symbol':symbol,**q})
                if q['status']!='COMPLETE':continue
                for row in parsed:row.update(asset_id=identity['asset_id'],asset_name=identity['name'],asset_classification_available_at=observed,
                  asset_classification='PUBLIC_METADATA_CLASSIFIED_CRYPTO_TOKEN_NOT_PIT_CERTIFIED')
                rows+=parsed;selected.append(symbol);used_ids.add(identity['asset_id']);new_ids.append(key)
            except c.BudgetStop:break
            except (c.SourceError,ValueError,KeyError,zipfile.BadZipFile) as exc:
                checks.append({'symbol':symbol,'status':'SOURCE_FAILED','error_class':type(exc).__name__})
            if len(selected)>=300:break
            if len(checks)%25==0:print(json.dumps({'attempted':len(checks),'classified_complete':len(selected),'reused':reused}),flush=True)
    if set(PRIORITY)-set(selected):raise ValueError('priority pairs not fully qualified')
    if rows:pd.DataFrame(rows).to_parquet(out/'bars.parquet',index=False,compression='zstd')
    config={'schema_version':1,'stocks':seed_config['stocks'],'crypto':selected,'observed_at':observed,
            'qualification':'V2 public metadata classified crypto-token universe; current rank ordering for collection only, not PIT/survivor-free history or exchange-contract identity certification'}
    c.save(out/'config.json',config);c.save(root/'selected_universe.json',config)
    c.save(out/'asset_classification.json',{'observed_at':observed,'metadata_hashes':sources,
       'identities':{s:identities[s] for s in selected},'unique_asset_ids':len(used_ids),
       'seed_batch':seed_batch.name,'seed_manifest_sha256':c.sha((seed_batch/'manifest.json').read_bytes()),
       'old_cohort_limitation':'Contained tokenized equities and unclassified names. Preserve raw history but do not count old full cohort as300 classified crypto.'})
    q={'schema_version':1,'lane':'crypto1h','status':'COMPLETE' if len(selected)>=300 else 'PARTIAL',
       'qualified_symbols':len(selected),'unique_public_asset_ids':len(used_ids),'bars':len(rows),'date':date,
       'checks':checks,'reused_verified_symbols':reused,'requests':http.used_calls,
       'classification_gate':'CURRENT_PUBLIC_METADATA_SYMBOL_MATCH_WITH_LIMITATIONS','independent_price_crosscheck':'PENDING'}
    c.save(out/'quality.json',q);c.seal(out);c.verify(out)
    state['completed']=sorted(set(state['completed'])|set(new_ids));c.save(checkpoint,state)
    c.import_batch(out,root/'catalog.sqlite')
    print(json.dumps({k:q[k] for k in ['status','qualified_symbols','unique_public_asset_ids','bars','reused_verified_symbols','requests']}),flush=True)
    return q

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    for name in ['root','seed','catalog','directory']:ap.add_argument('--'+name,required=True,type=Path)
    ap.add_argument('--date',required=True);a=ap.parse_args();q=refine(a.root,a.seed,a.catalog,a.directory,a.date)
    raise SystemExit(0 if q['status']=='COMPLETE' else 2)
