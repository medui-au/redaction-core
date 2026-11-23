"""One invented case per rule (docs/RULES.md): what must go, and what must stay.

Patients: 1001 Marigold Elspeth "Goldie" Quillfeather, 42 Wombat Hollow Road,
Mudgeeraba QLD 4213, DOB 14/03/1957, son Rupert; 1002 Mary-Jane Van Dyke;
1003 Ann Rose, who shares her name with a psychologist in the address book;
1004 Perpetua Haverstock, 17 O'Reilly Road; 1005 Cuthbert Pennywhistle,
9 Smith- Jones Road (both Tallebudgera QLD 4228); 1007 Odile Pemberly,
9 Old Mill Road, Farville QLD 4999 (the practice's postcode).
Practitioners: Peter Small (radiology), Ann Rose, Dr Ophelia Wrenfield (a
practice doctor), Barnaby Tumblewick (practice nurse). Everyone is invented,
and phone numbers are from the ACMA's fictitious ranges.
"""

import re
from pathlib import Path

import pytest

from bps_redaction import Redactor, StaticLists
from bps_redaction.adapters.fixture import FixtureSource

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
TOKEN = re.compile(r'\[Redacted ([A-Za-z]+): ([A-Z]+)-[0-9a-f]+\]')

LISTS = StaticLists({
    'always_redact': [{'string': 'Fernhollow Lodge', 'label': 'Place', 'active': 'true'}],
    'name_variants': [{'name': 'marigold', 'form': 'goldy', 'active': 'true'},
                      {'name': 'marigold', 'form': 'tilly', 'active': 'true'},
                      {'name': 'marigold', 'form': 'rose', 'active': 'true'}],
})


@pytest.fixture(scope='module')
def redactor():
    return Redactor(FixtureSource(FIXTURE), lists=LISTS)


def run(redactor, profile, text, patient_id=1001):
    vault = redactor.vault('rules')
    if profile == 'prompt':
        return redactor.prompt(text, patient_id, vault)
    return redactor.input({'text': text}, patient_id, vault)['text']


