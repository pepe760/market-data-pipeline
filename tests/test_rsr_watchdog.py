import datetime as dt
import json
import unittest
from unittest.mock import patch
import rsr_watchdog as w

class WatchdogTests(unittest.TestCase):
    def test_data_only_commit_uses_website_revision_with_identical_html(self):
        calls=[]
        def reader(path,token,raw=False):
            calls.append(path)
            if path.startswith('commits?'): return [{'sha':'website-revision'}]
            if path.startswith('contents/'): return b'current html'
            return {'check_runs':[{'name':'Cloudflare Pages','conclusion':'success'}]}
        sha,checks=w.deployment_evidence('data-only-head','current html','secret',reader)
        self.assertEqual(sha,'website-revision')
        self.assertEqual(checks[0]['conclusion'],'success')
        self.assertIn('sha=data-only-head',calls[0])
        self.assertEqual(calls[-1],'commits/website-revision/check-runs')

    def test_different_artifact_cannot_reuse_successful_deployment(self):
        def reader(path,token,raw=False):
            return b'older html' if raw else [{'sha':'old'}]
        with self.assertRaisesRegex(ValueError,'artifact mismatch'):
            w.deployment_evidence('head','new html','secret',reader)

    def fixture(self):
        rows=[dict(ticker='A'+str(i),RS_Rank=100-i) for i in range(30)]
        return 'const BUILD_TS_HK = "2026-10-09 10:00";'+''.join('const '+k+' = '+json.dumps(rows)+';' for k in ('POOL_RSR','POOL_NRSR','POOL_URSR'))
    def assess(self,runs,cf,gate=401):
        with patch.object(w.p,'closed_sessions',return_value=['2026-10-08']):
            return w.assess(dt.datetime(2026,10,9,4,17,tzinfo=dt.timezone.utc),runs,self.fixture(),cf,gate)
    def test_success_does_not_claim_authenticated_live_content(self):
        result=self.assess([dict(id=1,created_at='2026-10-09T01:00:00Z',event='schedule',conclusion='success')],
                           [dict(name='Cloudflare Pages',conclusion='success')])
        self.assertEqual(result['status'],'NORMAL')
        self.assertFalse(result['live_authenticated_content_verified'])
    def test_missing_postclose_run_is_incident(self):
        self.assertIn('BUILD_FAILED_OR_POST_CLOSE_RUN_MISSING',self.assess([],[])['issues'])
    def test_failed_deploy_and_bad_endpoint_are_incidents(self):
        result=self.assess([dict(id=1,created_at='2026-10-09T01:00:00Z',event='schedule',conclusion='success')],[],503)
        self.assertIn('CLOUDFLARE_DEPLOYMENT_NOT_VERIFIED',result['issues'])
        self.assertIn('LIVE_ENDPOINT_UNAVAILABLE',result['issues'])
