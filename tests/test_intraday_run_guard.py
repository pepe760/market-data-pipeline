import datetime as dt
import unittest
import intraday_run_guard as g

class Tests(unittest.TestCase):
    def setUp(self):self.now=dt.datetime(2026,10,25,5,tzinfo=g.UTC)
    def runrow(self,**kw):
        return dict(dict(id=1,head_branch='main',created_at='2026-10-25T04:30:00Z',
                    event='workflow_dispatch',status='completed',conclusion='success'),**kw)
    def test_sunday_success(self):self.assertEqual(g.decide([self.runrow()],self.now)['status'],'HEALTHY')
    def test_saturday_missing(self):self.assertEqual(g.decide([],self.now-dt.timedelta(days=1))['status'],'DISPATCH_REQUIRED')
    def test_grace(self):self.assertEqual(g.decide([],self.now.replace(hour=3))['status'],'NOT_DUE')
    def test_yesterday_not_fresh(self):
        self.assertEqual(g.decide([self.runrow(created_at='2026-10-24T16:25:00Z')],self.now)['status'],'DISPATCH_REQUIRED')
    def test_other_branch_ignored(self):self.assertEqual(g.decide([self.runrow(head_branch='test')],self.now)['status'],'DISPATCH_REQUIRED')
    def test_active_dedup(self):self.assertEqual(g.decide([self.runrow(status='in_progress',conclusion=None)],self.now)['status'],'RUNNING')
    def test_previous_day_stall(self):
        self.assertEqual(g.decide([self.runrow(status='queued',created_at='2026-10-24T23:00:00Z')],self.now)['status'],'ACTIVE_STALE')
    def test_retry_once(self):
        self.assertEqual(g.decide([self.runrow(conclusion='failure')],self.now)['status'],'DISPATCH_REQUIRED')
    def test_retry_limit(self):
        runs=[self.runrow(id=i,conclusion='failure') for i in [1,2]]
        self.assertEqual(g.decide(runs,self.now)['status'],'RETRY_LIMIT')
    def test_schedule_failure_allows_recovery(self):
        self.assertEqual(g.decide([self.runrow(event='schedule',conclusion='failure')],self.now)['status'],'DISPATCH_REQUIRED')
    def test_future_fails(self):
        with self.assertRaises(ValueError):g.decide([self.runrow(created_at='2026-10-26T00:00:00Z')],self.now)
    def test_dispatch_only_main(self):
        class Fake:
            calls=[]
            def call(self,path,body=None):
                self.calls.append((path,body))
                if path.endswith('/dispatches'):return None
                return {'workflow_runs':[]} if '/runs?' in path else {'state':'active'}
        f=Fake();self.assertEqual(g.check(f,self.now)['status'],'DISPATCHED_PENDING_VERIFICATION')
        self.assertEqual(f.calls[-1][1],{'ref':'main'})
    def test_disabled_no_dispatch(self):
        class Fake:
            def call(self,path,body=None):return {'state':'disabled_manually'}
        self.assertEqual(g.check(Fake(),self.now)['status'],'WORKFLOW_DISABLED')

if __name__=='__main__':unittest.main()