# (ADR, profile, patient, text, must go, must stay)
CASES = [
    ('018', 'prompt', 1001, 'Marigold Quillfeather was seen.', ['Quillfeather'], ['was seen']),
    ('020', 'prompt', 1001, 'Lives at 42 Wombat Hollow Road, Mudgeeraba QLD 4213 now.',
     ['Wombat', 'Mudgeeraba', '4213'], ['Lives at', 'now.']),
    ('021', 'prompt', 1001, 'Address: 42 Wombat Hollow Rd.', ['Wombat'], ['Address:']),
    ('022', 'prompt', 1001, 'Home (07) 5550 1234, mobile +61 491 570 156, or 0491 570156.',
     ['5550 1234', '491 570 156', '0491 570156'], ['Home', 'mobile']),
    ('023', 'prompt', 1001, 'Our rooms: 7 Example Lane, phone 07 5550 9999.',
     ['Example Lane', '5550 9999'], ['Our rooms']),
    ('024', 'input', 1001, 'Small cell carcinoma noted. Reviewed by Peter Small.',
     ['Peter Small'], ['Small cell carcinoma']),
    ('025', 'prompt', 1001, 'Drinks marigold tea. Marigold is well.', ['Marigold is'], ['marigold tea']),
    ('026', 'prompt', 1001, 'Seen by Dr Zebedee Fantoccini last week.', ['Fantoccini'], ['last week']),
    ('028', 'prompt', 1001, 'Her daughter is on 0491 570 158, daughter@example.net.',
     ['0491 570 158', 'daughter@example.net'], ['Her daughter']),
    ('030', 'prompt', 1001, 'Moving to Fernhollow Lodge next month.', ['Fernhollow'], ['next month']),
    ('032', 'prompt', 1001, 'Letter written to Jemima Puddleby re. results.', ['Puddleby'],
     ['Letter written to', 're. results']),
    ('033', 'prompt', 1001, 'Walks along Wombat Hollow Road daily.', ['Wombat'], ['[Redacted Address]', 'daily']),
    ('034', 'prompt', 1001, 'Card 2953 70156 2; her husband: Medicare 2123 45670 1.',
     ['70156', '45670'], ['her husband']),
    ('035', 'prompt', 1001, 'Rooms at 12 Banksia Street, Tallowood QLD 4998.',
     ['Banksia', 'Tallowood'], ['[Redacted Address]']),
    ('036', 'prompt', 1001, 'From Dr Katherine (Kate) Fantoccini and Dr Mary Anne Ollerenshaw.',
     ['Fantoccini', 'Ollerenshaw', 'Kate'], ['From', 'and']),
    ('037', 'prompt', 1001, 'Reviewed by Dr O. Wrenfield.', ['Wrenfield'], ['Reviewed by']),
    ('040', 'input', 1001, 'Dressing by Barnaby Tumblewick.', ['Tumblewick'], ['Dressing by']),
    ('041', 'prompt', 1001, 'Email to Jemima re appointment.\nHi Rosalind,\nThanks', ['Jemima', 'Rosalind'],
     ['re appointment', 'Thanks']),
    ('042', 'prompt', 1002, 'Mary Jane, MaryJane Van Dyke and Mary came in.',
     ['Mary Jane', 'MaryJane', 'Van Dyke', 'Mary came'], ['came in']),
    ('043', 'prompt', 1001, 'Images: https://viewer.example.com/view?guid=3f2a9c and http://localhost/r/7',
     ['viewer.example.com', 'localhost'], ['[Link removed]']),
    ('044', 'prompt', 1001, 'File QUILLFEATHER_MARIGOLD_19570314.pdf attached.',
     ['QUILLFEATHER', 'MARIGOLD', '19570314'], ['.pdf attached']),
    ('045', 'prompt', 1001, 'RE: MARIGOLD QUILLFEATHER', ['MARIGOLD', 'QUILLFEATHER'], ['RE:']),
    ('046', 'prompt', 1001, '[Doctor]: How are you going, Jemima?', ['Jemima'], ['How are you going']),
    ('047', 'prompt', 1001, 'Escort: daughter, DOB 01/02/1980.', ['01/02/1980'], ['[Redacted DOB]']),
    ('048', 'prompt', 1001, 'Mari says the knee is better.', ['Mari says'], ['the knee is better']),
    ('049', 'prompt', 1001, 'Goldy says hello.', ['Goldy'], ['says hello']),
    ('056', 'prompt', 1001, 'tilly and TILLY; Seen by Dr Tilly Fantoccini; rose to 140.',
     ['tilly', 'TILLY', 'Fantoccini'], ['rose to 140']),
    ('050', 'prompt', 1001, 'Write to ruby9@gmr or ruby nine at gmail dot com.',
     ['ruby9@gmr', 'gmail'], ['Write to']),
    ('051', 'prompt', 1001, 'Medicare 2953 70156 1 Exp 04/27.', ['70156', '04/27'], ['Medicare']),
    ('053', 'prompt', None, 'Dr Zebedee Fantoccini, someone@example.net.', ['Fantoccini', 'someone@'], []),
    ('054', 'input', 1001, 'Reviewed by Dr Small.', ['Small'], ['Dr [Redacted HCP:']),
    ('019', 'input', 1001, 'Discussed with Peter Small, radiology.', ['Peter Small'], ['radiology']),
]


@pytest.mark.parametrize('adr,profile,patient_id,text,gone,kept', CASES, ids=[c[0] for c in CASES])
def test_rule(redactor, adr, profile, patient_id, text, gone, kept):
    out = run(redactor, profile, text, patient_id)
    for g in gone:
        assert g not in out, f'ADR-{adr}: {g!r} survived in {out!r}'
    for k in kept:
        assert k in out, f'ADR-{adr}: {k!r} lost from {out!r}'


def test_031_record_number_never_reaches_the_model(redactor):
    rows = [{'INTERNALID': 1001, 'RESULTNAME': 'HbA1c', 'VALUE': '6.1'}]
    out, _ = redactor.record(rows, 1001, redactor.vault('rules'))
    assert 'INTERNALID' not in out[0] and out[0]['RESULTNAME'] == 'HbA1c'


def test_038_author_field(redactor):
    rows = [{'Author': 'Jemima Puddleby - Discharge summary', 'Summary': 'Stable.'}]
    out, _ = redactor.record(rows, 1001, redactor.vault('rules'))
    assert 'Puddleby' not in out[0]['Author'] and 'Discharge summary' in out[0]['Author']


