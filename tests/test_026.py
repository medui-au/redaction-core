"""0.2.6: the practice's own name keeps its label in a shared vault (MedUI ADR-126), and more
of the forms scans make of it (ADR-127). Invented practice only."""
import json
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists, deredact
from bps_redaction.adapters.fixture import FixtureSource
from bps_redaction.checker.patterns import DETECTOR_VERSION

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
UP = '[Redacted UsualPractice: UP-'


@pytest.fixture
def redactor():
    data = json.loads(FIXTURE.read_text())
    # An address-book organisation entered under the practice's own site name.
    data['hcp_practice_contacts'].append({'CONTACTID': 110, 'FIRSTNAME': '',
                                          'SURNAME': 'Avonleigh Medical Centre',
                                          'CATEGORYNAME': 'Medical practice'})
    return Redactor(FixtureSource(data), lists=StaticLists({}))


def _record(r, text, vault=None):
    return r.record({'text': text}, 1001, vault or r.vault('k'))[0]['text']


def test_version():
    assert RULESET.startswith('medui-adr') and DETECTOR_VERSION.startswith('pattern-v')


# ── ADR-126 ──────────────────────────────────────────────────────────────────

def test_the_practice_label_wins_in_a_vault_shared_across_documents(redactor):
    vault = redactor.vault('patient')
    _record(redactor, 'Seen today.', vault)               # the HCP pass pre-mints the contact
    out = _record(redactor, 'Letter from Avonleigh Medical Centre.', vault)
    assert out.startswith('Letter from ' + UP) and 'HCP' not in out
    assert deredact(out, vault.get_reverse_map()).text == 'Letter from Avonleigh Medical Centre.'


def test_the_patient_label_still_wins_over_everything():
    from bps_redaction.vault import TokenVault
    v = TokenVault('k')
    patient = v.mint_patient('Avonleigh Medical Centre')
    assert v.mint_usual_practice('Avonleigh Medical Centre') == patient


# ── ADR-127 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text, rest', [
    ('Avonleigh Medical Centre² note', '² note'),       # superscript digit
    ('Avonleigh Medical Centre⁵', '⁵'),
    ('Avonleigh Medical Centra', ''),                             # OCR endings
    ('Avonleigh Medical Centro.', '.'),
    ('Avonleigh Medical Centry', ''),
    ('Avonleigh Medical CentreAppointments', 'Appointments'),    # a capital straight after
    ('AVONLEIGH MEDICAL CENTREAPPOINTMENTS', 'APPOINTMENTS'),
])
def test_more_scanned_forms(redactor, text, rest):
    out = _record(redactor, text)
    assert out.startswith(UP) and out.endswith(rest) and 'avonleigh' not in out.lower()


def test_one_stray_letter_before_a_capitalised_town(redactor):
    assert _record(redactor, 'Dr Quokkaby\n  xAvonleigh Medical Centre').endswith('\n  x' + _record(
        redactor, 'Avonleigh Medical Centre'))


@pytest.mark.parametrize('text', [
    'Avonleigh Medical Central',          # an ordinary word, not a misread
    'AVONLEIGH MEDICAL CENTRAL',
    'Avonleigh Medical Centrexyz',        # lower case stuck on: another word
    'xavonleigh medical centre',          # the stray letter only before a capital
    'Lives in Avonleigh.',
])
def test_still_left_alone(redactor, text):
    assert UP not in _record(redactor, text)
