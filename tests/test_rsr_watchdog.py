import datetime as dt
import json
import unittest
from unittest.mock import patch
import rsr_watchdog as w

class WatchdogTests(unittest.TestCase):
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
