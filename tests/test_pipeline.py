import json
import datetime as dt
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import pipeline as p


class PipelineTests(unittest.TestCase):
    def test_provider_identity_mapping(self):
        self.assertEqual(p.yahoo_symbol('BRK.B'),'BRK-B')
        self.assertEqual(p.yahoo_symbol('BF.B'),'BF-B')
        self.assertEqual(p.yahoo_symbol('SATS','2026-06-23'),'SATS')
        self.assertEqual(p.yahoo_symbol('SATS','2026-06-24'),'ECHO')
        self.assertEqual(p.yahoo_symbol('AVB','2026-10-08'),'AVB')
    @unittest.skipUnless(importlib.util.find_spec('exchange_calendars'), 'calendar dependency absent')
    def test_calendar_sessions(self):
        # Independence Day observed holiday and a normal summer/winter close.
        summer = p.closed_sessions(dt.datetime(2026, 7, 4, 3, 17, tzinfo=dt.timezone.utc))
        self.assertEqual(summer[-1], '2026-07-02')
        early = p.closed_sessions(dt.datetime(2026, 10, 2, 20, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(early[-1], '2026-10-01')
        winter = p.closed_sessions(dt.datetime(2026, 12, 4, 22, 47, tzinfo=dt.timezone.utc))
        self.assertEqual(winter[-1], '2026-12-04')

    def fixture(self, root):
        dest = Path(root) / '20261003T000000Z-0123456789ab'
        dest.mkdir()
        row = dict(ticker='TEST', available_at='2026-10-03T00:00:00+00:00', source='synthetic')
        (dest / 'prices.jsonl').write_text(json.dumps(row) + '\n')
        (dest / 'events.jsonl').write_text('')
        p.write_json(dest / 'quality.json', {'status': 'PARTIAL'})
        with patch('pipeline.importlib.metadata.version', return_value='test'):
            p.seal(dest)
        return dest

    def test_hash_tamper(self):
        with tempfile.TemporaryDirectory() as root:
            dest = self.fixture(root)
            p.verify(dest)
            (dest / 'events.jsonl').write_text('tampered')
            with self.assertRaises(ValueError):
                p.verify(dest)

    def test_idempotent_import(self):
        with tempfile.TemporaryDirectory() as root:
            dest = self.fixture(root)
            db = Path(root) / 'test.sqlite'
            self.assertTrue(p.import_batch(dest, db))
            self.assertFalse(p.import_batch(dest, db))
            with sqlite3.connect(db) as c:
                self.assertEqual(c.execute('SELECT count(*) FROM observations').fetchone()[0], 1)

    def test_missing_commit(self):
        with tempfile.TemporaryDirectory() as root:
            dest = self.fixture(root)
            (dest / 'COMMITTED.json').unlink()
            with self.assertRaises(ValueError):
                p.verify(dest)

    def test_path_traversal_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            dest = self.fixture(root)
            value = json.loads((dest / 'manifest.json').read_text())
            value['files']['../secret'] = 'x'
            p.write_json(dest / 'manifest.json', value)
            p.write_json(dest / 'COMMITTED.json', {'manifest_sha256': p.digest(dest / 'manifest.json')})
            with self.assertRaises(ValueError):
                p.verify(dest)

    def test_bad_universe(self):
        for symbols in [['SPY', 'SPY'], ['../secret'], []]:
            with self.assertRaises(ValueError):
                p.universe({'as_of': '2026-10-03', 'prices': symbols, 'events': []})

    def test_price_validation(self):
        row = {'Open': 10, 'High': 11, 'Low': 9, 'Close': 10, 'Adj Close': 9.5, 'Volume': 1}
        self.assertEqual(p.price_row('TEST', '2026-10-02', row, 'now')['price_basis'], 'VENDOR_SPLIT_ADJUSTED_NOT_RAW')
        for key, val in [('High', 8), ('Volume', -1), ('Close', float('nan')), ('Adj Close', 0)]:
            with self.assertRaises(ValueError):
                p.price_row('TEST', '2026-10-02', dict(row, **{key: val}), 'now')

    def test_import_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            dest = self.fixture(root)
            with (dest / 'prices.jsonl').open('a') as f:
                f.write('{}\n')
            with patch('pipeline.importlib.metadata.version', return_value='test'):
                p.seal(dest)
            db = Path(root) / 'test.sqlite'
            with self.assertRaises(ValueError):
                p.import_batch(dest, db)
            with sqlite3.connect(db) as c:
                self.assertEqual(c.execute('SELECT count(*) FROM observations').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
