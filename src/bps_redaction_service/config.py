"""Settings, from the environment.

  BPS_REDACT_DB                 SQLite file (default ./bps_redact.sqlite)
  BPS_REDACT_KEYS               required: Fernet key(s), comma-separated, newest
                                first (older ones still decrypt: rotation)
  BPS_REDACT_AUDIT_SECRET       the key for patient-id hashes in flags and the audit
                                (bps-redact keygen audit). Separate from the
                                encryption keys so rotating those keeps one
                                patient's hash the same. Unset: derived from the
                                first encryption key, with a warning
  BPS_REDACT_SOURCE             'bp' (with BPS_REDACT_BP_DSN, an ODBC connection
                                string) or 'fixture' (with BPS_REDACT_FIXTURE, a
                                JSON file of invented data)
  BPS_REDACT_SESSION_TTL_HOURS  24: a session lives this long after its last /redact
  BPS_REDACT_SESSION_CAP_DAYS   7: and never longer than this from creation
  BPS_REDACT_MAX_BYTES          2000000: request size cap
  BPS_REDACT_RATE_PER_MINUTE    120: requests per key
  BPS_REDACT_PURGE_MINUTES      10: how often expired sessions are purged
  BPS_REDACT_FLAG_DAYS          90: how long flag metadata is kept (flagged
                                content goes with its session, in 24 h)
  BPS_REDACT_HOST / _PORT       127.0.0.1 / 8470: LAN exposure is opt-in
  BPS_REDACT_ADMIN_GROUPS       1,3: BP group codes that may use /admin (Practice
                                Manager, Practice Owner)
  BPS_REDACT_ADMIN_IDLE_MINUTES 30: an /admin login ends after this long unused
  BPS_REDACT_ADMIN_CAP_HOURS    8: and never lasts longer than this
  BPS_REDACT_SECURE_COOKIE      0: set 1 behind an HTTPS proxy (cookie sent over HTTPS only)

Generate a key with:  bps-redact keygen
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = '') -> str:
    return os.environ.get(f'BPS_REDACT_{name}', default)


@dataclass
class Settings:
    db_path: str = 'bps_redact.sqlite'
    fernet_keys: list[str] = field(default_factory=list)
    audit_secret: str = ''
    source: str = 'fixture'
    bp_dsn: str = ''
    fixture_path: str = ''
    session_ttl: float = 24 * 3600
    session_cap: float = 7 * 86400
    max_bytes: int = 2_000_000
    rate_per_minute: int = 120
    purge_seconds: float = 600
    flag_days: float = 90
    host: str = '127.0.0.1'
    port: int = 8470
    admin_groups: list[int] = field(default_factory=lambda: [1, 3])
    admin_idle: float = 1800
    admin_cap: float = 8 * 3600
    secure_cookie: bool = False

    @classmethod
    def from_env(cls) -> 'Settings':
        return cls(
            db_path=_env('DB', 'bps_redact.sqlite'),
            fernet_keys=[k.strip() for k in _env('KEYS').split(',') if k.strip()],
            audit_secret=_env('AUDIT_SECRET').strip(),
            source=_env('SOURCE', 'fixture'),
            bp_dsn=_env('BP_DSN'),
            fixture_path=_env('FIXTURE'),
            session_ttl=float(_env('SESSION_TTL_HOURS', '24')) * 3600,
            session_cap=float(_env('SESSION_CAP_DAYS', '7')) * 86400,
            max_bytes=int(_env('MAX_BYTES', '2000000')),
            rate_per_minute=int(_env('RATE_PER_MINUTE', '120')),
            purge_seconds=float(_env('PURGE_MINUTES', '10')) * 60,
            flag_days=float(_env('FLAG_DAYS', '90')),
            host=_env('HOST', '127.0.0.1'),
            port=int(_env('PORT', '8470')),
            admin_groups=[int(g) for g in _env('ADMIN_GROUPS', '1,3').split(',') if g.strip()],
            admin_idle=float(_env('ADMIN_IDLE_MINUTES', '30')) * 60,
            admin_cap=float(_env('ADMIN_CAP_HOURS', '8')) * 3600,
            secure_cookie=_env('SECURE_COOKIE', '0').strip().lower() in ('1', 'true', 'yes', 'on'),
        )

    def identity_source(self):
        if self.source == 'bp':
            if not self.bp_dsn:
                raise ValueError('BPS_REDACT_SOURCE=bp needs BPS_REDACT_BP_DSN')
            from bps_redaction.adapters.bp import BPSource
            return BPSource(self.bp_dsn)
        if self.source == 'fixture':
            if not self.fixture_path:
                raise ValueError('BPS_REDACT_SOURCE=fixture needs BPS_REDACT_FIXTURE')
            from bps_redaction.adapters.fixture import FixtureSource
            return FixtureSource(self.fixture_path)
        raise ValueError(f'unknown source: {self.source}')
