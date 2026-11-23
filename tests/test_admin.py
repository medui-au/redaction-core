"""The /admin pages, on invented data: who may log in, and what an admin may do."""

import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from bps_redaction_service import Settings, create_app

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'


@pytest.fixture
def svc(tmp_path):
    settings = Settings(db_path=str(tmp_path / 'svc.sqlite'), fernet_keys=[Fernet.generate_key().decode()],
                        source='fixture', fixture_path=str(FIXTURE), rate_per_minute=1000)
    app = create_app(settings)
    app.state.store.add_admin('setup', 'local-admin-password')
    with TestClient(app) as client:
        client.store = app.state.store
        yield client


def login(svc, kind, user, password):
    return svc.post('/admin/login', data={'kind': kind, 'user': user, 'password': password},
                    follow_redirects=False)


def csrf(svc, page='/admin/keys'):
    return re.search(r'name="csrf" value="([^"]+)"', svc.get(page).text).group(1)


def test_pages_need_a_login(svc):
    for path in ('/admin/flags', '/admin/keys', '/admin/flags/f_x'):
        r = svc.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers['location'] == '/admin/login'


def test_login_lists_only_admin_groups(svc):
    page = svc.get('/admin/login').text
    assert 'Brambleby' in page and 'Thistlewood' in page      # groups 3 and 1
    assert 'Hollowmere' not in page                            # a nurse, group 5


@pytest.mark.parametrize('kind,user,password', [
    ('bp', '901', 'owner-pass-901'), ('bp', '902', 'manager-pass-902'),
    ('local', 'setup', 'local-admin-password')])
def test_admins_log_in(svc, kind, user, password):
    r = login(svc, kind, user, password)
    assert r.status_code == 303 and r.headers['location'] == '/admin/flags'
    cookie = r.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie
    assert svc.get('/admin/flags').status_code == 200


@pytest.mark.parametrize('kind,user,password', [
    ('bp', '901', 'wrong'), ('bp', '903', 'nurse-pass-903'),   # right password, not an admin group
    ('local', 'setup', 'wrong'), ('local', '901', 'owner-pass-901'), ('bp', 'setup', 'local-admin-password')])
def test_others_do_not(svc, kind, user, password):
    r = login(svc, kind, user, password)
    assert r.status_code == 200 and 'don’t match' in r.text
    assert svc.get('/admin/flags', follow_redirects=False).status_code == 303


def test_repeated_failures_lock_the_name(svc):
    for _ in range(5):
        login(svc, 'local', 'setup', 'wrong')
    r = login(svc, 'local', 'setup', 'local-admin-password')
    assert 'Too many failed attempts' in r.text


def test_any_admin_makes_and_revokes_keys(svc):
    login(svc, 'bp', '902', 'manager-pass-902')
    page = svc.post('/admin/keys', data={'name': 'clinic-app', 'scopes': ['redact', 'deredact'],
                                         'csrf': csrf(svc)}).text
    key = re.search(r'<div class="key">([^<]+)</div>', page).group(1)
    assert svc.get('/v1/info', headers={'Authorization': f'Bearer {key}'}).status_code == 200
    svc.post('/admin/keys/revoke', data={'name': 'clinic-app', 'csrf': csrf(svc)})
    assert svc.get('/v1/info', headers={'Authorization': f'Bearer {key}'}).status_code == 401
    actions = [(a['action'], a['actor']) for a in svc.store.recent_audit('admin_key')]
    assert ('admin_key_revoke', 'Percival Thistlewood (BP 902)') in actions


def test_forms_need_the_session_csrf_token(svc):
    login(svc, 'local', 'setup', 'local-admin-password')
    r = svc.post('/admin/keys', data={'name': 'sneaky', 'scopes': ['redact'], 'csrf': 'forged'})
    assert r.status_code == 400 and not any(k['name'] == 'sneaky' for k in svc.store.list_keys())


def test_review_a_flag_with_its_hits_highlighted(svc):
    key = svc.store.add_key('client', {'redact'})
    # The checker flags what survives: practice staff, whom the prompt profile leaves alone.
    r = svc.post('/v1/redact', json={'profiles': ['prompt'], 'patient_id': 1001,
                                     'content': 'Seen by Barnaby Tumblewick today.'},
                 headers={'Authorization': f'Bearer {key}'}).json()
    flag_id = r['flags']['flag_id']
    login(svc, 'local', 'setup', 'local-admin-password')
    page = svc.get(f'/admin/flags/{flag_id}').text
    assert '<mark' in page
    token = csrf(svc, f'/admin/flags/{flag_id}')
    svc.post(f'/admin/flags/{flag_id}/review', data={'disposition': 'true_leak', 'note': 'n', 'csrf': token})
    flag = svc.store.get_flag(flag_id)
    assert flag['disposition'] == 'true_leak' and flag['reviewer'] == 'setup'


def test_logout_ends_the_session(svc):
    login(svc, 'local', 'setup', 'local-admin-password')
    svc.post('/admin/logout', data={'csrf': csrf(svc)})
    assert svc.get('/admin/keys', follow_redirects=False).status_code == 303


def test_local_admin_passwords_are_hashed(svc):
    with pytest.raises(ValueError):
        svc.store.add_admin('short', 'too-short')
    row = svc.store._db.execute("SELECT password_hash FROM admin_users WHERE name = 'setup'").fetchone()
    assert row[0].startswith('scrypt$') and 'local-admin-password' not in row[0]


def test_keys_page_explains_itself(svc):
    login(svc, 'local', 'setup', 'local-admin-password')
    page = svc.get('/admin/keys').text
    assert 'popovertarget="keys-help"' in page and '<div id="keys-help" popover>' in page
    assert '<script' not in page
