"""Bounded same-repo Actions liveness guard; no market/Drive credentials."""
import datetime as dt
import json
import os
from pathlib import Path
import urllib.request

UTC=dt.timezone.utc
REPO='pepe760/market-data-pipeline'
WORKFLOW='intraday15m.yml'
ACTIVE={'queued','in_progress','waiting','pending','requested'}

def timestamp(value):
    return dt.datetime.fromisoformat(value.replace('Z','+00:00'))

def decide(runs,now):
    if now.tzinfo is None:raise ValueError('UTC clock required')
    now=now.astimezone(UTC)
    due=now.replace(hour=2,minute=19,second=0,microsecond=0)
    if now<due+dt.timedelta(hours=2):return {'status':'NOT_DUE'}
    valid=[]
    for run in runs:
        if run.get('head_branch')!='main':continue
        created=timestamp(run['created_at'])
        if created>now:raise ValueError('future run clock')
        valid.append((run,created))
    current=[r for r,t in valid if t>=due]
    # Do not overlap ANY active collector, including a stalled previous-day run.
    active=[(r,t) for r,t in valid if r.get('status') in ACTIVE]
    if active:
        stale=any(now-t>dt.timedelta(minutes=90) for _,t in active)
        return {'status':'ACTIVE_STALE' if stale else 'RUNNING','run_id':active[0][0]['id']}
    successful=[r for r in current if r.get('status')=='completed' and r.get('conclusion')=='success']
    if successful:return {'status':'HEALTHY','run_id':successful[0]['id']}
    attempts=sum(r.get('event')=='workflow_dispatch' for r in current)
    if attempts>=2:return {'status':'RETRY_LIMIT','attempts':attempts}
    return {'status':'DISPATCH_REQUIRED','prior_attempts':attempts}

class API:
    def __init__(self,token):self.token=token
    def call(self,path,body=None):
        request=urllib.request.Request('https://api.github.com/repos/'+REPO+'/'+path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json',
                     'X-GitHub-Api-Version':'2022-11-28','User-Agent':'private-intraday-run-guard'},
            method='GET' if body is None else 'POST')
        with urllib.request.urlopen(request,timeout=30) as response:
            data=response.read(2_000_001)
            if len(data)>2_000_000:raise ValueError('API response limit')
            return json.loads(data) if data else None

def check(api,now):
    due=now.astimezone(UTC).replace(hour=4,minute=19,second=0,microsecond=0)
    if now<due:return {'status':'NOT_DUE'}
    metadata=api.call('actions/workflows/'+WORKFLOW)
    if metadata.get('state')!='active':return {'status':'WORKFLOW_DISABLED'}
    data=api.call('actions/workflows/'+WORKFLOW+'/runs?branch=main&per_page=100')
    runs=data['workflow_runs']
    # In high volume, refuse to infer missing history from a truncated API page.
    if len(runs)>=100 and timestamp(runs[-1]['created_at'])>=now.replace(hour=2,minute=19,second=0,microsecond=0):
        return {'status':'HISTORY_TRUNCATED'}
    result=decide(runs,now)
    if result['status']=='DISPATCH_REQUIRED':
        api.call('actions/workflows/'+WORKFLOW+'/dispatches',{'ref':'main'})
        result['status']='DISPATCHED_PENDING_VERIFICATION'
    return result

def main():
    try:result=check(API(os.environ['GH_TOKEN']),dt.datetime.now(UTC))
    except Exception as exc:result={'status':'CHECK_FAILED','error_class':type(exc).__name__}
    print(json.dumps(result))
    summary=os.getenv('GITHUB_STEP_SUMMARY')
    if summary:
        with Path(summary).open('a') as f:
            f.write('### Intraday collection liveness\n\n`'+json.dumps(result)+'`\n\n')
            f.write('HEALTHY = today completed collector with configured bars and private hash readback. '
                    'Dispatch accepted is PENDING, not successful collection. No option/Greek or price-accuracy certification. '
                    'Same-platform guard cannot guarantee delivery during a GitHub-wide outage.\n')
    if result['status'] not in {'NOT_DUE','RUNNING','HEALTHY','DISPATCHED_PENDING_VERIFICATION'}:
        raise SystemExit(1)

if __name__=='__main__':main()
