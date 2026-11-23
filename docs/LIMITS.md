# What this can and can't do

Read this before you send anything to a cloud model on the strength of it.

## Best-effort, by design

Redaction here is rule-based: it removes what it can recognise, either because
the patient's or the practice's own record says what to look for, or because
the text has a recognisable shape (a phone number, a titled name, an address,
a labelled date of birth). It is not a guarantee that a text is free of
identifying information, and nothing in this repository should be read as one.

That is why every `/v1/redact` response carries the checker's flag report, and
why `/v1/verify` exists. **Treat the checker as part of the workflow**, not an
optional extra: look at flags, review them on the `/admin` page, and turn what
you learn into list entries or new rules.

## Measured on one practice

The rules were developed against one Australian general practice's real
prompts, letters, results and consultation transcripts, and each was measured
there before and after it changed (see [RULES.md](RULES.md)). Other practices
write differently: other letter templates, other ways staff record emails and
phone calls, other transcription tools, other local organisations. Expect the
checker to find things here that it didn't find there, and measure your own
data before relying on the rules.

## Australian, and Best Practice

- Formats are Australian: phone numbers and area codes, Medicare numbers and
  their check digit, IHI, DVA and pension numbers, street types and unit forms,
  state abbreviations and four-digit postcodes. Some organisation words that
  stop a letter recipient being taken for a person are Queensland-flavoured.
- The identity source is Best Practice Premier, read through the SQL in
  `src/bps_redaction/adapters/sql/`. Those queries were written against one BP
  installation; check them against yours. The JSON fixture source is for trying
  the library and for tests, with invented people only.

## What it misses

- **People nobody has described.** A name with no title, no record behind it
  and no tell-tale position — "her friend Margaret came too" — is not redacted.
  Titles, letter lines, email lines, greetings, Author fields and transcript
  vocatives catch many such names; plain mentions in free text are not caught.
- **Names written differently from the record**, beyond the spelling variants,
  multi-word given names and shortened names the rules generate. A
  misspelling, a transcription error or an unusual nickname gets through.
  Transcripts are the hardest case: speech-to-text renders names freely.
- **Identifying combinations.** A rare condition, an occupation, a small town
  and an age can identify someone together; nothing here looks at that.
- **Other dates and numbers.** Only dates of birth are treated as identifiers;
  appointment dates, ages and reference numbers other than those listed pass
  through.
- **Anything that isn't text**: images, scanned pages, attachments.
- **Languages other than English.**

## What it may over-redact

- Ordinary words that are also someone's name are matched only when
  capitalised, so a sentence that starts with one ("Rose to 140 overnight")
  can be redacted.
- Clinical eponyms that are also a practitioner's surname are protected only
  where the clinical word follows (`White Cell Count`); others may be taken
  for the practitioner.
- A street name, once known to be the patient's, is removed wherever it
  appears, including a clinic on the same street.

## The token map and de-redaction

- De-redaction needs the session's token map, which the service keeps for 24
  hours after the last `/v1/redact` (7 days at most). A client that tries later
  is refused: store the restored text, never the tokens. See
  [API.md](API.md).
- A patient identifier comes back in the record's spelling, not the text's:
  "MARIGOLD" written in capitals returns as "Marigold", and an abbreviated
  street type may return in lower case ("Rd" as "rd").
- Models sometimes alter a token. De-redaction sweeps mangled and unknown
  tokens rather than handing them back, but a token the model rewrote beyond
  recognition is removed, not restored.

## Inherent to a redaction service

- **It reveals what it knows.** Anyone with a `redact` key can test whether a
  string is a patient's identifier by watching whether it becomes a token. Give
  keys only to programs that need them, one each, and revoke them when they
  are retired. Rate limits and the audit table are the other controls.
- **It holds what it removes**, for as long as a session lives: the originals
  behind each token, encrypted at rest, and flagged content. See
  [OPERATING.md](OPERATING.md) for what is stored and for how long.
- **It is only one part of compliance.** Using it doesn't make sending data to
  any cloud model appropriate by itself. A practice's privacy obligations, its
  agreements with model providers and its own policies still apply. This
  software is not a medical device and is not certified for any purpose.
