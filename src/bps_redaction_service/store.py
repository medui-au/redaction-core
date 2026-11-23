"""SQLite storage: sessions and their token maps, API keys, admin lists, cache, audit.

What is kept, and for how long (plan D4, D5):
  sessions, session_tokens  the only health information held: each token's
                            original, encrypted; purged 24 h after the last
                            /redact (7 days at most), or on DELETE
  api_keys                  name, SHA-256 of the key, scopes
  list_rows                 the admin lists (always_redact, name_variants)
  cache                     slow-changing derived tables (address book, nicknames)
  audit                     counts and timings only: no text, no tokens; for
                            /admin actions, who did it
  admin_users               local admins (not BP users): name, scrypt hash
  admin_sessions            /admin logins: who, when; ended at logout or timeout
  flags, flag_reviews       what the checker found: types, tiers, offsets (90 days)
  flag_content              a flagged query's redacted content, encrypted, gone
                            with its session (the one place a known leak sits)

secure_delete overwrites deleted pages, and incremental vacuum gives the space
back, so purged originals don't linger in the file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    key_hash TEXT NOT NULL UNIQUE,
    scopes TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    key_id INTEGER NOT NULL REFERENCES api_keys(id),
    vault_key BLOB NOT NULL,
    created_at REAL NOT NULL,
    last_redact_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS session_tokens (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    token TEXT NOT NULL,
    original BLOB NOT NULL,
    PRIMARY KEY (session_id, token)
);
CREATE TABLE IF NOT EXISTS list_rows (
    id INTEGER PRIMARY KEY,
    list_name TEXT NOT NULL,
    row TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS list_rows_name ON list_rows(list_name);
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS flags (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    key_id INTEGER NOT NULL,
    at REAL NOT NULL,
    profiles TEXT,
    patient_hash TEXT,
    top_tier INTEGER NOT NULL,
    counts TEXT NOT NULL,
    hits TEXT NOT NULL,
    ruleset TEXT,
    detector TEXT
);
CREATE INDEX IF NOT EXISTS flags_at ON flags(at);
CREATE TABLE IF NOT EXISTS flag_reviews (
    flag_id TEXT PRIMARY KEY REFERENCES flags(id) ON DELETE CASCADE,
    disposition TEXT NOT NULL,
    reviewer TEXT,
    note TEXT,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS flag_content (
    flag_id TEXT PRIMARY KEY REFERENCES flags(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    content BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_users (
    name TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    id_hash TEXT PRIMARY KEY,
    who TEXT NOT NULL,
    kind TEXT NOT NULL,
    csrf TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    key_id INTEGER,
    action TEXT NOT NULL,
    profiles TEXT,
    patient_hash TEXT,
    counts TEXT,
    duration_ms INTEGER,
    outcome TEXT NOT NULL
);
"""

SCOPES = frozenset({'redact', 'deredact', 'verify', 'flags', 'admin'})
DISPOSITIONS = ('unreviewed', 'true_leak', 'false_positive', 'acceptable')
LISTS = ('always_redact', 'name_variants', 'first_names', 'identifier_labels')


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


_SCRYPT = {'n': 2 ** 14, 'r': 8, 'p': 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f'scrypt${salt.hex()}${digest.hex()}'


def check_password(password: str, stored: str) -> bool:
    try:
        _scheme, salt, digest = stored.split('$')
    except ValueError:
        return False
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), dklen=32, **_SCRYPT)
    return hmac.compare_digest(candidate.hex(), digest)


@dataclass
class ApiKey:
    id: int
    name: str
    scopes: frozenset


@dataclass
class Session:
    id: str
    key_id: int
    vault_key: str
    created_at: float
    last_redact_at: float
    expires_at: float