def test_052_the_patients_label_wins(redactor):
    # Ann Rose is both patient 1003 and an address-book psychologist.
    out = run(redactor, 'input', 'Ann Rose rang about her results.', 1003)
    labels = {m.group(2) for m in TOKEN.finditer(out)}
    assert labels == {'PT'}, out


def test_055_intercapped_dictionary_surname_alone(redactor):
    # 'Vandyke' is a dictionary word; written VanDyke it matches as stored.
    out = run(redactor, 'prompt', 'Spoke to VanDyke today.', 1002)
    assert 'VanDyke' not in out


def test_every_case_round_trips(redactor):
    """De-redaction restores every reversible case: patient identifiers come back
    in the record's spelling ('Rd' may return as 'rd'), so compared ignoring case."""
    for adr, profile, patient_id, text, _gone, kept in CASES:
        if any(fixed in ' '.join(kept) for fixed in ('[Redacted Address]', '[Redacted DOB]', '[Link removed]')):
            continue
        vault = redactor.vault(f'rt-{adr}')
        out = (redactor.prompt(text, patient_id, vault) if profile == 'prompt'
               else redactor.input({'text': text}, patient_id, vault)['text'])
        assert redactor.deredact(out, vault.get_reverse_map()).text.lower() == text.lower(), f'ADR-{adr}'


def test_056_author_with_a_listed_first_name_goes_whole(redactor):
    rows = [{'Author': 'Tilly Fantoccini - Discharge summary'}]
    out, _ = redactor.record(rows, 1001, redactor.vault('rules'))
    assert 'Fantoccini' not in out[0]['Author'] and 'Discharge summary' in out[0]['Author']


def test_058_common_first_names_swept_last(redactor):
    # Linda (12 patients) and Joanne (15 + 2 as 'Joanne Mary') are on the list;
    # June is a month, Rose an ordinary word, Zephyrine held by too few.
    out = run(redactor, 'prompt', 'Linda rang; Joanne Fennimore visited, then Linda Has gone. '
                                  'Rose and Zephyrine came in June. linda too.', 1002)
    assert 'Linda' not in out and 'Fennimore' not in out and 'Joanne' not in out
    assert 'Has gone' in out and 'Rose' in out and 'Zephyrine' in out and 'June' in out
    assert 'linda too' not in out          # lower case too, 5+ letters (ADR-059)


def test_058_listed_names_add_and_remove():
    lists = StaticLists({'always_redact': [], 'name_variants': [],
                         'first_names': [{'name': 'Zephyrine', 'include': 'true'},
                                         {'name': 'linda', 'include': 'false'}]})
    r = Redactor(FixtureSource(FIXTURE), lists=lists)
    out = r.prompt('Linda and Zephyrine came.', 1001, r.vault('x'))
    assert 'Linda' in out and 'Zephyrine' not in out


def test_058_sweep_list_is_its_own(redactor):
    # The HCP pass keeps a smaller common-first-name set (ADR-039) in the same
    # environment; running it first must not shrink the sweep's list.
    run(redactor, 'input', 'Discussed with Peter Small.')
    out = run(redactor, 'prompt', 'Linda rang.', 1002)
    assert 'Linda' not in out


def test_059_surname_before_a_comma_and_what_stays(redactor):
    out = run(redactor, 'prompt', 'PUDDLEBY, LINDA attended. Thanks, Linda. Daughter, Linda came. '
                                  'sam said hi. AVA 0.9 cm2 and Ava came.', 1002)
    assert 'PUDDLEBY' not in out and 'LINDA' not in out
    assert 'Thanks, [Redacted Name' in out and 'Daughter, [Redacted Name' in out
    assert 'sam said' in out                # short names stay in lower case
    assert 'AVA 0.9 cm2' in out and 'Ava came' not in out


def test_060_suburb_with_postcode_and_labelled_postcode(redactor):
    # Patient 1001 lives in Mudgeeraba 4213.
    # (A suburb followed within a few characters by its postcode goes too, even
    # across a full stop: "Mudgeeraba. Postcode: 4213" — over-redaction, by choice.)
    out = run(redactor, 'prompt', 'Location: Mudgeeraba, 4213. Lives in Mudgeeraba.\n'
                                  'Postcode: 4213. QLD 4213. Hb 4213 units.')
    assert 'Location: [Redacted Address]' in out and 'Lives in Mudgeeraba' in out
    assert 'Postcode: [Redacted Address]' in out and 'QLD [Redacted Address]' in out
    assert 'Hb 4213 units' in out


