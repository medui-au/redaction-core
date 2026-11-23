"""0.2.1: a host calls the rule classes in the Redactor's context, and can drop its caches.
Invented practice only."""
from pathlib import Path

import pytest

from bps_redaction import Redactor, StaticLists
from bps_redaction.adapters.fixture import FixtureSource
from bps_redaction.redaction import RedactionService as R

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'


def test_rule_classes_need_a_context():
    with pytest.raises(RuntimeError):
        R.get_identifiers(1001)


def test_context_runs_a_single_pass_like_the_profile():
    r = Redactor(FixtureSource(FIXTURE))
    v = r.vault('k')
    text = 'Marigold Quillfeather rang.'
    with r.context():
        ids = R.get_identifiers(1001)
        one = R.redact(text, ids, v, patterns=R.get_identifier_patterns(1001))
    assert 'Quillfeather' not in one
    assert one == r.prompt(text, 1001, r.vault('k'))


def test_forget_applies_a_list_change_at_once():
    lists = StaticLists({'always_redact': []})
    r = Redactor(FixtureSource(FIXTURE), lists=lists)
    text = 'Seen at Platypus Hearing Clinic.'
    assert 'Platypus Hearing Clinic' in r.prompt(text, 1001, r.vault('k'))
    lists._tables['always_redact'] = [{'id': 1, 'string': 'Platypus Hearing Clinic',
                                       'label': 'Organisation', 'active': True}]
    assert 'Platypus Hearing Clinic' in r.prompt(text, 1001, r.vault('k'))   # still cached
    r.forget()
    assert 'Platypus Hearing Clinic' not in r.prompt(text, 1001, r.vault('k'))
