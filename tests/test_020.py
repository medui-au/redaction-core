"""0.2.0: MedUI parity (ADR-083 to ADR-112) on invented data."""
import json
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists
from bps_redaction.adapters.fixture import FixtureSource
from bps_redaction.checker.scan import hits, is_blocking, summarise

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'


@pytest.fixture
def redactor():
    return Redactor(FixtureSource(FIXTURE))


def kinds(r, text, patient_id=1001):
    return {(h['tier'], h['type']) for h in hits(r.check(text, patient_id))}


def test_ruleset():
    assert RULESET.startswith('medui-adr')


# ── tier 0 (v21) ────────────────────────────────────────────────────────────

def test_suburb_with_postcode_and_state(redactor):
    assert (0, 'patient_address') in kinds(redactor, 'She lives in Mudgeeraba QLD 4213 now.')


def test_phone_digits_in_any_spacing(redactor):
    assert (0, 'patient_phone') in kinds(redactor, 'Rang from 049-157 0156 yesterday.')


def test_street_with_hyphen_between_words(redactor):
    assert (0, 'patient_address') in kinds(redactor, 'Visited 42 Wombat-Hollow Road.')


# ── tier 2 (v21) ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text,kind', [
    ('call 0412  345  678 today', 'mobile_number'),
    ('mob 0412 - 345 - 678', 'mobile_number'),
    ('ph (07)   5463   1234', 'landline_number'),
])
def test_phone_groups_with_wider_gaps(redactor, text, kind):
    assert (2, kind) in kinds(redactor, text, None)


def test_labelled_identifier_from_built_in_labels(redactor):
    assert (2, 'labelled_identifier') in kinds(redactor, 'UR: 4471923 seen in clinic', None)
    assert (2, 'labelled_identifier') not in kinds(redactor, 'UR: 12-03-2024', None)   # a date


def test_practice_row_switches_a_default_label_off():
    r = Redactor(FixtureSource(FIXTURE), lists=StaticLists(
        {'identifier_labels': [{'label': 'UR', 'shape': '', 'active': 'false'}]}))
    assert (2, 'labelled_identifier') not in kinds(r, 'UR: 4471923', None)


# ── ADR-083: the clinical-phrase guard looks at the same line only ────────

def test_surname_ending_a_line_before_a_clinical_word(redactor):
    vault = redactor.vault('t')
    out = redactor.prompt('Seen with Quillfeather\nBlood pressure taken.', 1001, vault)
    assert 'Quillfeather' not in out


# ── verdict, pause rule, card summary, fingerprint ──────────────────────────

def test_verdict_format_and_pause_rule(redactor):
    v = redactor.verdict('Goldie wrote from goldie.q@example.com.', 1001)
    assert set(v) == {'detector_version', 'lists', 'scanned_ms', 'top_tier', 'hits', 'patient_identifiers'}
    from bps_redaction.checker.patterns import DETECTOR_VERSION
    assert v['detector_version'] == DETECTOR_VERSION and v['top_tier'] == 0
    assert v['patient_identifiers'] == 'loaded' and is_blocking(v)
    clean = redactor.verdict('Nothing identifying here.', None)
    assert clean['top_tier'] is None and not is_blocking(clean) and clean['patient_identifiers'] == 'none'
    assert not is_blocking({'error': 'TimeoutError'})


def test_summary_counts_by_tier_type_coverage(redactor):
    card = summarise(redactor.verdict('Goldie, goldie.q@example.com, Goldie.', 1001))
    assert sum(f['count'] for f in card['findings']) == len(card['hits'])
    assert summarise(None) is None and summarise({'error': 'x'}) is None


def test_fingerprint_follows_the_practice_lists():
    a = Redactor(FixtureSource(FIXTURE)).fingerprint()
    b = Redactor(FixtureSource(FIXTURE), lists=StaticLists(
        {'identifier_labels': [{'label': 'Pt ID', 'shape': '', 'active': 'true'}]})).fingerprint()
    assert len(a) == len(b) == 8 and a != b


# ── host rows: extra practitioner names (e.g. preferred names) ────────────

def test_a_host_may_add_practitioner_rows_with_their_own_id(tmp_path):
    data = json.loads(FIXTURE.read_text())
    doctor = data['practice_doctors'][0]
    extra = dict(doctor, user_id=f"{doctor['user_id']}p", first_name='Wren', surname='Tallowmere')
    data['practice_doctors'] = data['practice_doctors'] + [extra]
    path = tmp_path / 'practice.json'
    path.write_text(json.dumps(data))
    r = Redactor(FixtureSource(path))
    vault = r.vault('t')
    out = r.prompt(f"Seen by Dr Wren Tallowmere and Dr {doctor['first_name']} {doctor['surname']}.", None, vault)
    assert 'Tallowmere' not in out and doctor['surname'] not in out
