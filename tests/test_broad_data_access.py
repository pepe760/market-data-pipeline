import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import broad_data_access as b
import intraday15m as c


def policy(**changes):
    result = dict(schema_version=1, symbol='EPICUSDT', market='spot',
                  old_asset_id='epic-cash', new_asset_id='epic-chain',
                  observed_at='2026-10-30T13:28:44.956130+00:00',
                  evidence=[{'url': 'https://api.coingecko.com/api/v3/coins/epic-chain',
                             'sha256': 'b85ede30ee68ecbd999d49581fb0bb1281fc4d8e750c525f56f612b2436a22f6'}])
    result.update(changes)
    return result


def frame(epic_id='epic-cash'):
    return pd.DataFrame([
        dict(symbol='EPICUSDT', asset_id=epic_id, open=1.0, high=2.0, low=0.5,
             close=1.5, volume=10.0, available_at='2026-10-29T00:05:00+00:00'),
        dict(symbol='BTCUSDT', asset_id='bitcoin', open=10.0, high=20.0, low=5.0,
             close=15.0, volume=100.0, available_at='2026-10-29T00:06:00+00:00')])


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.batch = Path(self.tmp.name) / '20261030T140000Z-123456789abc'
        self.batch.mkdir()

    def seal(self, data):
        data.to_parquet(self.batch / 'bars.parquet', index=False)
        (self.batch / 'raw.bin').write_bytes(b'original immutable exchange response')
        c.seal(self.batch)

    def bytes(self):
        return {p.name: p.read_bytes() for p in self.batch.iterdir()}

    def test_old_epic_default_fails_closed_even_when_policy_is_present(self):
        self.seal(frame())
        before = self.bytes()
        for kwargs in ({}, {'identity_policy': policy()}):
            with self.assertRaisesRegex(b.IdentityQualificationError, 'QUARANTINED'):
                b.read_qualified_bars(self.batch, **kwargs)
        self.assertEqual(self.bytes(), before)

    def test_explicit_overlay_only_copies_identity_and_preserves_values_and_bytes(self):
        original = frame()
        self.seal(original)
        before = self.bytes()
        p = policy(); original_policy = copy.deepcopy(p)
        with patch('pandas.read_parquet', return_value=original):
            result = b.read_qualified_bars(self.batch, p, True)
        self.assertIsNot(result, original)
        self.assertEqual(original.loc[0, 'asset_id'], 'epic-cash')
        self.assertEqual(result.loc[0, 'asset_id'], 'epic-chain')
        self.assertEqual(result.loc[0, 'original_asset_id'], 'epic-cash')
        self.assertEqual(result.loc[0, 'correction_available_at'], p['observed_at'])
        self.assertEqual(result.loc[0, 'identity_qualification'], 'NOT_HISTORICAL_PIT_IDENTITY')
        pd.testing.assert_frame_equal(result[original.columns.drop('asset_id')],
                                      original[original.columns.drop('asset_id')])
        pd.testing.assert_series_equal(result.loc[1, original.columns], original.loc[1])
        self.assertEqual(self.bytes(), before)
        self.assertEqual(p, original_policy)
        self.assertNotIn('identity_correction_applied_in_memory', original.attrs)
        c.verify(self.batch)

    def test_missing_wrong_or_naive_policy_is_rejected(self):
        self.seal(frame())
        cases = [None, {}, policy(schema_version=True), policy(symbol='OTHER'), policy(old_asset_id='wrong'),
                 policy(new_asset_id='another-epic'), policy(market='futures'),
                 policy(observed_at='2026-10-30T13:28:44'), policy(observed_at='bad'),
                 policy(evidence=[]), policy(evidence=[{'sha256': 'a' * 64, 'url': None}]),
                 policy(evidence=[{'sha256': 'bad', 'url': 'https://api.coingecko.com/x'}]),
                 policy(evidence=[{'sha256': 'a' * 64, 'url': 'file:///private/secret'}]),
                 policy(evidence=[{'sha256': 'a' * 64, 'url': 'https://private.example/token'}])]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(b.IdentityQualificationError):
                    b.read_qualified_bars(self.batch, candidate, True)

    def test_non_boolean_explicit_switch_does_not_enable_correction(self):
        self.seal(frame())
        for value in ('true', 1, None):
            with self.assertRaisesRegex(b.IdentityQualificationError, 'QUARANTINED'):
                b.read_qualified_bars(self.batch, policy(), value)

    def test_other_symbols_return_unchanged_and_do_not_require_overlay_policy(self):
        original = frame().iloc[[1]].reset_index(drop=True)
        self.seal(original)
        result = b.read_qualified_bars(self.batch, {'invalid': 'unused'}, True)
        pd.testing.assert_frame_equal(result, original)
        self.assertNotIn('original_asset_id', result.columns)

    def test_correct_epic_chain_is_read_without_pit_promotion_or_extra_columns(self):
        original = frame('epic-chain')
        self.seal(original)
        result = b.read_qualified_bars(self.batch)
        pd.testing.assert_frame_equal(result, original)
        self.assertNotIn('identity_correction_applied_in_memory', result.attrs)

    def test_integrity_verification_precedes_parquet_read(self):
        self.seal(frame())
        (self.batch / 'raw.bin').write_bytes(b'tampered')
        with patch('pandas.read_parquet') as read:
            with self.assertRaisesRegex(ValueError, 'payload_hash'):
                b.read_qualified_bars(self.batch, policy(), True)
            read.assert_not_called()

    def test_unknown_or_missing_epic_identity_is_fail_closed(self):
        for data in (frame(None), frame('unknown'), frame().drop(columns='asset_id')):
            with self.subTest(columns=list(data.columns)):
                with patch.object(c, 'verify'), patch('pandas.read_parquet', return_value=data):
                    with self.assertRaises(b.IdentityQualificationError):
                        b.read_qualified_bars(self.batch, policy(), True)

    def test_existing_overlay_fields_cannot_be_silently_overwritten(self):
        data = frame(); data['original_asset_id'] = ['different-source-history', None]
        self.seal(data)
        with self.assertRaisesRegex(b.IdentityQualificationError, 'CONFLICT'):
            b.read_qualified_bars(self.batch, policy(), True)

    def test_pure_policy_validation_accepts_aware_z_clock_and_does_not_mutate(self):
        p = policy(observed_at='2026-10-30T13:28:44Z')
        before = copy.deepcopy(p)
        result = b.validate_identity_policy(p)
        self.assertEqual(result['observed_at'], '2026-10-30T13:28:44+00:00')
        self.assertEqual(result['qualification'], 'NOT_HISTORICAL_PIT_IDENTITY')
        self.assertEqual(p, before)


if __name__ == '__main__':
    unittest.main()
