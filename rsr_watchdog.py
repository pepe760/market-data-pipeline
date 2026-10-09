"""Two-week read-only RSR watchdog; evidence and dedup state stay private."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo
import pipeline as p
import rsr_option_universe as rank

START=dt.date(2026,10,9)
END=dt.date(2026,10,23)
REPO=rank.REPO

def api(path,token,raw=False):
    req=urllib.request.Request('https://api.github.com/repos/'+REPO+'/'+path,
        headers={'Authorization':'Bearer '+token,'User-Agent':'RSR-Watchdog',
                 'Accept':'application/vnd.github.raw+json' if raw else 'application/vnd.github+json'})
    with urllib.request.urlopen(req,timeout=45) as response: data=response.read(20_000_001)
    if len(data)>20_000_000: raise ValueError('oversized')
    return data if raw else json.loads(data)

def assess(now,runs,html,checks,gate):
    sessions=p.closed_sessions(now)
    session=sessions[-1]
    expected=dt.datetime.fromisoformat(session+'T21:13:00+00:00')
    relevant=[r for r in runs if dt.datetime.fromisoformat(r['created_at'].replace('Z','+00:00'))>=expected
              and r.get('event') in ('schedule','workflow_dispatch')]
    successful=[r for r in relevant if r.get('conclusion')=='success']
    issues=[]
    if not successful: issues.append('BUILD_FAILED_OR_POST_CLOSE_RUN_MISSING')
    ranks=rank.parse_dashboard(html,now)
    built=dt.datetime.fromisoformat(ranks['source_build_hk']).astimezone(dt.timezone.utc)
    if built < expected: issues.append('DASHBOARD_STALE_FOR_COMPLETED_SESSION')
    cf=[c for c in checks if c['name']=='Cloudflare Pages']
    deploy=bool(cf and cf[-1].get('conclusion')=='success')
    if not deploy: issues.append('CLOUDFLARE_DEPLOYMENT_NOT_VERIFIED')
    if gate not in (200,401,403): issues.append('LIVE_ENDPOINT_UNAVAILABLE')
    waiting=now.astimezone(ZoneInfo('Asia/Hong_Kong')).hour<12
    if waiting:
        issues=[i for i in issues if i not in ('BUILD_FAILED_OR_POST_CLOSE_RUN_MISSING','DASHBOARD_STALE_FOR_COMPLETED_SESSION')]
    return dict(status='ACTION_REQUIRED' if issues else ('WAITING_FOR_CHECK_WINDOW' if waiting else 'NORMAL'),issues=issues,
        market_session=session,build_passed=bool(successful),deployment_passed=deploy,
        live_status=gate,live_authenticated_content_verified=False,
        limitation='Protected live page content not authenticated; repository HTML and deployment check verified.',
        source_build_hk=ranks['source_build_hk'],html_sha256=ranks['html_sha256'],
        top30_counts={k:len(v) for k,v in ranks['groups'].items()},
        latest_run_id=relevant[0]['id'] if relevant else None)

def deployment_evidence(head,html,token,reader=api):
    """Pin file history to head; prove identical HTML before reusing deployment."""
    history=reader('commits?path=index.html&sha='+head+'&per_page=1',token)
    if not history: raise ValueError('Missing website file history')
    website_sha=history[0]['sha']
    deployed_html=reader('contents/index.html?ref='+website_sha,token,True).decode()
    if deployed_html != html: raise ValueError('Website artifact mismatch')
    checks=reader('commits/'+website_sha+'/check-runs',token)['check_runs']
    return website_sha,checks

def run():
    now=p.now(); day=now.astimezone(ZoneInfo('Asia/Hong_Kong')).date()
    if day<START or day>END: return 0
    remote=p.remote()+'monitoring/rsr_pilot_20261009/'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp); state_file=root/'state.json'
        p.rclone('mkdir',remote)
        # Bootstrap or download the verified state; other transfer errors fail.
        listing=__import__('subprocess').run(['rclone','lsf',remote],capture_output=True,timeout=120)
        if listing.returncode: raise RuntimeError('transfer')
        if 'state.json' in listing.stdout.decode().splitlines():
            p.rclone('copyto',remote+'state.json',state_file)
            state=json.loads(state_file.read_text())
        else: state={'days':{},'open_issues':[],'incidents':[]}
        if day==END:
            report=dict(status='PILOT_COMPLETE',days=state['days'],incidents=state['incidents'],
                checked_days=len(state['days']),normal_days=sum(x['status']=='NORMAL' for x in state['days'].values()),
                limitation='Manual-check time, false positives and missed incidents require William review; no automatic strategy changes.')
        elif day.weekday() in (6,0): return 0  # HK Sun/Mon: no new US close
        else:
            token=os.environ['RSR_READ_TOKEN']
            try:
                sha=api('commits/main',token)['sha']
                html=api('contents/index.html?ref='+sha,token,True).decode()
                runs=api('actions/workflows/daily_run.yml/runs?per_page=20',token)['workflow_runs']
                website_sha,checks=deployment_evidence(sha,html,token)
                try:
                    with urllib.request.urlopen('https://usstockrv260420.pages.dev/',timeout=30) as response: gate=response.status
                except urllib.error.HTTPError as exc: gate=exc.code
                report=assess(now,runs,html,checks,gate)
                report['source_commit']=sha
                report['website_commit']=website_sha
                report['deployment_artifact_matches_current_html']=True
            except Exception as exc:
                report=dict(status='ACTION_REQUIRED',issues=['WATCHDOG_CHECK_FAILED'],error_type=type(exc).__name__)
            issues=report.get('issues',[])
            report['new_incident']=issues!=state['open_issues'] and bool(issues)
            report['recovered']=bool(state['open_issues']) and not issues
            if report['new_incident']: state['incidents'].append({'date':str(day),'issues':issues})
            state['open_issues']=issues; state['days'][str(day)]=report
        report.update(checked_at=now.isoformat(),pilot_start=str(START),pilot_end_exclusive=str(END),
                      owner='RSR watchdog / William decision owner')
        report_file=root/'latest.json'; p.write_json(report_file,report)
        history='runs/'+now.strftime('%Y%m%dT%H%M%SZ')+'.json'
        p.rclone('copyto',report_file,remote+history,'--immutable')
        p.rclone('copyto',report_file,remote+'latest.json')
        p.write_json(state_file,state); p.rclone('copyto',state_file,remote+'state.json')
        readback=root/'readback.json'; p.rclone('copyto',remote+'latest.json',readback)
        if p.digest(readback)!=p.digest(report_file): raise ValueError('readback mismatch')
        print('RSR pilot: '+report['status']+'; private receipt verified.')
        return 2 if report.get('new_incident') else 0

if __name__=='__main__':
    try: code=run()
    except Exception as exc:
        print('RSR watchdog unavailable ('+type(exc).__name__+').'); code=1
    raise SystemExit(code)
