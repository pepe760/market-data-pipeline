import unittest,json,sqlite3,tempfile
from pathlib import Path
from broad_local_sync import pending_batches,record_identity_policies

class Tests(unittest.TestCase):
    def test_only_new_bounded_batch_ids(self):
        names=['20261025T000000Z-%012x/'%i for i in range(150)]
        pending,count=pending_batches(names,{x.rstrip('/') for x in names[:20]})
        self.assertEqual(count,130);self.assertEqual(len(pending),100)
        self.assertFalse(set(pending)&{x.rstrip('/') for x in names[:20]})
    def test_unsafe_or_duplicate_names_rejected(self):
        for names in [['../other'],['20261025T000000Z-000000000001/']*2]:
            with self.assertRaises(ValueError):pending_batches(names,set())

    def test_identity_metadata_is_append_only_and_does_not_rewrite_batch_quality(self):
        policy={'schema_version':1,'symbol':'EPICUSDT','old_asset_id':'epic-cash','new_asset_id':'epic-chain',
                'observed_at':'2026-10-30T13:30:00+00:00','evidence':[{'url':'https://api.coingecko.com/api/v3/coins/epic-chain','sha256':'a'*64}]}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db=root/'catalog.sqlite';metadata=root/'qualification_overrides.json'
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE batches(id TEXT PRIMARY KEY,quality TEXT)');con.execute('INSERT INTO batches VALUES(?,?)',('old','ORIGINAL_BYTES'))
            metadata.write_text(json.dumps({'asset_identity_policies':[policy]}))
            record_identity_policies(metadata,db);record_identity_policies(metadata,db)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM asset_identity_policies').fetchone()[0],1)
                self.assertEqual(con.execute('SELECT quality FROM batches').fetchone()[0],'ORIGINAL_BYTES')

    def test_wrong_identity_policy_fails_before_catalog_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);metadata=root/'qualification_overrides.json';db=root/'catalog.sqlite'
            metadata.write_text(json.dumps({'asset_identity_policies':[{'symbol':'WRONG'}]}))
            with self.assertRaises(ValueError):record_identity_policies(metadata,db)
            self.assertFalse(db.exists())

if __name__=='__main__':unittest.main()