class Store:
    def __init__(self, path: str, fernet_keys: list[str]):
        if not fernet_keys:
            raise ValueError('at least one encryption key is required')
        self._fernet = MultiFernet([Fernet(k.encode() if isinstance(k, str) else k)
                                    for k in fernet_keys])
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            # auto_vacuum only takes effect on a new file (or after VACUUM).
            self._db.execute('PRAGMA auto_vacuum = INCREMENTAL')
            self._db.execute('PRAGMA journal_mode = WAL')
            self._db.execute('PRAGMA secure_delete = ON')
            self._db.execute('PRAGMA foreign_keys = ON')
            self._db.executescript(SCHEMA)
            columns = {r[1] for r in self._db.execute('PRAGMA table_info(audit)')}
            if 'actor' not in columns:               # files made before /admin
                self._db.execute('ALTER TABLE audit ADD COLUMN actor TEXT')

    # ── encryption ────────────────────────────────────────────────────────────

    def encrypt(self, text: str) -> bytes:
        return self._fernet.encrypt(text.encode())

    def decrypt(self, blob: bytes) -> str:
        return self._fernet.decrypt(blob).decode()

    # ── API keys ──────────────────────────────────────────────────────────────

    def add_key(self, name: str, scopes: set[str]) -> str:
        """Create a key; returns it once. Only its hash is kept."""
        unknown = set(scopes) - SCOPES
        if unknown:
            raise ValueError(f'unknown scopes: {sorted(unknown)}')
        key = 'bpsr_' + secrets.token_urlsafe(32)
        with self._lock:
            self._db.execute('INSERT INTO api_keys (name, key_hash, scopes, created_at) VALUES (?,?,?,?)',
                             (name, hash_key(key), ','.join(sorted(scopes)), time.time()))
        return key

    def find_key(self, key: str) -> ApiKey | None:
        with self._lock:
            row = self._db.execute('SELECT id, name, scopes FROM api_keys '
                                   'WHERE key_hash = ? AND revoked_at IS NULL', (hash_key(key),)).fetchone()
            if row:
                self._db.execute('UPDATE api_keys SET last_used_at = ? WHERE id = ?', (time.time(), row['id']))
        if not row:
            return None
        return ApiKey(row['id'], row['name'], frozenset(filter(None, row['scopes'].split(','))))

    def list_keys(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute('SELECT id, name, scopes, created_at, last_used_at, revoked_at '
                                    'FROM api_keys ORDER BY id').fetchall()
        return [dict(r) for r in rows]

    def revoke_key(self, name: str) -> bool:
        with self._lock:
            cur = self._db.execute('UPDATE api_keys SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL',
                                   (time.time(), name))
            # A revoked key's sessions go too: nothing can read them any more.
            self._db.execute('DELETE FROM sessions WHERE key_id IN '
                             '(SELECT id FROM api_keys WHERE name = ? AND revoked_at IS NOT NULL)', (name,))
        return cur.rowcount > 0

    # ── sessions ──────────────────────────────────────────────────────────────

    def create_session(self, key_id: int, ttl: float, cap: float) -> Session:
        now = time.time()
        session = Session(id=secrets.token_urlsafe(16), key_id=key_id,
                          vault_key=secrets.token_hex(32), created_at=now,
                          last_redact_at=now, expires_at=min(now + ttl, now + cap))
        with self._lock:
            self._db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?)',
                             (session.id, key_id, self.encrypt(session.vault_key), now, now,
                              session.expires_at))
        return session

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._db.execute('SELECT * FROM sessions WHERE id = ?', (session_id,)).fetchone()
        if not row:
            return None
        try:
            vault_key = self.decrypt(row['vault_key'])
        except InvalidToken:          # encrypted under a key no longer configured
            return None
        return Session(row['id'], row['key_id'], vault_key, row['created_at'],
                       row['last_redact_at'], row['expires_at'])

    def touch_session(self, session: Session, ttl: float, cap: float) -> float:
        """A /redact on the session: extend to 24 h from now, never past the cap."""
        now = time.time()
        expires = min(now + ttl, session.created_at + cap)
        with self._lock:
            self._db.execute('UPDATE sessions SET last_redact_at = ?, expires_at = ? WHERE id = ?',
                             (now, expires, session.id))
        session.last_redact_at, session.expires_at = now, expires
        return expires

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._db.execute('DELETE FROM sessions WHERE id = ?', (session_id,))

    def add_tokens(self, session_id: str, token_map: dict[str, str]) -> None:
        if not token_map:
            return
        with self._lock:
            self._db.executemany('INSERT OR IGNORE INTO session_tokens VALUES (?,?,?)',
                                 [(session_id, t, self.encrypt(o)) for t, o in token_map.items()])

    def token_map(self, session_id: str) -> dict[str, str]:
        with self._lock:
            rows = self._db.execute('SELECT token, original FROM session_tokens WHERE session_id = ?',
                                    (session_id,)).fetchall()
        out = {}
        for r in rows:
            try:
                out[r['token']] = self.decrypt(r['original'])
            except InvalidToken:
                continue
        return out

    def purge_expired(self, flag_days: float = 90, admin_idle: float = 1800,
                      admin_cap: float = 8 * 3600) -> int:
        """Delete expired sessions (their tokens and flagged content cascade), and
        flags past their keeping time; return how many sessions went."""
        with self._lock:
            cur = self._db.execute('DELETE FROM sessions WHERE expires_at <= ?', (time.time(),))
            self._db.execute('DELETE FROM flags WHERE at <= ?', (time.time() - flag_days * 86400,))
            self._db.execute('DELETE FROM admin_sessions WHERE last_seen_at <= ? OR created_at <= ?',
                             (time.time() - admin_idle, time.time() - admin_cap))
            self._db.execute('PRAGMA incremental_vacuum')
        return cur.rowcount

    def count_sessions(self) -> int:
        with self._lock:
            return self._db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]

    # ── admin lists ───────────────────────────────────────────────────────────

    def list_rows(self, list_name: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute('SELECT id, row FROM list_rows WHERE list_name = ? ORDER BY id',
                                    (list_name,)).fetchall()
        return [{**json.loads(r['row']), 'id': r['id']} for r in rows]

    def replace_list(self, list_name: str, rows: list[dict]) -> int:
        if list_name not in LISTS:
            raise ValueError(f'unknown list: {list_name}')
        with self._lock:
            self._db.execute('BEGIN')
            self._db.execute('DELETE FROM list_rows WHERE list_name = ?', (list_name,))
            self._db.executemany('INSERT INTO list_rows (list_name, row) VALUES (?, ?)',
                                 [(list_name, json.dumps({k: v for k, v in r.items() if k != 'id'}))
                                  for r in rows])
            self._db.execute('COMMIT')
        return len(rows)

    # ── cache ─────────────────────────────────────────────────────────────────

    def cache_get(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute('SELECT value FROM cache WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else None

    def cache_set(self, key: str, value: str, description: str = '') -> None:
        with self._lock:
            self._db.execute('INSERT INTO cache VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET '
                             'value = excluded.value, description = excluded.description, '
                             'updated_at = excluded.updated_at', (key, value, description, time.time()))

    # ── flags ─────────────────────────────────────────────────────────────────

    def add_flag(self, *, session_id, key_id, profiles, patient_hash, top_tier, counts,
                 hits, ruleset, detector, content: str) -> str:
        flag_id = 'f_' + secrets.token_urlsafe(12)
        with self._lock:
            self._db.execute('BEGIN')
            self._db.execute('INSERT INTO flags VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                             (flag_id, session_id, key_id, time.time(), ','.join(profiles),
                              patient_hash, top_tier, json.dumps(counts), json.dumps(hits),
                              ruleset, detector))
            self._db.execute('INSERT INTO flag_content VALUES (?,?,?)',
                             (flag_id, session_id, self.encrypt(content)))
            self._db.execute('COMMIT')
        return flag_id

    def _flag_row(self, r) -> dict:
        return {'id': r['id'], 'at': r['at'], 'key_id': r['key_id'],
                'profiles': [p for p in (r['profiles'] or '').split(',') if p],
                'patient_hash': r['patient_hash'], 'top_tier': r['top_tier'],
                'counts': json.loads(r['counts']), 'hits': json.loads(r['hits']),
                'ruleset': r['ruleset'], 'detector': r['detector'],
                'disposition': r['disposition'] or 'unreviewed',
                'reviewer': r['reviewer'], 'note': r['note'], 'reviewed_at': r['reviewed_at']}

    _FLAG_SELECT = ('SELECT f.*, r.disposition, r.reviewer, r.note, r.at AS reviewed_at '
                    'FROM flags f LEFT JOIN flag_reviews r ON r.flag_id = f.id')

    def list_flags(self, *, since=None, until=None, tier=None, finding_type=None,
                   disposition=None, key_id=None, newest_first=True, limit=50, offset=0) -> list[dict]:
        where, args = [], []
        if since is not None:
            where.append('f.at >= ?'); args.append(since)
        if until is not None:
            where.append('f.at < ?'); args.append(until)
        if tier is not None:
            where.append('f.top_tier = ?'); args.append(tier)
        if key_id is not None:
            where.append('f.key_id = ?'); args.append(key_id)
        if disposition == 'unreviewed':
            where.append('r.disposition IS NULL')
        elif disposition:
            where.append('r.disposition = ?'); args.append(disposition)
        sql = self._FLAG_SELECT + (' WHERE ' + ' AND '.join(where) if where else '')
        sql += f' ORDER BY f.at {"DESC" if newest_first else "ASC"}'
        with self._lock:
            rows = [self._flag_row(r) for r in self._db.execute(sql, args).fetchall()]
        if finding_type:
            rows = [r for r in rows if finding_type in r['counts']]
        return rows[offset:offset + limit]

    def get_flag(self, flag_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute(self._FLAG_SELECT + ' WHERE f.id = ?', (flag_id,)).fetchone()
            c = self._db.execute('SELECT content FROM flag_content WHERE flag_id = ?', (flag_id,)).fetchone()
        if not r:
            return None
        flag = self._flag_row(r)
        content = None
        if c:
            try:
                content = self.decrypt(c['content'])
            except InvalidToken:
                content = None
        flag['content'] = content            # None once its session has gone
        return flag

    def review_flag(self, flag_id: str, disposition: str, reviewer: str, note: str | None) -> bool:
        if disposition not in DISPOSITIONS:
            raise ValueError('unknown disposition')
        with self._lock:
            if not self._db.execute('SELECT 1 FROM flags WHERE id = ?', (flag_id,)).fetchone():
                return False
            if disposition == 'unreviewed':
                self._db.execute('DELETE FROM flag_reviews WHERE flag_id = ?', (flag_id,))
            else:
                self._db.execute('INSERT INTO flag_reviews VALUES (?,?,?,?,?) ON CONFLICT(flag_id) '
                                 'DO UPDATE SET disposition = excluded.disposition, reviewer = '
                                 'excluded.reviewer, note = excluded.note, at = excluded.at',
                                 (flag_id, disposition, reviewer, note, time.time()))
        return True

    def flag_summary(self, since=None) -> dict:
        rows = self.list_flags(since=since, limit=10 ** 9)
        by_type: dict = {}
        by_tier: dict = {}
        by_day: dict = {}
        by_disposition: dict = {}
        for r in rows:
            for t, n in r['counts'].items():
                by_type[t] = by_type.get(t, 0) + n
            by_tier[str(r['top_tier'])] = by_tier.get(str(r['top_tier']), 0) + 1
            day = time.strftime('%Y-%m-%d', time.gmtime(r['at']))
            by_day[day] = by_day.get(day, 0) + 1
            by_disposition[r['disposition']] = by_disposition.get(r['disposition'], 0) + 1
        return {'flags': len(rows), 'by_top_tier': by_tier, 'by_type': by_type,
                'by_day': by_day, 'by_disposition': by_disposition}

    # ── audit ─────────────────────────────────────────────────────────────────

    def audit(self, *, key_id, action, outcome, profiles=None, patient_hash=None,
              counts=None, duration_ms=None, actor=None) -> None:
        with self._lock:
            self._db.execute('INSERT INTO audit (at, key_id, action, profiles, patient_hash, counts, '
                             'duration_ms, outcome, actor) VALUES (?,?,?,?,?,?,?,?,?)',
                             (time.time(), key_id, action, ','.join(profiles or []) or None,
                              patient_hash, json.dumps(counts) if counts else None, duration_ms,
                              outcome, actor))

    def recent_audit(self, actions_prefix: str, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute('SELECT at, action, outcome, actor FROM audit WHERE action LIKE ? '
                                    'ORDER BY id DESC LIMIT ?', (actions_prefix + '%', limit)).fetchall()
        return [dict(r) for r in rows]

    # ── local admins ──────────────────────────────────────────────────────────
    # For the person who installs the service, and for when BP can't be
    # reached. BP users log in with their BP password and are never stored.

    def add_admin(self, name: str, password: str) -> None:
        if len(password) < 12:
            raise ValueError('a local admin password needs at least 12 characters')
        with self._lock:
            self._db.execute('INSERT INTO admin_users VALUES (?,?,?)',
                             (name, hash_password(password), time.time()))

    def check_admin(self, name: str, password: str) -> bool:
        with self._lock:
            row = self._db.execute('SELECT password_hash FROM admin_users WHERE name = ?',
                                   (name,)).fetchone()
        if not row:
            hash_password(password)          # same work either way: no timing hint
            return False
        return check_password(password, row['password_hash'])

    def list_admins(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                'SELECT name, created_at FROM admin_users ORDER BY name').fetchall()]

    def remove_admin(self, name: str) -> bool:
        with self._lock:
            cur = self._db.execute('DELETE FROM admin_users WHERE name = ?', (name,))
            self._db.execute("DELETE FROM admin_sessions WHERE kind = 'local' AND who = ?", (name,))
        return cur.rowcount > 0

    # ── /admin sessions ───────────────────────────────────────────────────────
    # The cookie holds a random id; only its hash is kept, as for API keys.

    def start_admin_session(self, who: str, kind: str) -> tuple[str, str]:
        session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._db.execute('INSERT INTO admin_sessions VALUES (?,?,?,?,?,?)',
                             (hash_key(session_id), who, kind, csrf, now, now))
        return session_id, csrf

    def admin_session(self, session_id: str, idle: float, cap: float) -> dict | None:
        """The live session for a cookie, touched; None (and deleted) once timed out."""
        now = time.time()
        with self._lock:
            row = self._db.execute('SELECT * FROM admin_sessions WHERE id_hash = ?',
                                   (hash_key(session_id),)).fetchone()
            if not row:
                return None
            if now - row['last_seen_at'] > idle or now - row['created_at'] > cap:
                self._db.execute('DELETE FROM admin_sessions WHERE id_hash = ?', (row['id_hash'],))
                return None
            self._db.execute('UPDATE admin_sessions SET last_seen_at = ? WHERE id_hash = ?',
                             (now, row['id_hash']))
        return {'who': row['who'], 'kind': row['kind'], 'csrf': row['csrf']}

    def end_admin_session(self, session_id: str) -> None:
        with self._lock:
            self._db.execute('DELETE FROM admin_sessions WHERE id_hash = ?', (hash_key(session_id),))


class StoreLists:
    """The core's Lists interface over the store."""

    def __init__(self, store: Store):
        self._store = store

    def rows(self, table: str) -> list[dict]:
        return self._store.list_rows(table)


class StoreCache:
    """The core's Cache interface over the store."""

    def __init__(self, store: Store):
        self._store = store

    def get(self, key: str) -> str | None:
        return self._store.cache_get(key)

    def set(self, key: str, value: str, description: str = '') -> None:
        self._store.cache_set(key, value, description)