def _provider_number(stem='049745', location='3'):
    total = sum(int(d) * w for d, w in zip(stem, (3, 5, 8, 4, 2, 1))) + int(location) * 6
    return stem + location + 'YXWTLKJHFBA'[total % 11]


def test_061_provider_numbers(redactor):
    good = _provider_number()
    bad = good[:-1] + ('A' if good[-1] != 'A' else 'B')
    vault = redactor.vault('pv')
    text = f'Provider No {good}; written short {good[1:]}; reference {bad}.'
    out = redactor.prompt(text, None, vault)
    assert good not in out and good[1:] not in out and bad in out
    assert '[Redacted Provider: PV-' in out
    assert redactor.deredact(out, vault.get_reverse_map()).text == text


def test_061_checker_flags_a_provider_number(redactor):
    from bps_redaction.checker.scan import hits
    good = _provider_number()
    assert 'provider_number' in {h['type'] for h in hits(redactor.check(f'Prov {good}', None))}


def test_062_an_empty_phone_fields_label_is_not_an_identifier(redactor):
    # Patient 1002's home and work phones are empty: BP returns them as '(H) '
    # and '(W) '.
    out = run(redactor, 'prompt', 'Mobile 0491 570 159 (M). Call (H) or (W) after 5.', 1002)
    assert '0491 570 159' not in out
    assert '(M)' in out and 'Call (H) or (W) after 5' in out


# The OREILLY ROAD, O'Reilly Road and SMITH-JONES ROAD cases passed before
# ADR-063 too (other rules catch them); they guard against a regression.
@pytest.mark.parametrize('patient_id,text,gone', [
    (1004, "Lives at 17 OREILLY ROAD now.", 'OREILLY'),
    (1004, "Lives at 17 O Reilly Rd now.", 'Reilly'),
    (1004, "Walks along OReilly Road daily.", 'OReilly'),
    (1004, "Lives at 17 O'Reilly Road now.", 'Reilly'),
    (1005, "Lives at 9 SMITH-JONES ROAD now.", 'SMITH'),
    (1005, "Lives at 9 Smith - Jones Rd now.", 'Smith'),
    (1005, "Walks along Smith Jones Road daily.", 'Smith'),
])
def test_063_street_punctuation(redactor, patient_id, text, gone):
    out = run(redactor, 'prompt', text, patient_id)
    assert gone not in out and '[Redacted ' in out, out


@pytest.mark.parametrize('patient_id,text,gone,kept', [
    # A number in front of the street name, and a suburb that is not theirs after it.
    (1004, 'Address: 12345 OREILLY ROAD, MUDGEE CREEK QLD 4229', ['12345', 'MUDGEE'], ['Address:']),
    (1004, 'RMB 12345 O Reilly Rd, Tallebudgera', ['12345', 'Reilly'], []),
    (1004, "Lives at 9 O'Reilly Road MUDGEE CREEK.", ['9 ', 'MUDGEE'], ['Lives at']),
    (1004, "9 O'Reilly Rd Hillview QLD 4211", ['Hillview', '4211'], []),
    # A field label after the suburb stays.
    (1004, "Lives O'Reilly Road MUDGEE CREEK PH 0491", ['MUDGEE'], ['PH 0491']),
    # "Dr" + initial after an address is a title, not Drive.
    (1004, 'Ref: 17 OREILLY ROAD HILLVIEW Dr Z. Fantoccini', ['HILLVIEW', 'Fantoccini'], ['Ref:']),
    (1002, 'Seen at 3 Banksia Street Dr J. Smith', ['Banksia', 'Smith'], ['Seen at']),
])
def test_064_address_edges(redactor, patient_id, text, gone, kept):
    out, _ = redactor.record({'t': text}, patient_id, redactor.vault('064'))
    for g in gone:
        assert g not in out['t'], out
    for k in kept:
        assert k in out['t'], out


