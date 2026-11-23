"""0.2.5: the practice's own name (MedUI ADR-121), a practitioner's name after an initial
(ADR-122, ADR-123), a month name after a number and a full stop (ADR-124). Invented practice only."""
import json
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists, deredact
from bps_redaction.adapters.fixture import FixtureSource
from bps_redaction.checker.patterns import DETECTOR_VERSION
from bps_redaction.redaction import practice_name_forms

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
UP = '[Redacted UsualPractice: UP-'


def _data() -> dict:
    data = json.loads(FIXTURE.read_text())
    # A contact whose surname is also a common first name, and one whose surname is not.
    data['hcp_practice_contacts'] += [
        {'CONTACTID': 104, 'FIRSTNAME': 'Hester', 'SURNAME': 'Ellery', 'CATEGORYNAME': 'Cardiology'},
        {'CONTACTID': 105, 'FIRSTNAME': 'Ivo', 'SURNAME': 'Quaverley', 'CATEGORYNAME': 'Surgery'},
    ]
    data['common_first_names'] += [{'name': 'ellery'}]
    return data


@pytest.fixture
def redactor():
    return Redactor(FixtureSource(_data()), lists=StaticLists({}))


def _record(redactor, text):
    return redactor.record({'text': text}, 1001, redactor.vault('k'))[0]['text']


def test_version():
    assert RULESET.startswith('medui-adr') and DETECTOR_VERSION.startswith('pattern-v')


# ── ADR-121: the practice's own name ─────────────────────────────────────────

TAKEN = [
    'Avonleigh and Brackenridge Medical Centres',
    'Brackenridge & Avonleigh Medical Centre',
    'Avonleigh Medical Centre',
    'Brackenridge Medical Centres',
    'AVONLEIGH MEDICAL CENTRE',
    'avonleigh medical center',
    'Avonleigh\nMedical  Centre',
    'Avonleigh Medical Centr',
    'Brackenridge Medical Cent.',
]
KEPT = [
    'Patient lives in Avonleigh.',
    'Admitted to Avonleigh Hospital.',
    'Avonleigh Pharmacy rang.',
    'Avonleigh Medical imaging booked.',
    'Avonleigh Medical Ce',
]


@pytest.mark.parametrize('text', TAKEN)
def test_each_form_of_the_name_is_one_token(redactor, text):
    for profile in (lambda t: redactor.prompt(t, 1001, redactor.vault('k')), lambda t: _record(redactor, t)):
        out = profile(text)
        assert out.startswith(UP) and out.count(UP) == 1, out
        assert 'Avonleigh' not in out and 'Brackenridge' not in out


@pytest.mark.parametrize('text', KEPT)
def test_a_town_alone_or_another_organisation_is_kept(redactor, text):
    assert UP not in redactor.prompt(text, 1001, redactor.vault('k'))


def test_the_and_possessive_and_digits_stay_outside(redactor):
    out = redactor.prompt("At the Avonleigh Medical Centre's front desk.", 1001, redactor.vault('k'))
    assert out.startswith('At the ' + UP) and out.endswith("]'s front desk.")
    out = redactor.prompt('Avonleigh Medical Centre0755509999', None, redactor.vault('k'))
    assert out.startswith(UP) and '[Redacted Practice: PR-' in out    # the number: the phone pass


def test_a_site_and_its_cut_or_capital_copy_share_one_token(redactor):
    vault = redactor.vault('k')
    out = redactor.prompt('Avonleigh Medical Centre; AVONLEIGH MEDICAL CENTR; Brackenridge Medical Centre',
                          1001, vault)
    tokens = [t for t in out.split('; ')]
    assert tokens[0] == tokens[1] != tokens[2]
    restored = deredact(out, vault.get_reverse_map()).text
    assert restored == 'Avonleigh Medical Centre; Avonleigh Medical Centre; Brackenridge Medical Centre'


def test_the_joined_name_restores_as_registered(redactor):
    vault = redactor.vault('k')
    out = redactor.prompt('From Avonleigh & Brackenridge Medical Centre.', 1001, vault)
    assert deredact(out, vault.get_reverse_map()).text == \
        'From Avonleigh and Brackenridge Medical Centres.'


def test_a_literal_line_break_inside_the_name(redactor):
    out = redactor.prompt('Letterhead: Avonleigh\\nMedical Centre', 1001, redactor.vault('k'))
    assert out.startswith('Letterhead: ' + UP) and out.count(UP) == 1 and 'Medical' not in out


def test_input_profile_takes_it_too(redactor):
    out = redactor.input({'note': 'Copy to Avonleigh Medical Centre'}, 1001, redactor.vault('k'))
    assert UP in out['note']


def test_the_checker_reports_a_leftover_form_at_tier_1(redactor):
    hits = redactor.verdict('Copy to Avonleigh Medical Centre', None)['hits']
    assert any(h['tier'] == 1 and h['kind'] == 'site_name' for h in hits), hits
    hits = redactor.verdict('Copy to ' + redactor.prompt('Avonleigh Medical Centre', None,
                                                         redactor.vault('k')), None)['hits']
    assert not any(h.get('kind') == 'site_name' for h in hits)


def test_an_always_redact_row_of_type_practice_name():
    lists = StaticLists({'always_redact': [
        {'id': 1, 'string': 'AvBrack Docs', 'label': 'practice name', 'active': True}]})
    r = Redactor(FixtureSource(_data()), lists=lists)
    assert r.prompt('Booked at AvBrack Docs.', 1001, r.vault('k')).startswith('Booked at ' + UP)


