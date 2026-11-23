"""The checker, on invented data: what survived redaction, by tier."""

from pathlib import Path

import pytest

from bps_redaction import Redactor
from bps_redaction.adapters.fixture import FixtureSource
from bps_redaction.checker.scan import hits

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'


@pytest.fixture
def redactor():
    return Redactor(FixtureSource(FIXTURE))


def kinds(redactor, text, patient_id=1001):
    return {(h['tier'], h['type']) for h in hits(redactor.check(text, patient_id))}


def test_tier0_the_patients_own_identifiers(redactor):
    found = kinds(redactor, 'Goldie wrote from goldie.q@example.com.')
    assert (0, 'patient_name') in found and (0, 'patient_email') in found


def test_tier0_next_of_kin_phone(redactor):
    assert (0, 'patient_nok_phone') in kinds(redactor, 'Her son rang on 0491 570 157.')


def test_tier1_practice_staff_by_full_name(redactor):
    assert (1, 'practice_contact_name') in kinds(redactor, 'Seen by Barnaby Tumblewick today.')


def test_tier2_shapes_that_could_be_anyone(redactor):
    found = kinds(redactor, 'Call 0491 570 158 or Dr Jones at 12 Banksia Street.', None)
    assert {(2, 'mobile_number'), (2, 'titled_name'), (2, 'street_address')} <= found


def test_tokens_are_masked(redactor):
    text = 'Seen by Dr [Redacted HCP: HP-0123456789ab] Imaging Services; [Redacted Patient: PT-0123456789ab].'
    assert kinds(redactor, text) == set()


def test_redacted_output_is_clean(redactor):
    vault = redactor.vault('t')
    out = redactor.prompt('Marigold Quillfeather, goldie.q@example.com, 0491 570 156.', 1001, vault)
    assert not [h for h in hits(redactor.check(out, 1001)) if h['tier'] == 0]


def test_hits_carry_offsets_not_text(redactor):
    for h in hits(redactor.check('Goldie wrote from goldie.q@example.com.', 1001)):
        assert set(h) == {'tier', 'type', 'kind', 'coverage', 'start', 'end'}   # coverage since 0.2.0
        assert h['coverage'] in (None, 'missed_by_rules', 'outside_rules')


def coverage(redactor, text, patient_id=1001):
    return {f['coverage'] for f in redactor.check(text, patient_id) if f['tier'] == 0}


def test_coverage_a_string_redaction_removes(redactor):
    # Redaction builds 14/03/1957: finding it in checked text is a bug.
    assert coverage(redactor, 'Born 14/03/1957.') == {'missed_by_rules'}


def test_coverage_a_string_redaction_leaves(redactor):
    # 'Mar 14, 1957' is a rendering only the checker looks for.
    assert coverage(redactor, 'Born Mar 14, 1957.') == {'outside_rules'}


def test_checker_searches_what_redaction_builds(redactor):
    # A next-of-kin first name alone: redaction's builder makes it, the
    # checker's own renderings don't.
    assert (0, 'patient_nok_name') in kinds(redactor, 'Rupert drove her in.')
