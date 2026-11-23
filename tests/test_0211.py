"""0.2.11: a patient's name with an s straight after it (MedUI ADR-141).

"Marigolds' daughter" (a possessive written s') and "Marigolds daughter" (no
apostrophe) went through whole: the name guard wanted no letter after the name.
The checker had the same blind spot. Invented people only (tests/fixtures/practice.json).
"""
import json
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists, __version__, deredact
from bps_redaction.adapters.fixture import FixtureSource
from bps_redaction.checker.patterns import DETECTOR_VERSION

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
PT = '[Redacted Patient: PT-'
NM = '[Redacted Name: NM-'


@pytest.fixture
def r():
    return Redactor(FixtureSource(json.loads(FIXTURE.read_text())), lists=StaticLists({}))


def _prompt(r, text, vault=None):
    return r.prompt(text, 1001, vault or r.vault('k'))


def test_version():
    assert __version__.startswith('0.2.') and RULESET.startswith('medui-adr') and DETECTOR_VERSION.startswith('pattern-v')


@pytest.mark.parametrize('text, name', [
    ("Marigolds' daughter rang.", 'Marigold'),        # first name, an ordinary word: Title case
    ("Marigolds’ daughter rang.", 'Marigold'),        # curly apostrophe
    ("MARIGOLDS' results", 'MARIGOLD'),                # capitals
    ("Marigolds daughter rang.", 'Marigold'),         # no apostrophe at all
    ("Elspeths' sister", 'Elspeth'),                  # middle name
    ("Quillfeathers' address changed.", 'Quillfeather'),  # surname
    ("the quillfeathers came in", 'quillfeather'),     # surname, any case
    ("Goldies' bloods", 'Goldie'),                    # preferred name
    ("Ruperts' phone", 'Rupert'),                     # next of kin
])
def test_the_name_goes_and_the_s_stays(r, text, name):
    out = _prompt(r, text)
    assert name.lower() not in out.lower() and PT in out
    assert ']s' in out or ']S' in out


def test_one_token_with_or_without_the_s(r):
    vault = r.vault('k')
    out = _prompt(r, "Marigold's and Marigolds' notes", vault)
    first, second = out.split(' and ')
    assert first.split(']')[0] == second.split(']')[0]
    assert deredact(out, vault.get_reverse_map()).text == "Marigold's and Marigolds' notes"


def test_a_practice_nickname_too(r):
    out = _prompt(r, "Maris' bloods and Maris’ bloods")
    assert 'mari' not in out.lower() and out.count(NM) == 2


def test_a_nicknames_bare_s_that_makes_a_word_stays(r):
    # "Mari" matches in any case, and "maris" is an ordinary word: a bare s is left.
    assert _prompt(r, 'Maris bloods') == 'Maris bloods'


@pytest.mark.parametrize('text', [
    'Started on marigolds tea.',      # lower case: the ordinary word, as "marigold" is
    'Marigoldsmith rang',             # a longer word is not the name
    "Quillfeathersmith's",
])
def test_kept(r, text):
    assert _prompt(r, text) == text


def test_the_checker_finds_both_forms(r):
    for text in ("Marigolds' daughter rang.", 'Marigolds daughter rang.', "Quillfeathers' address"):
        hits = r.verdict(text, 1001)['hits']
        assert any(h['tier'] == 0 for h in hits), text


def test_the_checker_is_clean_after_redaction(r):
    out = _prompt(r, "Marigolds' daughter and the Quillfeathers rang.")
    assert not [h for h in r.verdict(out, 1001)['hits'] if h['tier'] == 0]


def test_the_checker_leaves_the_ordinary_word(r):
    assert not [h for h in r.verdict('Started on marigolds tea.', 1001)['hits'] if h['tier'] == 0]


# ── v29: the patient's names glued to a letter or digit (checker only) ───────

def _glued(r, text):
    return [h for h in r.verdict(text, 1001)['hits'] if h['tier'] == 0]


@pytest.mark.parametrize('text', [
    'Seen xQuillfeather today',       # a letter before
    'Ref Quillfeather2 on file',      # a digit after
    'MARIGOLDQUILLFEATHER',           # two names run together: the surname is found
    'Elspethquillfeather rang',       # glued to the middle name
])
def test_a_glued_name_pauses(r, text):
    hits = _glued(r, text)
    assert hits and all(h['tier'] == 0 for h in hits)
    from bps_redaction.checker.scan import is_blocking
    assert is_blocking(r.verdict(text, 1001))


@pytest.mark.parametrize('text', [
    'Started on marigoldx tea',       # Marigold is an ordinary word: whole word only, as before
    'Quillfeather rang',              # whole word: today's finding, not a glued one
])
def test_not_a_glued_finding(r, text):
    assert not [h for h in _glued(r, text) if h.get('variant_form', '').endswith('_glued')]


def test_the_glued_finding_says_redaction_does_not_take_it(r):
    hits = [h for h in r.verdict('xQuillfeather', 1001)['hits'] if h['tier'] == 0]
    assert hits and all(h['coverage'] == 'outside_rules' for h in hits)


def test_short_names_and_ordinary_runs_are_not_searched_glued():
    from bps_redaction.checker.identifiers import _glued_names
    ids = [{'value': 'Anne', 'kind': 'name', 'variant_form': 'first', 'match_mode': 'any'},
           {'value': 'Pellow', 'kind': 'name', 'variant_form': 'surname', 'match_mode': 'any'},
           {'value': 'Ottilie', 'kind': 'name', 'variant_form': 'middle', 'match_mode': 'any'},
           {'value': 'Quenby', 'kind': 'name', 'variant_form': 'preferred', 'match_mode': 'any'}]
    # Anne: 4 letters. Ottilie: a middle name. Quenbys: glued only by an s, which the
    # whole-word search takes (0.2.11), so not here; Quenby9 is glued.
    text = 'xanne said; pellowx; xottilie; Quenby9'
    hits = _glued_names(text, text.lower(), 0, ids, [])
    assert [text[h['match_start']:h['match_end']] for h in hits] == ['pellow', 'Quenby']