@pytest.mark.parametrize('text,gone,kept', [
    ('Dr Zebedee Q. Fantoccini reviewed', ['Fantoccini', 'Q.'], ['reviewed']),
    ('Dr Mary J. Brown reviewed', ['Brown'], ['reviewed']),
    ('Dr J. Smith Cardiology', ['Smith'], ['Cardiology']),
    ('Seen by Dr Fantoccini I think', ['Fantoccini'], ['I think']),
    ('Dr Google said', [], ['Dr Google said']),
])
def test_065_titled_middle_initial(redactor, text, gone, kept):
    out = run(redactor, 'prompt', text, None)
    for g in gone:
        assert g not in out, out
    for k in kept:
        assert k in out, out


def test_065_checker_flags_a_surname_left_after_a_token(redactor):
    from bps_redaction.checker.scan import hits
    found = {h['type'] for h in hits(redactor.check('Seen by [Redacted Name: NM-0a1b2c3d4e5f] Q. Fantoccini.', None))}
    assert 'name_after_token' in found
    assert not hits(redactor.check('[Redacted Name: NM-0a1b2c3d4e5f] I think so.', None))


@pytest.mark.parametrize('text,gone,kept', [
    # The practice is at 7 Example Lane, Sampleton 4999.
    ('Our rooms\n7 Example Lane\nSAMPLETON  QLD  4999\nPh', ['SAMPLETON', '4999'], ['Our rooms', 'Ph']),
    ('Sampleton, Qld 4999', ['Sampleton'], []),
    ('Lives in Sampleton.', [], ['Lives in Sampleton.']),
    ('Hb 4999 units', [], ['Hb 4999 units']),
])
def test_066_practice_suburb_with_postcode(redactor, text, gone, kept):
    for profile in ('prompt', 'record'):
        if profile == 'prompt':
            out = run(redactor, 'prompt', text, 1001)
        else:
            out = redactor.record({'t': text}, 1001, redactor.vault('066'))[0]['t']
        for g in gone:
            assert g not in out, (profile, out)
        for k in kept:
            assert k in out, (profile, out)
        if gone:
            assert '[Redacted Practice: PR-' in out


@pytest.mark.parametrize('text,gone,kept', [
    # Patient 1007 lives in Farville, which shares the practice's postcode (Sampleton 4999).
    ('Former: 3 Hill St, Farville Sampleton 4999', ['Farville', 'Sampleton', '4999'], ['Former:']),
    ('Lives at Farville, Sampleton QLD 4999.', ['Farville', '4999'], ['Lives at']),
    ('Our rooms\n7 Example Lane\nSAMPLETON  QLD  4999\nPh', ['SAMPLETON', '4999'], ['Ph']),
    ('Lives in Sampleton.', [], ['Lives in Sampleton.']),
])
def test_067_patient_sharing_the_practice_postcode(redactor, text, gone, kept):
    for profile in ('prompt', 'record'):
        out = (run(redactor, 'prompt', text, 1007) if profile == 'prompt'
               else redactor.record({'t': text}, 1007, redactor.vault('067'))[0]['t'])
        for g in gone:
            assert g not in out, (profile, out)
        for k in kept:
            assert k in out, (profile, out)


def test_067_practice_locality_with_no_patient(redactor):
    out = run(redactor, 'prompt', 'Visit us: Sampleton QLD 4999.', None)
    assert 'Sampleton' not in out and '[Redacted Practice: PR-' in out


@pytest.mark.parametrize('text,gone,kept', [
    # Patient 1001: home 07 5550 1234, mobile 0491 570 156; next of kin 0491 570 157.
    ('Ph 5550  1234 |', ['5550', '1234'], ['Ph']),
    ('Home (07)  5550   1234', ['5550', '1234'], ['Home']),
    ('NOK mob 0491  570  157', ['570', '157'], ['NOK mob']),
    ('Mob +61 491 570 156', ['570', '156'], ['Mob']),
    # Anyone's area-coded or mobile number, typed with two spaces.
    ('Clinic 07  5550  4321', ['4321'], ['Clinic']),
    ('Mob 0412  345  678', ['345', '678'], []),
    # Not joined: two 4-digit values in a table, or across a line.
    ('Plt  2345  6789', [], ['2345  6789']),
    ('5550\n1234 units', [], ['5550\n1234']),
])
def test_068_phone_numbers_in_any_spacing(redactor, text, gone, kept):
    out = redactor.record({'t': text}, 1001, redactor.vault('068'))[0]['t']
    for g in gone:
        assert g not in out, out
    for k in kept:
        assert k in out, out


