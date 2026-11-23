"""0.2.8: the recording clinician's own first name, in any case, in a transcript (MedUI ADR-133).

Invented names only. Fixture: patient 1001 is Marigold "Goldie" Quillfeather; practice
doctor Ophelia Wrenfield; Sam is on the practice's common first-name list.
"""
import re
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists
from bps_redaction.adapters.fixture import FixtureSource
from bps_redaction.checker.patterns import DETECTOR_VERSION
from bps_redaction.firstnames import EVERYDAY_WORD_NAMES, NOT_A_SURNAME

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
NM = re.compile(r'\[Redacted Name: NM-[0-9a-f]+\]')


@pytest.fixture(scope='module')
def redactor():
    return Redactor(FixtureSource(FIXTURE), lists=StaticLists({}))


def _input(r, text, names=('Ottilie',), vault=None, patient=1001):
    vault = vault or r.vault('k')
    return r.input({'raw_transcript': text}, patient, vault, clinician_first_names=names)['raw_transcript']


def test_version():
    assert RULESET.startswith('medui-adr') and DETECTOR_VERSION.startswith('pattern-v')


@pytest.mark.parametrize('text', [
    'oh hi ottilie how are you', 'thanks Ottilie, see you', 'OTTILIE: yes', "that was ottilie's idea",
    'uh ottilie here from the clinic',
])
def test_the_clinicians_name_in_any_case(redactor, text):
    out = _input(redactor, text)
    assert 'ottilie' not in out.lower() and NM.search(out)


def test_without_names_nothing_changes(redactor):
    text = 'oh hi ottilie how are you'
    assert _input(redactor, text, names=None) == text
    assert _input(redactor, text, names=()) == text


def test_lower_case_and_title_case_share_one_token(redactor):
    out = _input(redactor, 'hi ottilie. Thanks Ottilie.')
    assert len(set(NM.findall(out))) == 1


def test_a_preferred_name_and_a_multi_word_first_name(redactor):
    out = _input(redactor, 'tilly said hi, and so did Rosalind', names=('Ottilie Rosalind', 'Tilly'))
    assert 'tilly' not in out.lower() and 'Rosalind' not in out


@pytest.mark.parametrize('name,text', [
    ('Will', 'we will see how it goes'), ('Mark', 'a mark on the skin'), ('Pat', 'pat it dry'),
    ('Don', 'don gloves first'), ('May', 'it may be worse'), ('Ed', 'et al ed'),
    ('Max', 'the max dose'), ('Drew', 'drew blood today'),
])
def test_everyday_word_names_never_in_lower_case(redactor, name, text):
    assert _input(redactor, text, names=(name,)) == text


@pytest.mark.parametrize('name,text', [('Will', 'thanks Will, bye'), ('Mark', 'MARK: hello')])
def test_everyday_word_names_still_in_title_case_and_capitals(redactor, name, text):
    assert name.lower() not in _input(redactor, text, names=(name,)).lower()


def test_guard_list_has_no_ordinary_clinician_names():
    assert 'tony' not in EVERYDAY_WORD_NAMES and 'sam' not in EVERYDAY_WORD_NAMES


@pytest.mark.parametrize('text', ['x-ottilie', 'ottilie-x', 'ottilies', 'bottilie'])
def test_whole_words_only(redactor, text):
    assert _input(redactor, text) == text


def test_the_patients_own_name_keeps_its_label(redactor):
    # A clinician who shares the patient's first name: the patient label wins.
    out = _input(redactor, 'Marigold said her knee is sore', names=('Marigold',))
    assert '[Redacted Patient: PT-' in out and 'Marigold' not in out


def test_a_full_practitioner_name_stays_one_hcp_token(redactor):
    out = _input(redactor, 'Ophelia Wrenfield here, hi', names=('Ophelia',))
    assert '[Redacted HCP: HP-' in out and 'Ophelia' not in out and not NM.search(out)


def test_a_misheard_surname_goes_with_the_name(redactor):
    out = _input(redactor, 'Hello. Ottilie Pellowe here', names=('Ottilie',))
    assert 'Pellowe' not in out and out.startswith('Hello. [Redacted Name: NM-')


def test_a_filler_word_before_the_name_stays(redactor):
    for filler in ('Yeah', 'Okay', 'Oh', 'Hey', "G'day", 'Good', 'Exactly'):
        out = _input(redactor, f'{filler}, Ottilie, thanks')
        assert out.startswith(f'{filler}, [Redacted Name: NM-')


def test_a_contraction_after_the_name_stays(redactor):
    out = _input(redactor, "Ottilie I've got your results")
    assert out.endswith(" I've got your results") and 'Ottilie' not in out


def test_filler_words_are_not_surnames():
    assert {'yeah', 'okay', 'oh', 'um', 'uh', 'hey'} <= NOT_A_SURNAME


def test_the_sweep_keeps_a_filler_word_out_of_the_token(redactor):
    out = redactor.prompt('Yeah, Sam rang about it', None, redactor.vault('k'))
    assert out.startswith('Yeah, [Redacted Name: NM-')


def test_the_prompt_profile_is_unchanged_by_the_option(redactor):
    # Only input takes the clinician's names; prompt has no such argument.
    out = redactor.prompt('oh hi ottilie', 1001, redactor.vault('k'))
    assert 'ottilie' in out
