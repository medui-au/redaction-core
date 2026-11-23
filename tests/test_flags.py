"""The flag report, /v1/verify and the flag store (plan D7), on invented data."""

import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from bps_redaction_service import Settings, create_app

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
# The prompt profile has no practitioner pass, so a staff member's name in a
# prompt gets through: what the checker is for.
LEAKY = 'Nurse notes by Barnaby Tumblewick.'


@pytest.fixture
def svc(tmp_path):
    settings = Settings(db_path=str(tmp_path / 's.sqlite'), fernet_keys=[Fernet.generate_key().decode()],
                        source='fixture', fixture_path=str(FIXTURE), rate_per_minute=1000)
    app = create_app(settings)
    store = app.state.store
    with TestClient(app) as client:
        client.store = store
        client.keys = {'client': store.add_key('client', {'redact', 'deredact', 'verify'}),
                       'reviewer': store.add_key('reviewer', {'flags'})}
        yield client


def h(svc, who):
    return {'Authorization': f'Bearer {svc.keys[who]}'}


def redact(svc, text):
    return svc.post('/v1/redact', json={'patient_id': 1001, 'profiles': ['prompt'], 'content': text},
                    headers=h(svc, 'client')).json()


def test_clean_output_reports_no_flag(svc):
    flags = redact(svc, 'Marigold Quillfeather is improving.')['flags']
    assert flags == {'checked': True, 'flagged': False, 'top_tier': None, 'hits': [], 'more': 0}


def test_a_leak_is_flagged_with_offsets_into_the_returned_content(svc):
    r = redact(svc, LEAKY)
    f = r['flags']
    assert f['flagged'] and f['top_tier'] == 1 and f['flag_id'].startswith('f_')
    hit = f['hits'][0]
    assert r['content'][hit['start']:hit['end']] == 'Barnaby Tumblewick'
    assert set(hit) == {'type', 'tier', 'start', 'end'}           # no matched text echoed


def test_reviewer_sees_the_flag_in_context_then_reviews_it(svc):
    flag_id = redact(svc, LEAKY)['flags']['flag_id']
    assert svc.get(f'/v1/flags/{flag_id}', headers=h(svc, 'client')).status_code == 403
    flag = svc.get(f'/v1/flags/{flag_id}', headers=h(svc, 'reviewer')).json()
    assert flag['content'] == LEAKY and flag['disposition'] == 'unreviewed'
    r = svc.post(f'/v1/flags/{flag_id}/review', json={'disposition': 'true_leak', 'note': 'staff name'},
                 headers=h(svc, 'reviewer')).json()
    assert r['disposition'] == 'true_leak' and r['reviewer'] == 'reviewer'
    listed = svc.get('/v1/flags?disposition=true_leak', headers=h(svc, 'reviewer')).json()['flags']
    assert [f['id'] for f in listed] == [flag_id]
    assert svc.get('/v1/flags?disposition=unreviewed', headers=h(svc, 'reviewer')).json()['flags'] == []


def test_flagged_content_goes_with_its_session_but_the_flag_stays(svc):
    r = redact(svc, LEAKY)
    svc.delete(f'/v1/sessions/{r["session_id"]}', headers=h(svc, 'client'))
    flag = svc.get(f'/v1/flags/{r["flags"]["flag_id"]}', headers=h(svc, 'reviewer')).json()
    assert flag['content'] is None
    assert flag['counts'] == {'practice_contact_name': 1} and flag['top_tier'] == 1


def test_flags_past_their_keeping_time_are_purged(svc):
    flag_id = redact(svc, LEAKY)['flags']['flag_id']
    svc.store._db.execute('UPDATE flags SET at = ? WHERE id = ?', (time.time() - 91 * 86400, flag_id))
    svc.store.purge_expired(flag_days=90)
    assert svc.get(f'/v1/flags/{flag_id}', headers=h(svc, 'reviewer')).status_code == 404


def test_flagged_content_is_encrypted_at_rest(svc, tmp_path):
    redact(svc, LEAKY)
    svc.store._db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    assert b'Tumblewick' not in (tmp_path / 's.sqlite').read_bytes()


def test_summary_counts(svc):
    redact(svc, LEAKY)
    redact(svc, LEAKY)
    s = svc.get('/v1/flags/summary', headers=h(svc, 'reviewer')).json()
    assert s['flags'] == 2 and s['by_type'] == {'practice_contact_name': 2}
    assert s['by_top_tier'] == {'1': 2} and s['by_disposition'] == {'unreviewed': 2}


def test_verify_checks_without_storing(svc):
    r = svc.post('/v1/verify', json={'content': 'Goldie wrote from goldie.q@example.com', 'patient_id': 1001},
                 headers=h(svc, 'client')).json()
    assert {(x['tier'], x['type']) for x in r['hits']} >= {(0, 'patient_name'), (0, 'patient_email')}
    assert svc.get('/v1/flags', headers=h(svc, 'reviewer')).json()['flags'] == []


def test_verify_needs_its_scope(svc):
    assert svc.post('/v1/verify', json={'content': 'x'}, headers=h(svc, 'reviewer')).status_code == 403


def test_a_failing_checker_never_blocks_redaction(svc, monkeypatch):
    from bps_redaction import Redactor
    monkeypatch.setattr(Redactor, 'check', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    r = redact(svc, LEAKY)
    assert r['flags'] == {'checked': False} and r['content'] == LEAKY