@pytest.mark.parametrize('space', [' ', ' ', ' ', '\t'])
def test_069_house_number_after_any_space(redactor, space):
    # A former address on the patient's street (1001: 42 Wombat Hollow Road), the
    # number followed by a no-break space as RTF and HTML conversions write it.
    text = f'Previously:\n17{space}Wombat Hollow Road Otherton QLD 4888\n'
    out = redactor.record({'t': text}, 1001, redactor.vault('069'))[0]['t']
    assert '17' not in out and 'Wombat' not in out, out


def test_070_titled_name_after_an_earlier_token(redactor):
    # The practice locality becomes a token earlier in the text. The address
    # guard used to read its window at the wrong offset after a token, and
    # for some layouts turned the name away.
    for pad in range(40):
        text = ('Re: 14 Example Ridge Sampleton QLD 4999 '
                + 'x' * pad + '\nDr Anna Kowal\n')
        out = redactor.record({'t': text}, 1001, redactor.vault('070'))[0]['t']
        assert 'Kowal' not in out, (pad, out)


def test_068_composite_fields_still_match_whole():
    # BP's composite strings must still go as one token: the any-spacing phone
    # patterns run after the literal identifiers, not before.
    import copy
    import json
    data = json.loads(FIXTURE.read_text())
    demo = copy.deepcopy(data['patients']['1001']['demographics'])
    demo.update(NOK='Rupert Quillfeather 0491570157 Brother', PHONE_MOBILE='(M) 0491570156')
    data['patients']['1001']['demographics'] = demo
    r = Redactor(FixtureSource(data))
    out = r.record({'NOK': demo['NOK'], 'PHONE_MOBILE': demo['PHONE_MOBILE']}, 1001, r.vault('068c'))[0]
    assert re.fullmatch(r'\[Redacted [A-Za-z ]+: [A-Z]{2}-[0-9a-f]+\]', out['NOK']), out
    assert re.fullmatch(r'\[Redacted [A-Za-z ]+: [A-Z]{2}-[0-9a-f]+\]', out['PHONE_MOBILE']), out


@pytest.mark.parametrize('text,hidden', [
    ('|2024|Referring Medical Officer Dr Q. Zarkovsky|', True),
    ('|Seen 2024\nReferring Medical Officer Dr Q. Zarkovsky|', True),
    ('Room 12 Referring Officer Dr Q. Zarkovsky', True),
    ('Seen 2024\nReferring Officer Dr Zarkovsky', True),
    ('Unit 3 Smith Mr Zarkovsky', True),
    # Still a street: a number, a name and Dr with no initial, on one line.
    ('14 Example Dr Springfield', False),
])
def test_071_titled_name_address_guard(redactor, text, hidden):
    out = redactor.record({'t': text}, 1001, redactor.vault('071'))[0]['t']
    if hidden:
        assert 'Zarkovsky' not in out, out
    else:
        assert 'Springfield' in out, out


@pytest.mark.parametrize('text', [
    # Patient 1001 lives in Mudgeeraba 4213; a rural road named after the locality.
    "Address:\n42 Mudgeeraba/O'Neill Creek Rd, .\t4213\n",
    'Home: 42 Mudgeeraba Valley Road Mudgeeraba 4213\n',
])
def test_072_house_number_before_the_suburb_pair(redactor, text):
    out = redactor.record({'t': text}, 1001, redactor.vault('072'))[0]['t']
    assert '42' not in out and 'Mudgeeraba' not in out and '4213' not in out, out


