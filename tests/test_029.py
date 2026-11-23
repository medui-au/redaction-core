"""0.2.9: the practice's own users' first names in the first-name sweep, opt-in (MedUI ADR-134).

Invented names, places and accounts only.
"""
import re

import pytest

from bps_redaction import RULESET, Redactor, StaticLists
from bps_redaction import firstnames
from bps_redaction.redaction import RedactionService

NM = re.compile(r'\[Redacted Name: NM-[0-9a-f]+\]')

ROWS = {
    'practice_doctors': [
        {'user_id': 1, 'first_name': 'Thaddeus', 'surname': 'Quarrington'},
        {'user_id': 2, 'first_name': '', 'surname': 'Dr. Ysolde Brackenbury'},     # whole name in surname
        {'user_id': 3, 'first_name': 'H. A.', 'surname': 'Wenlock (K)'},           # initials only
        {'user_id': 4, 'first_name': 'Will', 'surname': 'Ashdown'},                # everyday-word name
        {'user_id': 5, 'first_name': 'Martin', 'surname': 'Fallowes'},             # ordinary word
    ],
    'practice_staff': [
        {'user_id': 10, 'first_name': 'Zephyrine', 'surname': 'Oakhurst', 'role': 'Receptionist'},
        {'user_id': 11, 'first_name': 'Telehealth', 'surname': 'Bookings', 'role': 'Nurse'},
        {'user_id': 12, 'first_name': 'Fluvax', 'surname': 'Northvale', 'role': 'Practice nurse'},
        {'user_id': 13, 'first_name': 'Quillon', 'surname': 'Baker', 'role': 'Practice nurse'},
    ],
    'practice_locations': [{'CITY': 'Northvale', 'ADDRESS1': '1 Invented St'}],
    # Quillon is held by one patient: a person, though the surname is an ordinary word.
    'patient_first_name_counts': [{'first_name': 'Quillon', 'n': 1}, {'first_name': 'Sam', 'n': 40}],
}


class Source:
    def __init__(self, rows=ROWS, fail=()):
        self.rows, self.fail = rows, fail

    def query(self, name, params=None):
        if name in self.fail:
            raise RuntimeError('down')
        return list(self.rows.get(name, []))


def _sweep(r, text):
    with r.context():
        return RedactionService.redact_common_first_names(text, r.vault('k'))


@pytest.fixture
def on():
    return Redactor(Source(), lists=StaticLists({}), practice_first_names=True)


def test_ruleset():
    assert RULESET.startswith('medui-adr')


def test_off_by_default_nothing_changes():
    r = Redactor(Source(), lists=StaticLists({}))
    text = 'Seen by Dr THADDEUS today. Thanks, Zephyrine.'
    assert _sweep(r, text) == text
    with r.context():
        assert firstnames.practice_names() == frozenset()


@pytest.mark.parametrize('text,name', [
    ('Seen by Dr THADDEUS [Redacted HCP: HP-0] today', 'THADDEUS'),
    ('Thanks, Zephyrine.', 'Zephyrine'),
    ('Letter from Dr Ysolde', 'Ysolde'),          # first name taken from the surname field
    ('Quillon took the bloods', 'Quillon'),
])
def test_practice_first_names_are_swept(on, text, name):
    out = _sweep(on, text)
    assert name not in out and NM.search(out)


def test_the_list(on):
    with on.context():
        got = firstnames.practice_names()
    assert got == {'thaddeus', 'ysolde', 'zephyrine', 'quillon'}


@pytest.mark.parametrize('text', [
    'Telehealth review next week', 'Follow-up via telehealth', 'Fluvax given today',
    'Will review in a week', 'Martin Fallowes',    # ordinary words stay with the admin list
    'Dr H. A. Wenlock',
])
def test_left_out(on, text):
    assert _sweep(on, text) == text


def test_the_admin_list_still_takes_a_name_out():
    r = Redactor(Source(), lists=StaticLists({'first_names': [{'name': 'Zephyrine', 'include': False}]}),
                 practice_first_names=True)
    assert _sweep(r, 'Thanks, Zephyrine.') == 'Thanks, Zephyrine.'


def test_the_admin_list_still_adds_an_ordinary_word():
    r = Redactor(Source(), lists=StaticLists({'first_names': [{'name': 'Martin', 'include': True}]}),
                 practice_first_names=True)
    assert 'Martin' not in _sweep(r, 'Sincerely, Martin')


def test_source_down_means_no_practice_names_and_no_error():
    r = Redactor(Source(fail=('practice_staff',)), lists=StaticLists({}), practice_first_names=True)
    assert _sweep(r, 'Thanks, Zephyrine.') == 'Thanks, Zephyrine.'


def test_user_first_names():
    assert firstnames.user_first_names('', 'Dr. Ysolde Brackenbury (K)') == (['Ysolde'], 'Brackenbury')
    assert firstnames.user_first_names('Mary-Jo Anne', 'Pellow') == (['Mary-Jo', 'Anne'], 'Pellow')
    assert firstnames.user_first_names('H. A.', 'Wenlock (K)') == ([], 'Wenlock')
    assert firstnames.user_first_names('', '') == ([], '')


def test_a_cache_from_before_held_names_is_rebuilt():
    from bps_redaction.interfaces import MemoryCache
    cache = MemoryCache()
    cache.set('common_first_name_list', '{"built_at": "2099-01-01T00:00:00+00:00", "names": ["sam"]}')
    r = Redactor(Source(), lists=StaticLists({}), cache=cache, practice_first_names=True)
    with r.context():
        assert 'quillon' in firstnames.held_names()
    assert '"held"' in cache.get('common_first_name_list')


def test_the_option_is_in_the_fingerprint_only_when_on():
    lists = StaticLists({'first_names': [{'name': 'Sam', 'include': True}]})
    off = Redactor(Source(), lists=lists).fingerprint()
    on = Redactor(Source(), lists=lists, practice_first_names=True).fingerprint()
    assert off != on and off == Redactor(Source(), lists=lists).fingerprint()
