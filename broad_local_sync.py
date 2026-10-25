"""Incremental private Drive download only; no data collection or remote writes."""
import argparse,fcntl,json,os,sqlite3,subprocess
from pathlib import Path
import intraday15m as core
import pipeline

def pending_batches(remote,known,limit=100):
    names=[x.rstrip('/') for x in remote]
    if len(names)!=len(set(names)) or any(not core.RUN.fullmatch(x) for x in names):raise ValueError('unsafe remote batch listing')
    pending=sorted(set(names)-set(known))
    return pending[:limit],len(pending)

def sync(root,prefix):
    if prefix not in {'crypto1h','crypto_options'} and not __import__('re').fullmatch('stocks15m/part[0-9]{2}',prefix):raise ValueError('prefix')
    root.mkdir(parents=True,exist_ok=True)
    with (root/'.writer.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db=root/'catalog.sqlite';known=set()
        if db.exists():
            with sqlite3.connect(db) as c:
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='batches'").fetchone():known={x[0] for x in c.execute('SELECT id FROM batches')}
        target=pipeline.remote(local=True)+'broad/'+prefix+'/batches/'
        listing=subprocess.check_output(['rclone','lsf',target,'--dirs-only','--retries','2'],timeout=120).decode().splitlines()
        names,total=pending_batches(listing,known)
        imported=incomplete=0
        for name in names:
            batch=root/'batches'/name
            pipeline.rclone('copy',target+name,batch,'--immutable')
            if not (batch/'COMMITTED.json').exists():incomplete+=1;continue
            core.verify(batch);imported+=core.import_batch(batch,db)
        receipt={'checked_at':core.now().isoformat(),'lane':prefix,'new_hash_verified_imports':imported,
                 'remote_uncommitted':incomplete,'remaining_batches':total-imported,'previously_imported':len(known),
                 'scope':'Only new immutable batches downloaded/hashed. Price/PIT/execution quality not certified.'}
        core.save(root/'SYNC_STATUS.json',receipt);return receipt

if __name__=='__main__':
    os.umask(0o077);ap=argparse.ArgumentParser();ap.add_argument('group',choices=['fast','stocks']);ap.add_argument('--root',type=Path,required=True)
    args=ap.parse_args()
    if not args.root.is_absolute():raise ValueError('absolute private root')
    prefixes=['crypto1h','crypto_options'] if args.group=='fast' else ['stocks15m/part%02d'%i for i in range(24)]
    result=[];failed=[]
    args.root.mkdir(parents=True,exist_ok=True)
    try:
        pipeline.rclone('copyto',pipeline.remote(local=True)+'config/broad_universe.json',args.root/'universe.json')
        from broad_collect import validate
        validate(json.loads((args.root/'universe.json').read_text()))
    except Exception as exc:failed.append({'lane':'universe','error_class':type(exc).__name__})
    for prefix in prefixes:
        try:result.append(sync(args.root/prefix,prefix))
        except Exception as exc:failed.append({'lane':prefix,'error_class':type(exc).__name__})
    core.save(args.root/(args.group+'_SYNC_RECEIPT.json'),{'checked_at':core.now().isoformat(),'lanes':result,'failures':failed})
    print(json.dumps({'lanes_checked':len(result),'new_imports':sum(x['new_hash_verified_imports'] for x in result),'failed_lanes':len(failed)}))
    raise SystemExit(1 if failed else 0)