def test_074_end_of_a_multi_word_suburb_with_the_postcode():
    # A suburb of several words: the end of it with the postcode is the patient's.
    import copy
    import json
    data = json.loads(FIXTURE.read_text())
    demo = copy.deepcopy(data['patients']['1001']['demographics'])
    demo.update(CITY_PART='Upper Mudgeeraba', ADDRESS='42 Wombat Hollow Road, Upper Mudgeeraba QLD 4213')
    data['patients']['1001']['demographics'] = demo
    r = Redactor(FixtureSource(data))
    for text in ('Lives near Mudgeeraba 4213.', 'Mudgeeraba, QLD 4213'):
        out = r.record({'t': text}, 1001, r.vault('074a'))[0]['t']
        assert 'Mudgeeraba' not in out and '4213' not in out, out
    # The suburb word alone stays (shared by thousands), as for a one-word suburb.
    assert 'Mudgeeraba' in r.record({'t': 'Mudgeeraba clinic'}, 1001, r.vault('074b'))[0]['t']


def test_074_suburb_written_before_the_house_number(redactor):
    # Patient 1001: Wombat Hollow Road, Mudgeeraba. Another number on that road,
    # the suburb first.
    out = redactor.record({'t': 'Old address: Mudgeeraba 1180 Wombat Hollow Road\n'},
                          1001, redactor.vault('074c'))[0]['t']
    assert 'Mudgeeraba' not in out and '1180' not in out and 'Wombat' not in out, out


def _rural_redactor():
    # Patient 1001 with a rural address: the locality on line 2, the postal town in CITY.
    import copy
    import json
    data = json.loads(FIXTURE.read_text())
    demo = copy.deepcopy(data['patients']['1001']['demographics'])
    demo.update(ADDRESS2_PART='Bunyip Flat')
    data['patients']['1001']['demographics'] = demo
    return Redactor(FixtureSource(data))


@pytest.mark.parametrize('text, gone, kept', [
    # A scanned letter: the street garbled into a lower-case word, then the locality.
    ('DOB: 14/03/1957\n77 Old lvlill Road BUNYIP FLAT 4213\nULTRASOUND',
     ['77', 'lvlill', 'BUNYIP', '4213'], ['ULTRASOUND']),
    # The same, as the prompt pass reads it: a data block written out as Python text.
    ('DOB: 14/03/1957\\n77 Old lvlill Road BUNYIP FLAT 4213\\nULTRASOUND',
     ['77', 'lvlill', 'BUNYIP', '4213'], ['ULTRASOUND']),
    ('Location: Bunyip Flat, QLD 4213.', ['Bunyip', '4213'], ['Location']),
    ('Take 2 tablets daily\nBunyip Flat 4213', ['Bunyip', '4213'], ['Take 2 tablets daily']),
    # The locality alone, and someone else's postcode, stay.
    ('Bunyip Flat show day', [], ['Bunyip Flat']),
    ('12 Smith St Bunyip Flat 4000', [], ['Bunyip Flat 4000']),
])
def test_075_rural_locality_with_the_postcode(text, gone, kept):
    r = _rural_redactor()
    out = r.record({'t': text}, 1001, r.vault('075'))[0]['t']
    assert not [g for g in gone if g in out] and all(k in out for k in kept), out


def test_075_any_case_street_before_the_suburb_pair(redactor):
    # The CITY suburb too: a garbled street between the number and the pair.
    out = redactor.record({'t': 'Home:\n9 Old lvlill Road Mudgeeraba QLD 4213\n'},
                          1001, redactor.vault('075b'))[0]['t']
    assert '9 Old' not in out and 'lvlill' not in out and '4213' not in out, out


def _id_label_redactor():
    import csv
    rows = list(csv.DictReader((Path(__file__).parent.parent / 'docs' / 'identifier_labels_seed.csv').open()))
    return Redactor(FixtureSource(FIXTURE), lists=StaticLists({'identifier_labels': rows}))


@pytest.mark.parametrize('text, want', [
    ('- Hospital UR Number: 123456\n', '- Hospital UR Number: [ID Removed]\n'),
    ('URN 123456; UR No. 4321', 'URN [ID Removed]; UR No. [ID Removed]'),
    ('Accession Number: 22334455 (I-MED)', 'Accession Number: [ID Removed] (I-MED)'),
    ('Patient ID: ss-1234', 'Patient ID: [ID Removed]'),
    ('Specimen No: 123456-78XY', 'Specimen No: [ID Removed]'),
    # Left alone: no value, too short, a date, a bare year, no label.
    ('UR: pending', 'UR: pending'),
    ('URN 12', 'URN 12'),
    ('Episode: 12-03-2024', 'Episode: 12-03-2024'),
    ('Previous episode 2019.', 'Previous episode 2019.'),
    ('Your 12345 steps', 'Your 12345 steps'),
])
def test_076_labelled_identifiers(text, want):
    r = _id_label_redactor()
    assert r.record({'t': text}, 1001, r.vault('076'))[0]['t'] == want


