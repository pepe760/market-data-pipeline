import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import event_repair as e
import pipeline as p


NOW = dt.datetime(2026, 11, 28, 10, tzinfo=dt.timezone.utc)
SESSIONS = ['2026-11-26', '2026-11-27']
VALUES = dict(Open=10, High=11, Low=9, Close=10, **{'Adj Close': 10, 'Volume': 5})
REVISION = 'a' * 40


class EventRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = dict(as_of='2026-10-03',
            prices=list(p.RETIRED_AFTER) + ['T%03d' % i for i in range(610)], events=sorted(e.M7))
        self.source = self.root / '20261128T090000Z-0123456789ab'
        self.source.mkdir()
        self.checks = []
        rows = []
        for ticker in self.config['prices']:
            retired = ticker in p.RETIRED_AFTER
            check = dict(ticker=ticker, kind='prices', status='NOT_EXPECTED_RETIRED' if retired else 'OK',
                         missing_sessions=[])
            if retired:
                check['retired_after'] = p.RETIRED_AFTER[ticker]
            self.checks.append(check)
            if not retired:
                for day in SESSIONS:
                    row = p.price_row(ticker, day, VALUES, '2026-11-28T08:00:00+00:00')
                    row['provider_symbol'] = p.yahoo_symbol(ticker, day)
                    rows.append(row)
        (self.source / 'prices.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        (self.source / 'events.jsonl').write_text('')
        self.event_checks = [dict(ticker=t, kind=k, status='RATE_LIMIT' if k == 'news' else 'OK')
                             for t in sorted(e.M7) for k in ('calendar', 'news')]
        self.quality = dict(status='PARTIAL', checks=self.checks + self.event_checks,
                            expected_sessions=SESSIONS, universe_as_of=self.config['as_of'],
                            expected_price_tickers=615, event_tickers=7,
                            research_gate='RESEARCH_ONLY_UNVERIFIED', cross_vendor_random_check='PENDING')
        self.reseal()

    def reseal(self):
        p.write_json(self.source / 'quality.json', self.quality)
        with patch.object(p, 'now', return_value=NOW-dt.timedelta(hours=1)), \
                patch.dict('os.environ', {'GITHUB_SHA': REVISION}), \
                patch('pipeline.importlib.metadata.version', return_value='test'):
            p.seal(self.source)

    def validate(self):
        return e.validate_snapshot(self.source, self.config, SESSIONS, NOW)

    def rows(self):
        return [json.loads(line) for line in (self.source / 'prices.jsonl').read_text().splitlines()]

    def save_rows(self, rows):
        (self.source / 'prices.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.reseal()

    def test_full_price_coverage_is_eligible_despite_event_only_partial(self):
        manifest, quality, checks = self.validate()
        self.assertEqual(quality['status'], 'PARTIAL')
        self.assertEqual(len(checks), 615)
        self.assertEqual(p.batch_status(checks), 'COMPLETE')
        self.assertEqual(manifest['code_revision'], REVISION)

    def test_bad_hash_and_missing_commit_rejected(self):
        (self.source / 'prices.jsonl').write_text('tampered')
        with self.assertRaises(ValueError):
            self.validate()
        self.reseal()
        (self.source / 'COMMITTED.json').unlink()
        with self.assertRaises(ValueError):
            self.validate()

    def test_stale_window_or_configuration_rejected(self):
        for field, value in [('expected_sessions', SESSIONS[:1]), ('universe_as_of', '2026-10-02')]:
            with self.subTest(field=field):
                prior = self.quality[field]
                self.quality[field] = value
                self.reseal()
                with self.assertRaises(ValueError):
                    self.validate()
                self.quality[field] = prior

    def test_duplicate_missing_or_unexpected_price_keys_rejected(self):
        rows = self.rows()
        for changed in [rows + rows[:1], rows[1:], [dict(rows[0], ticker='ALIEN')] + rows[1:]]:
            with self.subTest(length=len(changed)):
                self.save_rows(changed)
                with self.assertRaises(ValueError):
                    self.validate()

    def test_invalid_raw_value_identity_source_basis_and_clocks_rejected(self):
        rows = self.rows()
        changes = [dict(values=dict(rows[0]['values'], Close=0)),
                   dict(values=dict(rows[0]['values'], Volume=True)),
                   dict(provider_symbol='ALIEN'), dict(source='other'), dict(price_basis='RAW'),
                   dict(pit_status='CERTIFIED'), dict(available_at='2026-11-28T08:00:00'),
                   dict(available_at='2026-11-28T10:00:00Z'), dict(available_at='2026-11-25T10:00:00Z')]
        for change in changes:
            with self.subTest(change=list(change)):
                self.save_rows([dict(rows[0], **change)] + rows[1:])
                with self.assertRaises((ValueError, TypeError)):
                    self.validate()

    def test_incomplete_or_duplicate_price_checks_rejected(self):
        original = list(self.quality['checks'])
        for checks in [original[1:], [original[0]] + original,
                       [dict(original[0], status='PARTIAL')] + original[1:]]:
            self.quality['checks'] = checks
            self.quality['status'] = p.batch_status(checks)
            self.reseal()
            with self.assertRaises(ValueError):
                self.validate()

    def test_options_schema_rejected(self):
        (self.source / 'options.jsonl').write_text('')
        self.reseal()
        with self.assertRaises(ValueError):
            self.validate()

    def collect_events(self, config, dest):
        self.assertEqual(config['prices'], [])
        self.assertEqual(config['events'], self.config['events'])
        (dest / 'prices.jsonl').write_text('')
        (dest / 'events.jsonl').write_text('{"new":"event"}\n')
        p.write_json(dest / 'quality.json', dict(self.quality, checks=[dict(c, status='OK') for c in self.event_checks],
                                               expected_price_tickers=0, status='COMPLETE'))
        return 'COMPLETE'

    def test_repair_fetches_only_events_preserves_exact_price_bytes_and_checks(self):
        original = (self.source / 'prices.jsonl').read_bytes()
        manifest, quality, checks = self.validate()
        dest = self.root / 'output'
        dest.mkdir()
        with patch.object(p, 'now', return_value=NOW), patch.object(p, 'closed_sessions', return_value=SESSIONS), \
                patch.object(e, 'select_snapshot', return_value=(self.source, manifest, quality, checks, {})), \
                patch.object(p, 'collect', side_effect=self.collect_events) as collect:
            self.assertEqual(e.collector(self.config, dest, 'test:', self.root), 'COMPLETE')
        collect.assert_called_once()
        self.assertEqual((dest / 'prices.jsonl').read_bytes(), original)
        merged = json.loads((dest / 'quality.json').read_text())
        self.assertEqual(merged['checks'][:615], self.checks)
        self.assertEqual(merged['expected_price_tickers'], 615)
        self.assertEqual(merged['price_snapshot']['code_revision'], REVISION)
        self.assertEqual(merged['price_snapshot']['prices_sha256'], p.digest(self.source / 'prices.jsonl'))
        self.assertIn('NOT_CERTIFIED', merged['price_snapshot']['observation_policy'])

    def test_window_change_during_event_fetch_aborts_before_price_copy(self):
        manifest, quality, checks = self.validate()
        dest = self.root / 'output'
        dest.mkdir()
        with patch.object(p, 'now', return_value=NOW), \
                patch.object(p, 'closed_sessions', side_effect=[SESSIONS, SESSIONS[1:]]), \
                patch.object(e, 'select_snapshot', return_value=(self.source, manifest, quality, checks, {})), \
                patch.object(p, 'collect', side_effect=self.collect_events):
            with self.assertRaises(ValueError):
                e.collector(self.config, dest, 'test:', self.root)
        self.assertEqual((dest / 'prices.jsonl').read_bytes(), b'')

    def test_unresolved_new_event_failure_remains_partial(self):
        manifest, quality, checks = self.validate()
        dest = self.root / 'output'
        dest.mkdir()

        def partial_events(config, dest):
            self.collect_events(config, dest)
            value = json.loads((dest / 'quality.json').read_text())
            value['checks'][0]['status'] = 'FAILED'
            value['status'] = 'PARTIAL'
            p.write_json(dest / 'quality.json', value)
            return 'PARTIAL'

        with patch.object(p, 'now', return_value=NOW), patch.object(p, 'closed_sessions', return_value=SESSIONS), \
                patch.object(e, 'select_snapshot', return_value=(self.source, manifest, quality, checks, {})), \
                patch.object(p, 'collect', side_effect=partial_events):
            self.assertEqual(e.collector(self.config, dest, 'test:', self.root), 'PARTIAL')
        self.assertEqual(p.run_result('PARTIAL')[0], 2)

    def test_wrong_config_universe_never_fetches_events(self):
        with patch.object(p, 'collect') as collect:
            with self.assertRaises(ValueError):
                e.collector(dict(self.config, prices=self.config['prices'][:-1]), self.root, 'test:', self.root)
        collect.assert_not_called()

    def test_search_reads_newest_eligible_committed_snapshot(self):
        listing = subprocess.CompletedProcess([], 0, stdout=(self.source.name + '/\n').encode())

        def copy(command, source, dest):
            if command == 'copyto':
                shutil.copyfile(self.source / 'manifest.json', dest)
            else:
                shutil.copytree(self.source, dest, dirs_exist_ok=True)

        with patch.object(p.subprocess, 'run', return_value=listing), patch.object(p, 'rclone', side_effect=copy):
            selected = e.select_snapshot(self.config, 'test:', self.root / 'cache', SESSIONS, NOW)
        self.assertEqual(selected[0].name, self.source.name)
        self.assertEqual(selected[4]['checked_price_batches'], 1)

    def test_search_manifest_and_price_payload_bounds(self):
        listing = subprocess.CompletedProcess([], 0, stdout=''.join(
            '20261128T09%04dZ-%012x/\n' % (i, i) for i in range(40)).encode())
        for schema, limit in [(2, 32), (1, 8)]:
            calls = []

            def copy(command, source, dest):
                calls.append(command)
                if command == 'copyto':
                    Path(dest).write_text(json.dumps({'schema': schema}))

            with self.subTest(schema=schema), patch.object(p.subprocess, 'run', return_value=listing), \
                    patch.object(p, 'rclone', side_effect=copy):
                with self.assertRaises(ValueError):
                    e.select_snapshot(self.config, 'test:', self.root / ('cache'+str(schema)), SESSIONS, NOW)
            self.assertEqual(calls.count('copyto'), limit)
            self.assertEqual(calls.count('copy'), 8 if schema == 1 else 0)

    def test_cli_suppresses_vendor_output_and_uses_cloud_hook(self):
        def cloud(**kwargs):
            self.assertIs(kwargs['collector'], e.collector)
            print('private vendor response')
            return 0, 'Private pipeline finished.'

        output = io.StringIO()
        with patch.object(p, 'cloud', side_effect=cloud), contextlib.redirect_stdout(output):
            self.assertEqual(e.main(), 0)
        self.assertEqual(output.getvalue(), 'Private pipeline finished.\n')

    def test_cli_error_does_not_print_vendor_exception_message(self):
        output = io.StringIO()
        with patch.object(p, 'cloud', side_effect=ValueError('private vendor response')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(e.main(), 1)
        self.assertNotIn('private vendor response', output.getvalue())
        self.assertIn('(ValueError). No data printed.', output.getvalue())


if __name__ == '__main__':
    unittest.main()
