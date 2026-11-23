"""0.2.14: a curated list of common name forms, shipped with the core (MedUI ADR-148).

A patient called by a common short form (Michael -> Mick, Vicki -> Vicky) whose practice
has no preferred-name entries for it, and no listed form, kept that form in the text, and
the checker reported it as `outside_rules`. The curated list in data/name_forms.csv now
adds these forms for every first- and middle-name word. Invented people only.
"""
import copy
import csv
import json
from importlib import resources
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists, __version__
from bps_redaction.adapters.fixture import FixtureSource
from bps_redaction.firstnames import EVERYDAY_WORD_NAMES
from bps_redaction.nicknames import MIN_FORM_LENGTH, curated_forms

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'


def _redactor(first, middle='', lists=None):
    data = copy.deepcopy(json.loads(FIXTURE.read_text()))
    demo = data['patients']['1001']['demographics']
    demo.update({'FIRSTNAME': first, 'MIDDLENAME': middle, 'PREFERREDNAME': '',
                 'NAME': f'Mr {first} Quillfeather'})
    return Redactor(FixtureSource(data), lists=StaticLists(lists or {}))


def test_version():
    assert tuple(int(p) for p in __version__.split('.')) >= (0, 2, 14) and RULESET.startswith('medui-adr')


# ── the list itself ───────────────────────────────────────────────────────────

def _rows():
    with resources.files('bps_redaction').joinpath('data/name_forms.csv').open(newline='') as fh:
        return list(csv.DictReader(fh))


def test_the_list_ships_with_the_package():
    rows = _rows()
    assert len(rows) > 200
    assert {'name', 'form'} == set(rows[0])


def test_no_everyday_word_short_or_repeated_form():
    rows = _rows()
    pairs = [(r['name'], r['form']) for r in rows]
    assert len(pairs) == len(set(pairs))
    for name, form in pairs:
        assert name == name.strip().lower() and form == form.strip().lower()
        assert form != name and len(form) >= MIN_FORM_LENGTH
        assert form not in EVERYDAY_WORD_NAMES, form


@pytest.mark.parametrize('name,form', [('michael', 'mick'), ('vicki', 'vicky'), ('vicky', 'vicki'),
                                       ('elizabeth', 'libby'), ('sarah', 'sara'), ('lachlan', 'lachie')])
def test_common_forms_are_in(name, form):
    assert form in curated_forms()[name]


@pytest.mark.parametrize('form', ['will', 'may', 'mark', 'pat', 'sue', 'bill', 'drew', 'rose',
                                  # clinical abbreviations a form would match in any case
                                  'reg', 'cath', 'seb', 'dom', 'des', 'theo'])
def test_everyday_word_and_abbreviation_forms_are_not(form):
    assert not any(form in forms for forms in curated_forms().values())


# ── redaction ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('profile', ['record', 'prompt'])
@pytest.mark.parametrize('first,middle,text,form', [
    ('Michael', '', 'Mick rang about his bloods.', 'Mick'),
    ('Vicki', '', 'Seen with Vicky and her daughter.', 'Vicky'),
    ('Marigold', 'Elizabeth', 'Libby says the rash is better.', 'Libby'),   # a middle name
    ('Michael', '', "Mick's wife rang.", 'Mick'),
])
def test_a_curated_form_of_the_patients_name_is_redacted(profile, first, middle, text, form):
    r = _redactor(first, middle)
    v = r.vault('k')
    out = r.record({'t': text}, 1001, v)[0]['t'] if profile == 'record' else r.prompt(text, 1001, v)
    assert form not in out
    assert '[Redacted Name:' in out
    assert r.deredact(out, v.get_reverse_map()).text == text


def test_only_for_a_patient_with_that_name():
    r = _redactor('Rupert')
    assert 'Mick rang' in r.prompt('Mick rang about his bloods.', 1001, r.vault('k'))


def test_ordinary_word_forms_still_title_case_only():
    # "mick" is in the word list, so lower case is left, as for the practice's table.
    r = _redactor('Michael')
    out = r.prompt('Mick rang; taking the mick.', 1001, r.vault('k'))
    assert 'Mick rang' not in out and 'the mick' in out


def test_never_after_a_hyphen_joining_a_word():
    r = _redactor('Michael')
    assert 'X-Mick' in r.prompt('X-Mick result.', 1001, r.vault('k'))
    assert '2-Mick' in r.prompt('2-Mick result.', 1001, r.vault('k'))


@pytest.mark.parametrize('text', ['Plan:\n-Mick to bring his tablets.', 'Plan:\\n-Mick to bring them.',
                                  '-Mick rang.', 'Discussed with wife -Mick happy.'])
def test_a_dash_bullet_before_the_form_is_a_boundary(text):
    r = _redactor('Michael')
    v = r.vault('k')
    out = r.prompt(text, 1001, v)
    assert 'Mick' not in out and '[Redacted Name:' in out
    assert r.deredact(out, v.get_reverse_map()).text == text


def test_listed_forms_still_apply_alongside():
    r = _redactor('Michael', lists={'name_variants': [
        {'name': 'michael', 'form': 'mikko', 'active': True}]})
    out = r.prompt('Mikko and Mick both rang.', 1001, r.vault('k'))
    assert 'Mikko' not in out and 'Mick' not in out


def test_the_patients_recorded_name_is_not_a_form():
    # A Vicky's own first name is left to the patient pass (a patient token), not NM.
    r = _redactor('Vicky')
    out = r.prompt('Vicky rang.', 1001, r.vault('k'))
    assert 'Vicky' not in out and '[Redacted Patient:' in out


@pytest.mark.parametrize('text', ['Mick Thornbury rang.', 'Spoke with Mick Thornbury today.',
                                  'Thornbury, Mick rang.'])
def test_a_surname_beside_the_form_goes_with_it(text):
    r = _redactor('Michael')
    v = r.vault('k')
    out = r.prompt(text, 1001, v)
    assert 'Mick' not in out and 'Thornbury' not in out
    assert r.deredact(out, v.get_reverse_map()).text == text


@pytest.mark.parametrize('text,kept', [('Mick rang.', 'rang'), ('Mick Reviewed today.', 'Reviewed'),
                                       ('Mick and Ann came.', 'and')])
def test_not_a_surname_stays(text, kept):
    r = _redactor('Michael')
    out = r.prompt(text, 1001, r.vault('k'))
    assert 'Mick' not in out and kept in out


# ── the checker ───────────────────────────────────────────────────────────────

def test_the_checker_now_counts_the_form_as_covered():
    r = _redactor('Michael')
    hits = [h for h in r.verdict('Mick rang about his bloods.', 1001)['hits']
            if h['type'] == 'patient_name_variant']
    assert hits and all(h['coverage'] != 'outside_rules' for h in hits)


def test_redacted_text_is_clean_for_the_checker():
    r = _redactor('Michael')
    out = r.prompt('Mick rang about his bloods.', 1001, r.vault('k'))
    assert not [h for h in r.verdict(out, 1001)['hits'] if h['tier'] == 0]
