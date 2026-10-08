import datetime as dt
import json
import unittest
import rsr_option_universe as r

class RankingTests(unittest.TestCase):
    def fixture(self):
        rows=[{'ticker':'A'+str(i),'RS_Rank':100-i} for i in range(30)]
        return 'const BUILD_TS_HK = "2026-10-08 12:00";\n'+'\n'.join('const '+k+' = '+json.dumps(rows)+';' for k in ('POOL_RSR','POOL_NRSR','POOL_URSR'))
    def test_overlap_keeps_all_group_positions(self):
        p=r.parse_dashboard(self.fixture(),dt.datetime(2026,10,8,5,tzinfo=dt.timezone.utc))
        self.assertEqual(len(p['memberships']),30)
        self.assertEqual(len(p['memberships']['A0']),3)
        self.assertEqual(p['memberships']['A0'][2]['group'],'URSR')
    def test_stale_source_rejected(self):
        with self.assertRaises(ValueError):
            r.parse_dashboard(self.fixture(),dt.datetime(2026,10,14,5,tzinfo=dt.timezone.utc))
    def test_partial_or_duplicate_pool_rejected(self):
        with self.assertRaises(ValueError):
            r.parse_dashboard(self.fixture().replace('"A29"','"A0"'),dt.datetime(2026,10,8,5,tzinfo=dt.timezone.utc))
