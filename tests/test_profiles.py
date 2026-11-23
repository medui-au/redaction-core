"""Behaviour tests on invented data."""

import json
import re
from pathlib import Path

import pytest

from bps_redaction import IdentitySourceUnavailable, Redactor, StaticLists
from bps_redaction.adapters.fixture import FixtureSource

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
TOKEN = re.compile(r'\[Redacted [A-Za-z]+: [A-Z]+-[0-9a-f]+\]')


@pytest.fixture
def redactor():
    return Redactor(FixtureSource(FIXTURE))


def round_trip(redactor, text, vault):
    return redactor.deredact(text, vault.get_reverse_map()).text


def test_tokens_are_stable_per_key_and_differ_across_keys():
    a1, a2, b = Redactor.vault('k1'), Redactor.vault('k1'), Redactor.vault('k2')
    assert a1.mint_patient('Marigold') == a2.mint_patient('Marigold')
    assert a1.mint_patient('Marigold') != b.mint_patient('Marigold')


def test_patient_identifiers_in_a_prompt_round_trip(redactor):
    vault = redactor.vault('t')
    text = 'Marigold Quillfeather (goldie.q@example.com, 0491 570 156) reports chest pain.'
    out = redactor.prompt(text, 1001, vault)
    assert 'Quillfeather' not in out and 'goldie.q' not in out and '0491' not in out
    assert round_trip(redactor, out, vault) == text


def test_titled_practitioner_keeps_title_outside_token(redactor):
    # ADR-054: the title stays in the text, so de-redaction restores it.
    vault = redactor.vault('t')
    text = 'Reviewed by Dr Small. Small myometrial cysts. Referred to Mrs. Rose; BP rose to 140.'
    out = redactor.input({'question': text}, 1001, vault)['question']
    assert out.startswith('Reviewed by Dr [Redacted HCP: ')
    assert 'Small myometrial cysts' in out and 'BP rose to 140' in out
    assert round_trip(redactor, out, vault) == text


def test_ordinary_word_surname_needs_a_title(redactor):
    vault = redactor.vault('t')
    out = redactor.input({'q': 'small bowel follow-up with Quokkaby'}, 1001, vault)['q']
    assert out.startswith('small bowel')
    assert 'Quokkaby' not in out


def test_control_values_are_left_alone(redactor):
    out = redactor.input({'mode': 'issues_list'}, 1001, redactor.vault('t'))
    assert out == {'mode': 'issues_list'}


def test_input_fails_closed_without_practitioners():
    redactor = Redactor(FixtureSource({'patients': {}}))
    with pytest.raises(IdentitySourceUnavailable):
        redactor.input({'q': 'Seen by Dr Wrenfield today'}, None, redactor.vault('t'))


def test_record_without_a_source_reports_no_identifiers():
    blocks, loaded = Redactor().record([{'Note': 'nothing to match'}], 1001, Redactor.vault('t'))
    assert loaded is False


def test_record_redacts_patient_practice_and_contacts(redactor):
    vault = redactor.vault('t')
    blocks = [{'Demographics': {'Name': 'Marigold Quillfeather', 'DOB': '14/03/1957'}},
              {'Letter': 'Dear Dr Brindlecombe, please see Mrs Quillfeather. '
                         'Practice phone 07 5550 9999. INTERNALID 1001.'}]
    out, loaded = redactor.record(blocks, 1001, vault)
    text = json.dumps(out)
    assert loaded
    for leaked in ('Quillfeather', 'Brindlecombe', '5550 9999', '1957'):
        assert leaked not in text


def test_always_redact_list_uses_its_label():
    redactor = Redactor(FixtureSource(FIXTURE), lists=StaticLists({'always_redact': [
        {'id': 1, 'string': 'Sunnybank Sleep Clinic', 'label': 'clinic', 'active': True}]}))
    vault = redactor.vault('t')
    out = redactor.prompt('Booked at Sunnybank Sleep Clinic.', 1001, vault)
    assert '[Redacted Clinic: ' in out
    assert round_trip(redactor, out, vault) == 'Booked at Sunnybank Sleep Clinic.'


def test_patient_label_wins_over_practitioner(redactor):
    # ADR-052: a name that is the patient's and a contact's gets the patient's label.
    vault = redactor.vault('t')
    out = redactor.input({'q': 'Marigold called about her results'}, 1001, vault)['q']
    assert out.startswith('[Redacted Patient: ')


def test_deredact_never_returns_a_raw_token(redactor):
    restored = redactor.deredact('see [Redacted HCP: HP-0123456789ab] and [Redacted Thing]', {})
    assert not TOKEN.search(restored.text)


def test_general_prompt_without_patient(redactor):
    vault = redactor.vault('t')
    out = redactor.prompt('Email reception@example.org or someone@example.net', None, vault)
    assert '@' not in out


def test_demographics_read_once_per_patient():
    """Many documents for one patient: one BP demographics read, not one per pass per document."""
    calls = []
    src = FixtureSource(FIXTURE)
    original = src.query
    src.query = lambda name, params=None: (calls.append(name), original(name, params))[1]
    r = Redactor(src)
    for i in range(10):
        r.record({'text': f'Marigold Quillfeather, Mudgeeraba 4213, document {i}.'}, 1001, r.vault('k'))
    assert calls.count('patient_get_demographics') == 1
