"""0.2.4: a name that is also a month is kept when it is part of a date (MedUI ADR-118). Invented practice only."""
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists
from bps_redaction.adapters.fixture import FixtureSource
from bps_redaction.checker.patterns import DETECTOR_VERSION
from bps_redaction.redaction import RedactionService
from bps_redaction.vault import TokenVault

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'

DATES = [
    'Seen 12 May 2026.',
    'Seen 12th of May.',
    'HbA1c 8.4% (May 2026); stable.',
    'Next review May 12, 2026.',
    "Started May '26.",
    'Due in May for bloods.',
    'Stable since May.',
    'Plan for early May.',
    'Seen 3 Jan 2026.',
    'Results from Jan. 2026 attached.',
    'Seen 3/Jan/2026.',
]
NAMES = [
    'May rang about the results.',
    "May's daughter rang.",
    'Referred to May at the clinic.',
    'Confirm with May.',
    "Plan: Review Jan's leg lesion.",
    'Jan attended alone.',
    'Seen in May’s absence.',
]


@pytest.fixture
def redactor():
    lists = StaticLists({'name_variants': [
        {'name': 'marigold', 'form': 'may', 'active': True},
        {'name': 'marigold', 'form': 'jan', 'active': True},
    ]})
    return Redactor(FixtureSource(FIXTURE), lists=lists)


def test_version():
    assert RULESET.startswith('medui-adr') and DETECTOR_VERSION.startswith('pattern-v')


@pytest.mark.parametrize('text', DATES)
def test_a_nickname_month_in_a_date_is_kept(redactor, text):
    assert redactor.prompt(text, 1001, redactor.vault('k')) == text


@pytest.mark.parametrize('text', NAMES)
def test_a_nickname_month_used_as_a_name_is_redacted(redactor, text):
    out = redactor.prompt(text, 1001, redactor.vault('k'))
    assert '[Redacted Name:' in out and 'May' not in out and 'Jan' not in out


@pytest.mark.parametrize('text', DATES)
def test_the_patients_own_month_name_in_a_date_is_kept(text):
    assert RedactionService.redact(text, ['May', 'Jan', 'Exampleton'], TokenVault('k')) == text


@pytest.mark.parametrize('text', NAMES)
def test_the_patients_own_month_name_is_still_redacted(text):
    out = RedactionService.redact(text, ['May', 'Jan', 'Exampleton'], TokenVault('k'))
    assert '[Redacted Patient:' in out and 'May' not in out and 'Jan' not in out


def test_only_a_whole_month_match_is_kept():
    text = 'May Exampleton seen 12 May 2026.'
    out = RedactionService.redact(text, ['May Exampleton', 'May'], TokenVault('k'))
    assert out.endswith('seen 12 May 2026.') and 'Exampleton' not in out


@pytest.mark.parametrize('text', DATES)
def test_the_checker_does_not_report_a_kept_date(redactor, text):
    assert redactor.verdict(text, 1001)['hits'] == []


@pytest.mark.parametrize('text', ['Rang May about the results.', 'Confirm with May.', 'Jan attended alone.'])
def test_the_checker_still_reports_the_name(redactor, text):
    hits = redactor.verdict(text, 1001)['hits']
    assert [text[h['start']:h['end']] for h in hits] in (['May'], ['Jan'])
