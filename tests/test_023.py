"""0.2.3: a shortened name (practice table: Marigold -> Mari) in single quotes or with a possessive is redacted (MedUI ADR-117). Invented practice only."""
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists
from bps_redaction.adapters.fixture import FixtureSource

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'


@pytest.fixture
def redactor():
    lists = StaticLists({'name_variants': [
        {'name': 'marigold', 'form': 'don', 'active': True},     # an ordinary word: Title case only
    ]})
    return Redactor(FixtureSource(FIXTURE), lists=lists)

from bps_redaction.adapters.fixture import FixtureSource  # noqa: E402

def test_version():
    assert RULESET.startswith('medui-adr')

@pytest.mark.parametrize('text', [
    'Call her "Mari" at reviews.',
    'Call her “Mari” at reviews.',
    "Call her 'Mari' at reviews.",
    'Call her ‘Mari’ at reviews.',
    "'Mari' rang.",
    "Mari's daughter rang.",
    'Mari’s daughter rang.',
    "The daughters are Mari's.",
    "Ask for Don's number.",
    "Ask for 'Don'.",
])
def test_the_shortened_name_is_redacted(redactor, text):
    v = redactor.vault('k')
    out = redactor.prompt(text, 1001, v)
    assert not any(w in out.replace('[Redacted', '') for w in ('Mari', 'Don'))
    assert '[Redacted Name:' in out   # the nickname pass, not the patient pass
    assert redactor.deredact(out, v.get_reverse_map()).text == text

@pytest.mark.parametrize('text', [
    "Don't stop the tablets.",
    "Don’t stop the tablets.",
    "O'Mari is a different word.",
    "Mari'smith is one word.",
    'X-Mari-Y stays.',
    'Marigold Quillfeather is the name on file.',
])
def test_an_apostrophe_joining_letters_still_blocks_it(redactor, text):
    out = redactor.prompt(text, 1001, redactor.vault('k'))
    assert '[Redacted Name:' not in out