def test_076_built_in_labels_apply_without_a_list(redactor):
    # 0.2.0: the built-in labels ship with the package; the practice's list adds to them.
    assert redactor.record({'t': 'UR: 123456'}, 1001, redactor.vault('076b'))[0]['t'] == 'UR: [ID Removed]'


def test_076_every_default_switched_off_nothing_changes():
    from bps_redaction import StaticLists
    from bps_redaction.redaction import default_identifier_labels
    off = [dict(r, active='false') for r in default_identifier_labels()]
    r = Redactor(FixtureSource(FIXTURE), lists=StaticLists({'identifier_labels': off}))
    assert r.record({'t': 'UR: 123456'}, 1001, r.vault('076c'))[0]['t'] == 'UR: 123456'


def _redactor_with_street(street):
    import copy
    import json
    data = json.loads(FIXTURE.read_text())
    demo = copy.deepcopy(data['patients']['1001']['demographics'])
    demo.update(ADDRESS1_PART=street, ADDRESS=f'{street}, Mudgeeraba QLD 4213')
    data['patients']['1001']['demographics'] = demo
    return Redactor(FixtureSource(data))


def test_077_less_common_street_type_abbreviation():
    # BP holds the type spelled out; the letter writes a rarer abbreviation.
    r = _redactor_with_street('12 Wattle Avenue')
    out = r.record({'t': 'Address:\n12 Wattle Av\nMUDGEERABA QLD\n'}, 1001, r.vault('077a'))[0]['t']
    assert '12' not in out and 'Wattle' not in out, out


def test_077_hyphen_written_between_two_street_words():
    # BP holds two street words apart; the letter joins them with a hyphen and
    # writes the type in full. The line is the patient's own address, matched
    # as theirs (not only by the shape of the pair after it).
    r = _redactor_with_street('4321 Kangaroo Gully Rd')
    out = r.record({'t': 'Seen today\n4321 Kangaroo-Gully Road MUDGEERABA 4213\n'}, 1001, r.vault('077b'))[0]['t']
    assert 'Kangaroo' not in out and '[Redacted Patient: PT-' in out, out


def test_078_titled_name_after_a_date(redactor):
    out = redactor.record({'t': 'Seen 12/03/2024  Ann Bee  Dr Xavier Quill Thorne a)'},
                          1001, redactor.vault('078'))[0]['t']
    assert 'Xavier' not in out and 'Thorne' not in out, out


@pytest.mark.parametrize('text', [
    # Text extraction dropped a space: after the house number, before the type.
    'Address: 42Wombat Hollow Road, MUDGEERABA',
    'Seen at 42 Wombat-HollowRoad today',
    'HOME 42WOMBAT HOLLOWRD',
])
def test_079_glued_street(redactor, text):
    out = redactor.record({'t': text}, 1001, redactor.vault('079'))[0]['t']
    assert 'Wombat' not in out.title() and '42' not in out, out


def test_081_street_twice_on_one_line():
    # Two columns: the patient's street, a long gap, the suburb, a long gap, the
    # street again. The second house number is not a postcode.
    import copy
    import json
    data = json.loads(FIXTURE.read_text())
    demo = copy.deepcopy(data['patients']['1001']['demographics'])
    street = "1234 Wattleton O'Way"
    demo.update(ADDRESS1_PART=street, ADDRESS2_PART='', CITY_PART='Farville', POSTCODE_PART='4998',
                ADDRESS=f'{street}, Farville 4998')
    data['patients']['1001']['demographics'] = demo
    r = Redactor(FixtureSource(data))
    out = r.record({'t': f"  {street}{' ' * 30}Farville{' ' * 17}{street}\n"}, 1001, r.vault('081'))[0]['t']
    assert 'Wattleton' not in out and "O'Way" not in out, out
    # A former suburb and postcode right after the street still go.
    out = r.record({'t': f'  {street}, Otherton 4321\n'}, 1001, r.vault('081b'))[0]['t']
    assert 'Otherton' not in out and '4321' not in out, out
