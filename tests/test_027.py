"""0.2.7: a phone label typed straight onto the number (MedUI ADR-131). Invented numbers only.

Fixture patient 1001: home 07 5550 1234, mobile 0491 570 156; next of kin 0491 570 157.
"""
import re
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists
from bps_redaction.adapters.fixture import FixtureSource
from bps_redaction.checker.patterns import DETECTOR_VERSION
from bps_redaction.checker.scan import hits

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
TOKEN = re.compile(r'\[Redacted [A-Za-z]+: [A-Z]+-[0-9a-f]+\]')


@pytest.fixture(scope='module')
def redactor():
    return Redactor(FixtureSource(FIXTURE), lists=StaticLists({}))


def _prompt(r, text):
    return r.prompt(text, 1001, r.vault('k'))


def test_version():
    assert RULESET.startswith('medui-adr') and DETECTOR_VERSION.startswith('pattern-v')


@pytest.mark.parametrize('text,label', [
    ('ph0491570156', 'ph'), ('Ph0491 570 156', 'Ph'), ('mob0491570156', 'mob'),
    ('m0491570156', 'm'), ('call0491570156', 'call'), ('(ph0491570156)', '(ph'),
    ('ph55501234', 'ph'), ('ph0755501234', 'ph'), ('NOK mob0491570157', 'NOK mob'),
])
def test_the_patients_own_number_after_glued_letters(redactor, text, label):
    out = _prompt(redactor, text)
    assert out.startswith(label + '[Redacted Patient: PT-') and not re.search(r'\d', TOKEN.sub('', out))


@pytest.mark.parametrize('text', [
    'ph0412345678', 'Ph0412 345 678', 'mobile0412-345-678', 'tel(07) 5550 4321',
    'ph0755504321', 'ph55504321', 'fax+61 7 5550 4321'])
def test_anyones_number_after_a_glued_phone_label(redactor, text):
    out = _prompt(redactor, text)
    assert '[Redacted Phone: PH-' in out and not re.search(r'\d', TOKEN.sub('', out))


@pytest.mark.parametrize('text', ['AB0412345678', 'REF0412345678', 'graph0412345678', 'x55504321',
                                  'Lot55504321', 'ID 1230412345678'])
def test_other_letters_or_digits_in_front_still_mean_a_code(redactor, text):
    assert _prompt(redactor, text) == text


@pytest.mark.parametrize('text,kind', [
    ('ph0412345678', 'mobile_number'), ('mob0412 345 678', 'mobile_number'),
    ('ph0755504321', 'landline_number')])
def test_checker_sees_anyones_number_after_a_glued_label(redactor, text, kind):
    assert (2, kind) in {(h['tier'], h['type']) for h in hits(redactor.check(text, None))}


def test_checker_leaves_a_code(redactor):
    assert not hits(redactor.check('REF0412345678', None))
