"""The HTTP service, on invented data: each test is a promise from the plan (D4–D6, D11)."""

import logging
import sqlite3
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from bps_redaction_service import Settings, create_app
from bps_redaction_service.store import Store

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
TEXT = 'Marigold Quillfeather (goldie.q@example.com) was reviewed by Dr Small.'


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=str(tmp_path / 'svc.sqlite'), fernet_keys=[Fernet.generate_key().decode()],
                    source='fixture', fixture_path=str(FIXTURE), rate_per_minute=1000)


@pytest.fixture
def svc(settings):
    app = create_app(settings)
    store: Store = app.state.store
    keys = {'client': store.add_key('client', {'redact', 'deredact'}),
            'other': store.add_key('other', {'redact', 'deredact'}),
            'readonly': store.add_key('readonly', {'deredact'})}
    with TestClient(app) as client:
        client.keys = keys
        client.store = store
        yield client


def auth(svc, who='client'):
    return {'Authorization': f'Bearer {svc.keys[who]}'}


def redact(svc, content=TEXT, who='client', **extra):
    body = {'profiles': ['prompt'], 'patient_id': 1001, 'content': content, **extra}
    return svc.post('/v1/redact', json=body, headers=auth(svc, who))


def test_requires_a_valid_key_and_scope(svc):
    assert svc.post('/v1/redact', json={'content': 'x'}).status_code == 401
    assert svc.post('/v1/redact', json={'content': 'x'},
                    headers={'Authorization': 'Bearer nope'}).status_code == 401
    assert redact(svc, who='readonly').status_code == 403


def test_redact_then_deredact_round_trip(svc):
    r = redact(svc).json()
    assert 'Quillfeather' not in r['content'] and 'goldie' not in r['content']
    assert r['summary']['patient'] >= 1 and r['ruleset'].startswith('medui-')
    back = svc.post('/v1/deredact', json={'content': r['content'], 'session_id': r['session_id']},
                    headers=auth(svc)).json()
    assert back['content'] == TEXT and back['unresolved'] == 0 and back['swept'] == 0


def test_tokens_stable_within_a_session_and_not_across(svc):
    a = redact(svc).json()
    b = redact(svc, session_id=a['session_id']).json()
    c = redact(svc).json()
    assert a['content'] == b['content']
    assert a['content'] != c['content'] and a['session_id'] != c['session_id']


def test_another_keys_session_is_invisible(svc):
    sid = redact(svc).json()['session_id']
    r = svc.post('/v1/deredact', json={'content': 'x', 'session_id': sid}, headers=auth(svc, 'other'))
    assert r.status_code == 404
    assert redact(svc, who='other', session_id=sid).status_code == 404


def test_delete_session_purges_now(svc):
    sid = redact(svc).json()['session_id']
    assert svc.delete(f'/v1/sessions/{sid}', headers=auth(svc)).status_code == 204
    assert svc.post('/v1/deredact', json={'content': 'x', 'session_id': sid},
                    headers=auth(svc)).status_code == 404


def test_expired_session_is_refused_and_purged(svc, settings):
    sid = redact(svc).json()['session_id']
    svc.store._db.execute('UPDATE sessions SET expires_at = ? WHERE id = ?', (time.time() - 1, sid))
    r = svc.post('/v1/deredact', json={'content': 'x', 'session_id': sid}, headers=auth(svc))
    assert r.status_code == 410 and r.json() == {'error': 'session_expired'}
    sid2 = redact(svc).json()['session_id']
    svc.store._db.execute('UPDATE sessions SET expires_at = ? WHERE id = ?', (time.time() - 1, sid2))
    assert svc.store.purge_expired() == 1
    assert svc.store._db.execute('SELECT COUNT(*) FROM session_tokens').fetchone()[0] == 0


def test_deredact_does_not_extend_the_session(svc):
    r = redact(svc).json()
    before = svc.store.get_session(r['session_id']).expires_at
    time.sleep(0.01)
    svc.post('/v1/deredact', json={'content': r['content'], 'session_id': r['session_id']},
             headers=auth(svc))
    assert svc.store.get_session(r['session_id']).expires_at == before


def test_ttl_never_passes_the_absolute_cap(settings):
    store = Store(settings.db_path, settings.fernet_keys)
    kid = store.find_key(store.add_key('k', {'redact'})).id
    s = store.create_session(kid, ttl=24 * 3600, cap=3600)
    assert s.expires_at <= s.created_at + 3600
    assert store.touch_session(s, ttl=24 * 3600, cap=3600) <= s.created_at + 3600


def test_only_used_tokens_are_stored_and_encrypted(svc, settings):
    r = redact(svc).json()
    n = svc.store._db.execute('SELECT COUNT(*) FROM session_tokens WHERE session_id = ?',
                              (r['session_id'],)).fetchone()[0]
    assert 0 < n <= r['content'].count('[Redacted ')      # not the pre-minted address book
    svc.store._db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    raw = Path(settings.db_path).read_bytes()
    assert b'Quillfeather' not in raw and b'goldie' not in raw


