"""De-redaction keeps a JSON reply valid (MedUI ADR-073). Invented names only."""
import json

from bps_redaction.deredact import deredact

TOK = '[Redacted Patient: PT-0a1b2c3d]'
MAP = {TOK: 'Marigold "Goldie" Quillfeather'}


def test_json_envelope_stays_valid_with_a_quoted_name():
    reply = json.dumps({'Title': 'Plan', 'Response': f'Review {TOK} in a week.'})
    out = deredact(reply, MAP).text
    assert json.loads(out)['Response'] == 'Review Marigold "Goldie" Quillfeather in a week.'


def test_json_fence_is_parsed_and_written_back_as_json():
    reply = '```json\n' + json.dumps({'Title': TOK, 'Response': 'ok'}) + '\n```'
    out = deredact(reply, MAP)
    assert json.loads(out.text)['Title'] == 'Marigold "Goldie" Quillfeather'
    assert out.restored == 1


def test_array_and_nested_values():
    reply = json.dumps([{'a': [TOK, 3]}, 'x'])
    assert json.loads(deredact(reply, MAP).text) == [{'a': ['Marigold "Goldie" Quillfeather', 3]}, 'x']


def test_plain_text_unchanged_behaviour():
    out = deredact(f'Dear {TOK},', MAP)
    assert out.text == 'Dear Marigold "Goldie" Quillfeather,'


def test_unknown_token_inside_json_is_swept_and_json_survives():
    reply = json.dumps({'Response': 'See [Redacted HCP: HP-ffffffff].'})
    out = deredact(reply, MAP)
    assert json.loads(out.text)['Response'] == 'See (redacted).'
    assert out.unresolved == 1 and out.swept == 1


def test_not_quite_json_is_restored_as_text():
    out = deredact('{"Title": "x", ' + TOK, MAP)
    assert out.text.endswith('Marigold "Goldie" Quillfeather')
