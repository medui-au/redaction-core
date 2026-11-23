"""0.2.10: a practice user's 4-letter first name is also swept in lower case (MedUI ADR-135).

Invented names only.
"""
import re

import pytest

from bps_redaction import RULESET, Redactor, StaticLists, __version__
from bps_redaction.redaction import RedactionService

NM = re.compile(r'\[Redacted Name: NM-[0-9a-f]+\]')

ROWS = {
    'practice_doctors': [{'user_id': 1, 'first_name': 'Quen', 'surname': 'Harrowgate'},
                         {'user_id': 2, 'first_name': 'Yaz', 'surname': 'Penderel'}],
    'practice_staff': [{'user_id': 10, 'first_name': 'Zephyrine', 'surname': 'Oakhurst', 'role': 'Nurse'}],
    'practice_locations': [{'CITY': 'Northvale'}],
    # Ysko: a patients' first name only, not a practice user's.
    'patient_first_name_counts': [{'first_name': 'Ysko', 'n': 40}, {'first_name': 'Quen', 'n': 2}],
}


class Source:
    def query(self, name, params=None):
        return list(ROWS.get(name, []))


def _sweep(r, text):
    with r.context():
        return RedactionService.redact_common_first_names(text, r.vault('k'))


@pytest.fixture
def on():
    return Redactor(Source(), lists=StaticLists({}), practice_first_names=True)


def test_version():
    assert __version__.startswith('0.2.') and RULESET.startswith('medui-adr')


@pytest.mark.parametrize('text', ['how is quen going', 'ring quen next week', 'Thanks, Quen.', 'QUEN'])
def test_a_practice_users_four_letter_name_in_any_case(on, text):
    out = _sweep(on, text)
    assert 'quen' not in out.lower() and NM.search(out)


def test_longer_names_as_before(on):
    assert NM.search(_sweep(on, 'zephyrine rang'))


@pytest.mark.parametrize('text', [
    'ysko came in',       # 4 letters, a patients' name only: Title case and capitals only
    'yaz came in',        # 3 letters, even for a practice user
])
def test_left_in_lower_case(on, text):
    assert _sweep(on, text) == text


def test_patients_name_still_with_a_capital(on):
    assert 'Ysko' not in _sweep(on, 'Ysko came in')


def test_off_nothing_changes():
    r = Redactor(Source(), lists=StaticLists({}))
    assert _sweep(r, 'how is quen going') == 'how is quen going'


def test_the_admin_list_still_takes_it_out():
    r = Redactor(Source(), lists=StaticLists({'first_names': [{'name': 'Quen', 'include': False}]}),
                 practice_first_names=True)
    assert _sweep(r, 'how is quen going') == 'how is quen going'
