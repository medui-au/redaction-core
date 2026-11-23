"""0.2.2: a literal \\n, \\t or \\r is a word boundary (MedUI ADR-116). Invented practice only."""
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists
from bps_redaction.adapters.fixture import FixtureSource
from bps_redaction.checker.patterns import DETECTOR_VERSION
from bps_redaction.escapes import mask, unmask

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
BS = '\\'


@pytest.fixture
def redactor():
    lists = StaticLists({'name_variants': [{'name': 'marigold', 'form': 'goldie', 'active': True}]})
    return Redactor(FixtureSource(FIXTURE), lists=lists)


def test_versions():
    assert RULESET.startswith('medui-adr') and DETECTOR_VERSION.startswith('pattern-v')


@pytest.mark.parametrize('escape', ['n', 't', 'r'])
@pytest.mark.parametrize('name', ['Goldie', 'Marigold Quillfeather', 'Quillfeather'])
def test_a_name_after_a_literal_escape_is_redacted(redactor, escape, name):
    text = f'Seen.{BS}{escape}{name} rang.'
    v = redactor.vault('k')
    out = redactor.prompt(text, 1001, v)
    assert not any(w in out for w in name.split())
    assert f'{BS}{escape}[Redacted' in out                  # the escape itself is kept
    assert redactor.deredact(out, v.get_reverse_map()).text == text


def test_the_checker_finds_a_name_after_a_literal_escape(redactor):
    text = f'Seen.{BS}nGoldie rang.'
    hits = redactor.verdict(text, 1001)['hits']
    assert [text[h['start']:h['end']] for h in hits] == ['Goldie']


def test_record_and_input_profiles_too(redactor):
    v = redactor.vault('k')
    data, _ = redactor.record([{'note': f'Phoned.{BS}nGoldie well.'}], 1001, v)
    assert 'Goldie' not in data[0]['note'] and f'{BS}n[Redacted' in data[0]['note']
    out = redactor.input({'question': f'About{BS}tGoldie today'}, 1001, v)
    assert 'Goldie' not in out['question']


def test_tokens_never_hold_a_placeholder(redactor):
    v = redactor.vault('k')
    redactor.prompt(f'Address: 42 Wombat Hollow Road{BS}nMudgeeraba QLD 4213', 1001, v)
    assert not any(c in o for o in v.get_reverse_map().values() for c in '')


def test_an_escaped_backslash_is_not_an_escape():
    assert mask(f'a{BS}{BS}nb') == f'a{BS}{BS}nb'


def test_mask_keeps_length_and_round_trips():
    text = f'x{BS}nY{BS}tZ{BS}rW'
    assert len(mask(text)) == len(text) and unmask(mask(text)) == text


def test_text_already_holding_a_placeholder_is_left_alone():
    text = f'x{BS}nY'
    assert mask(text) == text


def test_rules_that_read_a_literal_newline_still_do(redactor):
    # A greeting ends at a literal \n (ADR-041), as a transcript utterance does (ADR-046):
    # while the rules run that \n is masked, and they must still see it.
    out = redactor.prompt(f'Email text:{BS}nHi Bartholomew{BS}n• Results attached', 1001, redactor.vault('k'))
    assert 'Bartholomew' not in out and '[Redacted Name:' in out
    spoken = redactor.prompt(f'[Speaker 1]: Come in, Bartholomew.{BS}n[Speaker 2]: Thanks.', 1001,
                             redactor.vault('k'))
    assert 'Bartholomew' not in spoken