def test_fails_closed_for_an_unknown_patient(svc):
    r = svc.post('/v1/redact', json={'profiles': ['prompt'], 'patient_id': 999, 'content': TEXT},
                 headers=auth(svc))
    assert r.status_code == 503 and r.json() == {'error': 'identity_source_unavailable'}


def test_general_prompt_without_a_patient(svc):
    r = svc.post('/v1/redact', json={'profiles': ['input', 'prompt'],
                                     'content': 'Ask Dr Small; mail someone@example.net'},
                 headers=auth(svc)).json()
    assert '@' not in r['content'] and 'Small' not in r['content']
    assert r['identifiers_loaded'] is False


def test_record_profile_takes_structured_content(svc):
    r = svc.post('/v1/redact', json={'profiles': ['record'], 'patient_id': 1001,
                                     'content': [{'Letter': 'Dear Dr Brindlecombe, re Mrs Quillfeather'}]},
                 headers=auth(svc))
    assert r.status_code == 200
    assert 'Brindlecombe' not in str(r.json()['content'])
    bad = svc.post('/v1/redact', json={'profiles': ['record'], 'content': [{}]}, headers=auth(svc))
    assert bad.status_code == 422


def test_request_size_cap(settings):
    settings.max_bytes = 1000
    app = create_app(settings)
    key = app.state.store.add_key('k', {'redact'})
    with TestClient(app) as client:
        r = client.post('/v1/redact', json={'content': 'x' * 5000},
                        headers={'Authorization': f'Bearer {key}'})
    assert r.status_code == 413


def test_rate_limit(settings):
    settings.rate_per_minute = 2
    app = create_app(settings)
    key = app.state.store.add_key('k', {'redact'})
    with TestClient(app) as client:
        codes = [client.get('/v1/info', headers={'Authorization': f'Bearer {key}'}).status_code
                 for _ in range(3)]
    assert codes == [200, 200, 429]


def test_invalid_requests_do_not_echo_text(svc):
    r = svc.post('/v1/redact', json={'profiles': ['nonsense'], 'content': TEXT}, headers=auth(svc))
    assert r.status_code == 422 and 'Quillfeather' not in r.text


def test_no_text_reaches_the_logs(svc, caplog):
    caplog.set_level(logging.DEBUG)
    r = redact(svc).json()
    svc.post('/v1/deredact', json={'content': r['content'], 'session_id': r['session_id']},
             headers=auth(svc))
    svc.post('/v1/redact', json={'profiles': ['prompt'], 'patient_id': 999, 'content': TEXT},
             headers=auth(svc))
    for record in caplog.records:
        message = record.getMessage()
        assert 'Quillfeather' not in message and 'goldie' not in message
        assert r['session_id'] not in message


def test_audit_holds_counts_only(svc):
    redact(svc)
    rows = svc.store._db.execute('SELECT * FROM audit').fetchall()
    assert rows and all('Quillfeather' not in str(tuple(row)) for row in rows)
    assert all(row['patient_hash'] != '1001' for row in rows)


def test_health_and_info(svc):
    assert svc.get('/v1/health').json() == {'status': 'ok', 'source': 'ok'}
    info = svc.get('/v1/info', headers=auth(svc)).json()
    assert info['session_ttl_hours'] == 24 and info['scopes'] == ['deredact', 'redact']


def test_key_rotation_keeps_old_sessions_readable(settings):
    store = Store(settings.db_path, settings.fernet_keys)
    kid = store.find_key(store.add_key('k', {'redact'})).id
    s = store.create_session(kid, 3600, 86400)
    store.add_tokens(s.id, {'[Redacted Patient: PT-0123456789ab]': 'Marigold'})
    rotated = Store(settings.db_path, [Fernet.generate_key().decode()] + settings.fernet_keys)
    assert rotated.token_map(s.id) == {'[Redacted Patient: PT-0123456789ab]': 'Marigold'}


def test_revoking_a_key_ends_its_sessions(svc):
    sid = redact(svc).json()['session_id']
    assert svc.store.revoke_key('client')
    assert svc.store.get_session(sid) is None
    assert redact(svc).status_code == 401


def test_patient_hashes_survive_key_rotation(tmp_path):
    """The hash key is its own secret: a new encryption key keeps one patient's hash."""
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    hashes = []
    for keys, db in (([old], 'a.sqlite'), ([new, old], 'b.sqlite')):
        app = create_app(Settings(db_path=str(tmp_path / db), fernet_keys=keys, audit_secret='s3cret',
                                  source='fixture', fixture_path=str(FIXTURE), rate_per_minute=1000))
        key = app.state.store.add_key('c', {'redact'})
        with TestClient(app) as client:
            client.post('/v1/redact', json={'profiles': ['prompt'], 'patient_id': 1001, 'content': TEXT},
                        headers={'Authorization': f'Bearer {key}'})
        row = app.state.store._db.execute("SELECT patient_hash FROM audit WHERE action = 'redact'").fetchone()
        hashes.append(row[0])
    assert hashes[0] and hashes[0] == hashes[1]
