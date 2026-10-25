import unittest
from broad_local_sync import pending_batches

class Tests(unittest.TestCase):
    def test_only_new_bounded_batch_ids(self):
        names=['20261025T000000Z-%012x/'%i for i in range(150)]
        pending,count=pending_batches(names,{x.rstrip('/') for x in names[:20]})
        self.assertEqual(count,130);self.assertEqual(len(pending),100)
        self.assertFalse(set(pending)&{x.rstrip('/') for x in names[:20]})
    def test_unsafe_or_duplicate_names_rejected(self):
        for names in [['../other'],['20261025T000000Z-000000000001/']*2]:
            with self.assertRaises(ValueError):pending_batches(names,set())

if __name__=='__main__':unittest.main()
