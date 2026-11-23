# BPS Redaction Service

Rule-based redaction and de-redaction of patient text, for practices and
developers working with Best Practice Premier data. **Pre-release.**

Send it a prompt, a typed question or a patient's record before it goes to a
cloud language model; it sends back the same content with identifiers replaced
by tokens such as `[Redacted Patient: PT-43391ac7ced4]`. Send the model's reply
back and the tokens are restored. Every response also says what a second,
independent check still found in the redacted text.

The rules come from an Australian general practice, where they were developed
and measured on real prompts, letters and transcripts (decision records
ADR-018 to ADR-067). **Read [docs/LIMITS.md](docs/LIMITS.md) first:** redaction
is best-effort, the formats are Australian, and the identity source is BP.

## What's here

- **`bps_redaction`** — the library: three profiles (`record` for structured
  EHR data, `input` for typed or dictated text, `prompt` for an assembled
  prompt; no patient = the general passes), a token vault, de-redaction, and
  the checker. Identity sources: Best Practice (`adapters/bp.py`, read-only
  ODBC) and a JSON fixture (`adapters/fixture.py`) for trying it out.
- **`bps_redaction_service`** — an HTTP service over the library: sessions
  that hold the token map for 24 hours, `/v1/redact`, `/v1/deredact`,
  `/v1/verify`, a flag store and API, and an `/admin` page (BP practice
  administrators or a local admin) to review flags and manage API keys.

## Documents

- [docs/LIMITS.md](docs/LIMITS.md) — what it can and can't do
- [docs/RULES.md](docs/RULES.md) — every rule, and why
- [docs/API.md](docs/API.md) — the HTTP API, for client developers
- [docs/OPERATING.md](docs/OPERATING.md) — running the service, storage, security, admin
- [SECURITY.md](SECURITY.md) — reporting a security problem privately

## Using the library

```python
from bps_redaction import Redactor
from bps_redaction.adapters.bp import BPSource

redactor = Redactor(BPSource(odbc_connection_string))
# or Redactor(BPSource(...), practice_first_names=True): also sweep the practice's own
# users' first names (0.2.9; 4-letter ones in lower case too, 0.2.10)
vault = redactor.vault(conversation_key)          # stable tokens per key
question = redactor.input({'question': typed}, patient_id, vault)['question']
# A consultation transcript: also the recording clinician's own first names, any case (0.2.8)
# text = redactor.input({'raw_transcript': t}, patient_id, vault,
#                       clinician_first_names=['Sam'])['raw_transcript']
prompt = redactor.prompt(assembled_prompt, patient_id, vault)
findings = redactor.check(prompt, patient_id)     # what got through
verdict = redactor.verdict(prompt, patient_id)    # the same, as a stored verdict (0.2.0)
answer = redactor.deredact(model_reply, vault.get_reverse_map()).text
```

`checker.scan.is_blocking(verdict)` says whether to stop the request for a person to review (a
tier 0 or 1 hit), and every verdict records the detector version and a fingerprint of the
practice's lists. See [docs/RULES.md](docs/RULES.md#the-checker).

A host that keeps its own order of passes (MedUI does, for a patient-only policy) calls the rule
classes inside `with redactor.context():`. After changing an input the caches hold (a list entry,
a practitioner's name), `redactor.forget()` applies it to the next call (0.2.1).

Never log text or tokens.

## Running the service

```
pip install 'bps-redaction[service,bp]'
bps-redact keygen          # encryption key → BPS_REDACT_KEYS
bps-redact keygen audit    # patient-hash secret → BPS_REDACT_AUDIT_SECRET
bps-redact serve           # 127.0.0.1:8470; /admin to log in
```

The BP source needs Microsoft's ODBC Driver for SQL Server, installed
system-wide. Details in [docs/OPERATING.md](docs/OPERATING.md).

## Tests

```
pip install -e '.[service,test]'
pytest
```

The tests use invented people only (`tests/fixtures/`); phone numbers are
from the ACMA's fictitious ranges.