@pytest.mark.parametrize('name, location, forms', [
    ('Avonleigh', False, []),                                    # one word: a town
    ('Avonleigh', True, []),
    ('Avonleigh Family Practice', False, ['Avonleigh Family Practice']),
    ('The Avonleigh Surgery', False, ['Avonleigh Surgery']),
    ('Avonleigh Doctors Group', True, ['Avonleigh Doctors Group']),
    ('Main Building', True, []),                                 # a site label, no ending
    ('Dr Quill and Partners', False, ['Dr Quill and Partners']),  # no ending: as written
])
def test_forms_from_a_registered_name(name, location, forms):
    assert [clean for _, clean in practice_name_forms(name, location=location)] == forms


def test_three_sites():
    clean = [c for _, c in practice_name_forms('Avonleigh, Brackenridge and Coldwater Medical Centres')]
    assert clean.count('Avonleigh, Brackenridge and Coldwater Medical Centres') == 6
    assert clean[-3:] == ['Avonleigh Medical Centre', 'Brackenridge Medical Centre',
                          'Coldwater Medical Centre']


# ── ADR-122: a surname, an initial and a name ─────────────────────────────────

@pytest.mark.parametrize('text, kept', [
    ('Seen by Dr Ellery J Quaverley today.', 'Seen by Dr '),       # first-name surname
    ('Seen by Dr Quokkaby J Thistlewood today.', 'Seen by Dr '),   # other surname
    ('Letter from Ellery J. Thistlewood, cardiologist.', 'Letter from '),
    ('Seen by Dr Rose A QUAVERLEY today.', 'Seen by Dr '),          # capitals: in the book
])
def test_the_token_takes_the_initial_and_the_name(redactor, text, kept):
    vault = redactor.vault('k')
    out = redactor.record({'text': text}, 1001, vault)[0]['text']
    assert out.startswith(kept + '[Redacted HCP: HP-')
    assert 'Quaverley' not in out and 'Thistlewood' not in out and 'QUAVERLEY' not in out
    assert deredact(out, vault.get_reverse_map()).text == text


def test_an_ordinary_word_after_the_initial_is_left(redactor):
    out = _record(redactor, 'Seen by Dr Rose A Little today.')
    assert out.startswith('Seen by Dr [Redacted HCP: HP-') and out.endswith('] A Little today.')


def test_an_address_book_surname_after_the_initial_is_taken_even_if_a_word():
    data = _data()
    data['hcp_practice_contacts'].append(
        {'CONTACTID': 106, 'FIRSTNAME': 'Una', 'SURNAME': 'Little', 'CATEGORYNAME': 'Surgery'})
    r = Redactor(FixtureSource(data), lists=StaticLists({}))
    out = r.record({'text': 'Seen by Dr Rose A Little today.'}, 1001, r.vault('k'))[0]['text']
    assert 'Little' not in out


@pytest.mark.parametrize('text', [
    'Seen by Dr Rose A LITTLE today.',
    'Paracetamol 1g, Quokkaby A NOCTE',                # a capitalised word that is no name
])
def test_a_word_in_capitals_not_in_the_book_is_left(redactor, text):
    out = _record(redactor, text)
    assert out.endswith(text.split(' A ')[1]) and ' A ' in out


def test_a_tail_on_the_next_line_is_not_taken(redactor):
    out = _record(redactor, 'Seen by Dr Quokkaby\nA Thistlewood rang.')
    assert out.endswith('\nA Thistlewood rang.')


# ── ADR-123: an initial and an ordinary-word surname, no title ────────────────

@pytest.mark.parametrize('text', [
    'Reported by\nJ Small FRANZCR\nRadiology',
    'Reported by\nJ. Small, FRANZCR',
    'Reported by:\nJ Small\nRadiologist',
    'Reported by:\\nJ Small\\nRadiologist',        # literal escapes in a data block
])
def test_initial_and_ordinary_word_surname_with_post_nominal_or_alone(redactor, text):
    out = _record(redactor, text)
    assert '[Redacted HCP: HP-' in out and 'Small' not in out


@pytest.mark.parametrize('text', [
    'a J Small lesion was seen',
    'Small myometrial cysts.',
    'Impression: J small effusion',
    'Seen with J Small and family at home today',
])
def test_initial_and_word_in_running_text_is_kept(redactor, text):
    assert _record(redactor, text) == text


# ── ADR-124: a number and a full stop are not a day before a month ────────────

@pytest.mark.parametrize('text', [
    'Reviewed 12/03/21. June reports less pain.',
    'BP 120/80. June is walking daily.',
    'BP 120/80.June is walking daily.',
    'Seen 5/6. June has no new symptoms.',
    'Pain score 7. June says it is worse.',
])
def test_a_month_name_after_a_number_and_a_stop_is_a_name(text):
    from bps_redaction.months import month_in_date
    i = text.index('June')
    assert not month_in_date(text, i, i + 4)


@pytest.mark.parametrize('text', [
    'Seen 12 June.', 'Seen 12th of June.', 'Seen 3/Jun.', 'Seen 3-Jun.', 'Due 12. June 2026.',
])
def test_a_day_before_a_month_still_makes_a_date(text):
    from bps_redaction.months import month_in_date
    i = text.index('Jun')
    assert month_in_date(text, i, i + (4 if text[i:i + 4] == 'June' else 3))


def test_a_patient_called_june_after_a_reading_is_redacted():
    data = _data()
    data['patients']['1001']['demographics']['PREFERREDNAME'] = 'June'
    r = Redactor(FixtureSource(data), lists=StaticLists({}))
    out = r.prompt('BP 120/80. June is walking daily.', 1001, r.vault('k'))
    assert 'June' not in out and out.startswith('BP 120/80. [Redacted Patient: PT-')
