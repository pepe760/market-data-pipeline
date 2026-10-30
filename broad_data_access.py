"""Opt-in immutable-batch reader with the known EPIC identity guard.

Only callers of this entry point receive this guard. Existing arbitrary parquet
or SQL consumers are not intercepted. No network, database or filesystem write
is performed, and an overlay never certifies historical PIT identity.
"""
import datetime as dt
import re
from pathlib import Path
from urllib.parse import urlparse

import intraday15m as core


class IdentityQualificationError(ValueError):
    """Known invalid or unsupported EPIC identity attribution cannot be used."""


def validate_identity_policy(policy):
    """Validate a dated explicit overlay; evidence hashes are references only.

    This pure function validates policy shape/scope, timezone and public-source
    SHA256 references. It does not download evidence or prove its content/hash.
    """
    if (not isinstance(policy, dict) or type(policy.get('schema_version')) is not int or
            policy['schema_version'] != 1):
        raise IdentityQualificationError('IDENTITY_POLICY_SCHEMA')
    if any(policy.get(key) != value for key, value in (
            ('symbol', 'EPICUSDT'), ('old_asset_id', 'epic-cash'),
            ('new_asset_id', 'epic-chain'))):
        raise IdentityQualificationError('IDENTITY_POLICY_SCOPE')
    if 'market' in policy and policy['market'] != 'spot':
        raise IdentityQualificationError('IDENTITY_POLICY_SCOPE')
    try:
        observed = dt.datetime.fromisoformat(policy['observed_at'])
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError('timezone')
    except (KeyError, TypeError, ValueError):
        raise IdentityQualificationError('IDENTITY_POLICY_OBSERVATION_CLOCK') from None
    evidence = policy.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        raise IdentityQualificationError('IDENTITY_POLICY_EVIDENCE')
    clean = []
    for item in evidence:
        if (not isinstance(item, dict) or not isinstance(item.get('sha256'), str) or
                not re.fullmatch('[a-f0-9]{64}', item['sha256']) or
                not isinstance(item.get('url'), str)):
            raise IdentityQualificationError('IDENTITY_POLICY_EVIDENCE')
        try:
            parsed = urlparse(item['url'])
            if (parsed.scheme != 'https' or parsed.hostname not in
                    {'api.coingecko.com', 'www.binance.com', 'data.binance.vision'} or
                    parsed.username is not None or parsed.password is not None or
                    parsed.port not in (None, 443) or not parsed.path):
                raise ValueError('public_url')
        except (KeyError, TypeError, ValueError):
            raise IdentityQualificationError('IDENTITY_POLICY_EVIDENCE') from None
        clean.append({'url': item['url'], 'sha256': item['sha256']})
    return {'schema_version': 1, 'symbol': 'EPICUSDT', 'market': 'spot',
            'old_asset_id': 'epic-cash', 'new_asset_id': 'epic-chain',
            'observed_at': observed.isoformat(), 'evidence': clean,
            'qualification': 'NOT_HISTORICAL_PIT_IDENTITY'}


def read_qualified_bars(batch, identity_policy=None, apply_identity_correction=False):
    """Verify one sealed broad batch and return its root bars.parquet DataFrame.

    Known EPIC/epic-cash rows fail closed by default, even when a policy is
    supplied. An explicitly requested, valid overlay copies rows in memory and
    preserves prices, original availability clocks and all immutable batch bytes.
    Correct epic-chain rows may be read; this function does not elevate them to
    historical PIT identity certification.
    """
    import pandas as pd

    batch = Path(batch)
    core.verify(batch)
    frame = pd.read_parquet(batch / 'bars.parquet')
    if 'symbol' not in frame.columns:
        raise IdentityQualificationError('SYMBOL_COLUMN_REQUIRED')
    epic = frame['symbol'].eq('EPICUSDT')
    if not epic.any():
        return frame
    if 'asset_id' not in frame.columns:
        raise IdentityQualificationError('EPIC_IDENTITY_MISSING')
    if (epic & ~frame['asset_id'].isin(['epic-cash', 'epic-chain'])).any():
        raise IdentityQualificationError('EPIC_IDENTITY_UNQUALIFIED')
    wrong = epic & frame['asset_id'].eq('epic-cash')
    if not wrong.any():
        return frame
    if apply_identity_correction is not True:
        raise IdentityQualificationError('EPIC_CASH_ATTRIBUTION_QUARANTINED')
    policy = validate_identity_policy(identity_policy)
    for name in ('original_asset_id', 'correction_available_at', 'identity_qualification'):
        if name in frame.columns and frame.loc[wrong, name].notna().any():
            raise IdentityQualificationError('IDENTITY_OVERLAY_COLUMN_CONFLICT')
    result = frame.copy(deep=True)
    result.loc[wrong, 'original_asset_id'] = result.loc[wrong, 'asset_id']
    result.loc[wrong, 'asset_id'] = policy['new_asset_id']
    result.loc[wrong, 'correction_available_at'] = policy['observed_at']
    result.loc[wrong, 'identity_qualification'] = 'NOT_HISTORICAL_PIT_IDENTITY'
    result.attrs['identity_correction_applied_in_memory'] = True
    result.attrs['identity_correction_evidence_sha256'] = [item['sha256'] for item in policy['evidence']]
    result.attrs['identity_correction_scope'] = 'THIS_READER_ONLY; NO_GLOBAL_PARQUET_OR_SQL_INTERCEPTION'
    return result
