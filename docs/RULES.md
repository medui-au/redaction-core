# The rules

Each rule is a decision record (ADR) with a number. Comments in the code cite
the number where a rule is applied, and the `ruleset` a response reports
(`RULESET` in `bps_redaction/__init__.py`) names the last record included. The
rules were developed and measured on one Australian general practice's real
prompts, letters and transcripts before being published here; see
[LIMITS.md](LIMITS.md) for what that does and doesn't mean.

Tokens are reversible: `[Redacted Patient: PT-…]`, `[Redacted HCP: HP-…]`,
`[Redacted Practice: PR-…]`, `[Redacted Name: NM-…]`, `[Redacted Email: EM-…]`,
`[Redacted Phone: PH-…]`, `[Redacted Provider: PV-…]`, `[Redacted Number: NB-…]`, `[Redacted Medicare: MC-…]`. Fixed text is not:
`[Redacted Address]`, `[Redacted DOB]`, `[Link removed]` — used where nothing
downstream needs the original back.

## The patient's own identifiers

Built from the patient's BP record (`redaction.identifier_entries`) and matched
in every profile.

| ADR | Rule |
|---|---|
| 018 | Identifiers match on word boundaries, and are built from the record's parts (first name, surname, DOB, phones…), never from display strings that text never contains. |
| 020 | An address is found by its street line and extended over a trailing suburb or postcode, so the whole address becomes one token. |
| 021 | The street line is matched at its core (number, name, type), with abbreviation (`Street`/`St`) and unit (`2/14`) forms; BP often stores a locality after the street type. |
| 022 | Phone numbers in national form, the area code supplied when BP stores 8 digits, in every common spacing (`0491 570 156`, `+61 491…`, `(07) 5550 1234`, half-spaced, dotted). |
| 062 | A phone field counts only if it holds a digit: BP returns an empty one as its bare label (`(W) `), which is not an identifier. |
| 068 | The patient's and next of kin's phone numbers match in any spacing: up to three spaces, tabs, dots or hyphens between any two digits, never a line break, with or without the area code, `+61` or brackets (`5550  1234`). They run after the literal identifiers, so BP's composite strings (the next-of-kin line, `(M) 04…`) still go whole. |
| 033 | The street *name* without its house number (`Mt Example Road`) becomes `[Redacted Address]`. |
| 063 | Every street rendering also matches with its apostrophes dropped or spaced (`OReilly`, `O Reilly`) and its hyphens with any spacing (`Smith-Jones`, `Smith - Jones`, `Smith Jones`). |
| 064 | A house, unit, lot or rural number in front of the street name goes with it, and so does a locality after the street that is not the patient's own (words and a state, words and a postcode, or words in capitals; never a field label). |
| 069 | The number in front of the street name may be followed by any space but a line break, including the no-break space that RTF and HTML conversions write. |
| 072 | The patient's suburb-with-postcode pair takes a house number directly in front of it, for a road named after the locality (`42 <Suburb>/<Name> Creek Rd, 4xxx`). |
| 074 | The end of a multi-word suburb with the patient's postcode (`Mudgeeraba 4213` for Upper Mudgeeraba) is matched, and the patient's suburb written before their house number and street goes with them. |
| 075 | A rural locality on the patient's address line 2 (letters only) counts as their suburb: `Bunyip Flat 4213` is matched where CITY holds the postal town. In front of either pair, a house number and up to four words ending in a street type go too, in any case (`77 Old lvlill Road`, a garbled scan). |
| 076 | A value after a label on the `identifier_labels` list (`UR: 123456`, `Accession Number: …`) becomes `[ID Removed]`; the label stays. Labels, not regular expressions; optional `shape` mask (`9` digit, `A` letter, `X` either). Without a shape: at least four digits, never a date or a bare year straight after the label. **Since 0.2.0 the starting labels ship with the package** (`data/identifier_labels.csv`, also `docs/identifier_labels_seed.csv`); the practice's `identifier_labels` list adds to them, and a practice row with the same label replaces the built-in one (`active` false switches it off). |
| 077 | Less common street-type abbreviations (`Av`, `Pd`, `Cr`, `Ter`, `Crt`…) are matched both ways; two street words stored apart may be written with a hyphen between them (one pair at a time, never into the type). |
| 078 | The titled-name guard's house number is not part of a date, time or decimal: `<date> Name Name Dr Name` is a titled name, not a Drive. |
| 079 | The patient's street with a space dropped by text extraction: after the house number (`42Wombat Hollow Road`) or before the street type (`42 Wombat-HollowRoad`), or both. Exact renderings of their own street only. |
| 081 | After the patient's own street, the locality tail takes four digits as a postcode only if they're the patient's postcode, or within three spaces of the locality words: a two-column layout that prints the street twice on one line no longer has its second house number read as a postcode. |
| 060 | The patient's suburb followed by their postcode on the same line (`Location: Suburb, 4xxx`), and their postcode after a label or a state (`Postcode 4xxx`, `QLD 4xxx`), become `[Redacted Address]`. The suburb alone stays. |
| 034 | The patient's Medicare card with any issue number, with or without the IRN. |
| 042 | Spelling variants of each name (hyphen and apostrophe forms, closed-up spaces), and each word of a multi-word given name on its own (BP often keeps first and middle names together). |
| 044 | An underscore separates words (a file name like `SURNAME_FIRST_<DOB>.pdf`), and 8-digit dates of birth (`YYYYMMDD`, `DDMMYYYY`). |
| 045 | A name that is also an ordinary word matches in capitals as well as Title case (`RE: MARY ROSE`). |
| 048 | The patient's likely shortened names, learned from the practice's own BP preferred names (a pairing counts when 3 or more patients share it). Title case only; an `NM` token, since a relative may share it. |
| 049 | Reviewed name variants (the `name_variants` list) are added to those forms, without the 3-patient minimum. |
| 056 | A shortened-name form that is not an ordinary word (`Tilly`) matches in any case; one that is (`Bill`, `Rod`) in Title case only. The shortened-name pass runs after the titled-name and Author passes, so `Dr Tilly Smith` goes whole. |
| 051 | A Medicare card's expiry goes with the card; the patient's own name fields in a demographics row are tokenised whole, whatever their capitals. |
| 052 | When a string is both the patient's and someone else's (a patient who shares a name with an address-book doctor), the patient's label wins. |
| 055 | A name that is also an ordinary word and has capitals inside it (McDonald, VanDyke) matches as stored as well. |

## Words that look like names but aren't

| ADR | Rule |
|---|---|
| 024 | A surname followed by a clinical word is a clinical term, not a person: `White Cell Count`, `white matter`. Analyte, unit and code fields are never redacted. |
| 025 | A one-word identifier that is also an ordinary English word (Small, Rose, Day) matches only when capitalised, so "small bowel" survives. The word list ships with the package. |

## Practitioners and the practice

| ADR | Rule |
|---|---|
| 019 | Text a person typed or dictated is redacted for practitioners (the HCP pass) before it is put into a prompt. |
| 023 | The practice's own addresses, phones, fax and email are redacted under their own `PR` token. |
| 121 | The practice's own name (BP's `PRACTICE.PRACTICENAME` and `PracticeLegalName`; a site's `LOCATIONNAME` only when it ends in words like Medical Centre) is a `[Redacted UsualPractice: UP-…]` token, one per site: the joined name with "and", "&" or "+" in any order, each site with the shared ending, Centre or Centres (Center), any capitals, a line break or a literal `\n` between words, digits run onto the end (left for the phone pass), a last word cut off by a scan if two whole words come before it and at least four letters are left. A town alone, and the town with any other organisation word (Hospital, Pharmacy), are kept. Runs before the patient and practitioner passes, over the whole prompt. An always-redact row labelled `practice name` gets the same token. |
| 066 | Each practice site's suburb with its postcode, a state allowed between and any spacing on one line (`SAMPLETON  QLD  4999`), is a `PR` token too. The suburb alone and a bare postcode stay. |
| 067 | The practice's suburb with its postcode is matched after the patient's own suburb pair (a patient in a neighbouring town shares the postcode) and before a labelled postcode; the patient's suburb pair allows a neighbouring town between suburb and postcode. |
| 026 | Anyone with a title (Dr, Prof, Mr, Mrs, Ms, Sister…) is redacted even when nobody can identify them — an `NM` token. |
| 036 | A titled name takes a third name word when it isn't an ordinary word, and a bracketed preferred name (`Dr Katherine (Kate) Smith`). |
| 037 | The practice's own doctors (from BP's USERS) are known to the HCP pass; titled names accept initials (`Dr J. A. Smith`). |
| 065 | Middle initials between first name and surname (`Dr Mary J. Smith`); the word after a middle initial is the surname even if it is an ordinary word. A bare `I` is not an initial. |
| 070 | The titled-name pass's street-address guard (`14 Example Dr Springfield`) reads the text just before the match itself, so a token earlier in the text no longer turns a name away. |
| 071 | That guard reads one line only and applies only to a `Dr` that can be Drive (no initial after it); other titles are never checked. Anyone else's street keeps its number, name and type on one line, so `Seen 2024⏎Referring Officer Dr Name` is a name, not a street. |
| 039 | An address-book surname that is a common first name is not matched when another name follows it (`Dr David Smith` is Dr Smith). |
| 122 | A practitioner surname followed on the same line by a lone initial and a capitalised word is one person: the token takes all of it (`Dr Rose J Quaverley`), title outside, and restores all of it. Not when that word is a lower-case ordinary word that is not an address-book surname (`Dr Rose A Little`), nor when it is in capitals and not an address-book surname (`A NOCTE` in a dose). |
| 123 | An initial and an address-book surname that is an ordinary word are taken without a title when a post-nominal follows (`J Small FRANZCR`) or the two are a line of their own. |
| 040 | The practice's other staff (nurses, reception, allied health) are redacted by full name. |
| 054 | A practitioner's title stays outside the token, so it survives de-redaction unchanged. |

## People and details the record doesn't know

| ADR | Rule |
|---|---|
| 028 | Email addresses and phone numbers that belong to nobody on file (relatives, carers, other rooms) are redacted reversibly, one token per value; a bare 8-digit number with no phone label becomes an `NB` token. |
| 061 | Medicare provider numbers (a six-digit stem, often written without its leading zero, a location character and a check letter) whose check letter is right become reversible `PV` tokens. |
| 030 | An administrator's always-redact list, applied after the record-based passes (`always_redact`). |
| 031 | The patient's BP record number (INTERNALID) never reaches the model. |
| 032 | The recipient in BP's letter lines (`Letter written to <recipient> re. …`) is redacted when it is a person. |
| 035 | Anyone else's street address, PO Box or Locked Bag becomes `[Redacted Address]`. |
| 068 | Anyone's area-coded or mobile number allows up to three spaces or tabs between its digit groups (`07  5550  4321`). A bare 8-digit number still allows one, so two 4-digit values in a table are not joined. |
| 038 | A person named in a document's Author field is redacted. |
| 041 | The person named in an email line (`email to <name> re …`) or a greeting (`Hi <name>,`). |
| 043 | Share links (whose query is the key) and links into a private network become `[Link removed]`, before anything inside them is tokenised. |
| 046 | The person addressed by name in a transcript (`How are you going, Jo?`). |
| 047 | A labelled date of birth that isn't the patient's becomes `[Redacted DOB]`. |
| 050 | Email addresses without their ending (`ruby9@gmr`) and spoken ones (`ruby nine at gmail dot com`). |

## The last sweep

| ADR | Rule |
|---|---|
| 059 | The sweep also takes a surname written before the name and a comma (`PUDDLEBY, LINDA`), unless it is a greeting, a relationship or a note word (`Thanks, Linda`, `Daughter, Linda`); names of 5 or more letters in lower case too; never `AVA` in capitals (aortic valve area). |
| 058 | The first names most common among the practice's own patients (held by 10 or more records, not ordinary words or months), in Title case or capitals, with the capitalised word that follows on the same line unless it is plainly not a surname — carers and relatives no record names. The list is rebuilt weekly from BP; the `first_names` list adds (`include` true) or removes (`include` false) names. |

## No patient

| ADR | Rule |
|---|---|
| 053 | With no patient (`patient_id` null), the `input` and `prompt` profiles run every pass that needs no patient record, with practitioners from the whole address book and organisations matched only as written. |

## Changes in 0.2.0 (MedUI ADR-073, ADR-083 to ADR-112)

| ADR | Rule |
|---|---|
| 083 | The clinical-phrase guard ("Count", "Blood"… after a surname keep it) looks at the next word on the same line only: with any whitespace it looked past blank lines, so a surname ending one line was kept whenever the next began with such a word. |
| 073 | De-redaction of a reply that is a JSON object or array (a ```json fence allowed) restores the tokens inside its parsed strings and writes it back as JSON, so a restored name with a quote or backslash (`First "Preferred" Last`) can't break it. Any other reply is restored as text, as before. |
| 112 | The practitioner and address-book queries keep inactive and deleted entries on purpose (a test guards it), so a "shadow" user entered under a preferred name is redacted and checked. |

## Changes in 0.2.2 (MedUI ADR-116)

| ADR | Rule |
|---|---|
| 116 | A literal `\n`, `\t` or `\r` (the two characters, as data blocks written out as Python or JSON text carry them) is a word boundary for every rule and for the checker: "\nGoldie" is the word "Goldie" after a line break, not "nGoldie". The escape is kept; the vault never stores the placeholder used while the rules run. Checker pattern-v23. |

## Changes in 0.2.3 (MedUI ADR-117)

| ADR | Rule |
|---|---|
| 117 | A patient's shortened name (practice table or `name_variants`) is redacted in single quotes and with a possessive: 'Goldie', Goldie's, Goldie’s. An apostrophe joining letters still blocks it (O'Goldie, "Don't"). Double quotes already worked. Checker unchanged (pattern-v23). |

## Changes in 0.2.4 (MedUI ADR-118)

| ADR | Rule |
|---|---|
| 118 | A name that is also a month (a patient called May or June, a shortened name Jan) is kept when it is written as part of a date: a day before it ("12 May", "12th of May"), a year after it ("May 2026", "May 12, 2026", "May '26"), or a word that leads a month ("in", "since", "until", "during", "early", "last" …). Every pass applies it; the checker's patient tier does not report such a month (pattern-v24). "to June", "by May" and possessives stay names. |

## Changes in 0.2.5 (MedUI ADR-121 to ADR-124)

| ADR | Rule |
|---|---|
| 121 | The practice's own name becomes `[Redacted UsualPractice: UP-…]` (see above). New query `practice_name`. Checker pattern-v25: a form left in the text is a tier-1 `practice_contact_detail` hit of kind `site_name`. |
| 122 | A practitioner token takes a following initial and name on the same line. |
| 123 | An initial and an ordinary-word address-book surname, with a post-nominal or on a line of their own. |
| 124 | Fixes ADR-118: a number and a full stop before a month name are not a day (`12/03/21. June reports …`, `BP 120/80. June …`), and neither is a number that ends a slash date or a ratio. The month name is a name there, as before 0.2.4. `12 June`, `12th of June`, `3/Jun`, `3-Jun` and `12. June 2026` stay dates. Found by the summariser's 0.2.4 check (5 documents). |

## Changes in 0.2.6 (MedUI ADR-126, ADR-127)

| ADR | Rule |
|---|---|
| 126 | The practice's own name keeps its `UsualPractice` label in a vault shared across documents. The HCP pass pre-mints every address-book name, so an address-book entry under a site's own name ("<Town> Medical Centre", category Medical practice) took the HCP label once any earlier document had run. Only the patient's label still wins over it. Labels only: the text was already redacted. |
| 127 | More of the forms scans make of the practice's name. A superscript digit ends the name (`Centre²`). A capital may follow the whole ending straight on (`CentreAppointments`). "Centra", "Centro" and "Centry" are taken after the whole site name and "Medical", but "Central" is not. One stray letter is allowed before a capitalised town (`x<Town> Medical Centre`), and stays outside the token. Checker pattern-v26 (the same forms). |

## Changes in 0.2.7 (MedUI ADR-131)

| ADR | Rule |
|---|---|
| 131 | A phone label typed straight onto the number, with no space or colon: `ph0412 345 678`, `mob0412345678`, `ph5550 1234`. The patient's and next of kin's own numbers are taken after any letters (`call0412…`), since the digits are theirs; anyone else's mobile, landline or bare 8-digit number only after a phone label (ph, phone, tel, telephone, mob, mobile, cell, home, work, fax, h, w, m, p, f, t), so `AB0412345678` or `REF0412345678` stays. The label stays outside the token. Checker pattern-v27 (the same labels before a mobile or landline). |

## Changes in 0.2.8 (MedUI ADR-133)

| ADR | Rule |
|---|---|
| 133 | **The recording clinician's own first name, in a consultation transcript, in any case.** A host passes the names (`Redactor.input(values, patient_id, vault, clinician_first_names=[...])`, or `RedactionService.redact_clinician_first_names`); without them nothing changes. They are matched in Title case, capitals and lower case ("oh hi sam"). Lower case is never used for a name of under three letters, or for one that is an everyday word (`EVERYDAY_WORD_NAMES`: Will, May, Mark, Pat, Don, Max, Drew…), which match in Title case and capitals only. Whole words; a possessive 's stays outside. A surname-like word beside the name goes with it, as in the first-name sweep (a mis-heard surname, "Sam Pellowe here"). One `NM` token per person, whatever the case. Runs last in the input profile, after the patient and practitioner passes: a patient's own name keeps its label and a full practitioner name stays one HCP token. Also for every caller: spoken fillers and a few short words before a comma or after the name are not taken as a surname by the sweep (yeah, yep, okay, oh, ah, um, uh, hey, right, alright, g'day, good, exactly, I'm, I've, I'll, I'd). Checker unchanged (pattern-v27). |

## Changes in 0.2.10 (MedUI ADR-135)

| ADR | Rule |
|---|---|
| 135 | **A practice user's 4-letter first name in lower case.** With `practice_first_names` on, the practice's own users' first names of 4 letters are also matched in lower case ("how is quen going"), as names of 5+ letters already were. These names are never ordinary words or everyday-word names (0.2.9 leaves those out), so the ADR-059 reason for the 5-letter floor does not apply to them. Names of 3 letters, and patients' first names of 4 letters, stay Title case and capitals only. With the option off nothing changes. Checker unchanged (pattern-v27). |
| 141 | **A patient's name with an s straight after it.** A possessive written s' ("Marigolds' daughter", curly ’ too, any case the name already matches in) and a bare s ("Marigolds daughter", "the Quillfeathers") are taken for the patient's own names (first, middle, surname, preferred, next of kin) and their nicknames. Before, the name guard wanted no letter after the name, so these went through whole. The s and the apostrophe stay outside the token, as a possessive 's does, and share the name's token. A bare s is not taken when the name was matched in any case and the name with the s is an ordinary word ("maris"); a name that is an ordinary word itself matches with a capital only, as before, so "Jeans" is taken for a patient named Jean and "jeans" is not. A longer word ("Marigoldsmith") is not the name. Clinicians' names and the first-name sweep are unchanged. Checker pattern-v29: the same forms (tier 0), and the patient's first name, surname or preferred name glued to a letter or digit ("xQuillfeather", "Quillfeather2", "MARIGOLDQUILLFEATHER"), which redaction does not take: tier 0, `outside_rules`, variant form `<form>_glued`, so the request pauses for the user to remove or allow it. Only names of 5+ letters that are not ordinary words, and never when the word the name sits in is an ordinary word (or one with an s). |
| 142 | **A street named for the patient's surname + s stays whole.** The ADR-141 match (a name with an s straight after it) stands down when a street type follows on the same line ("Quillfeathers Road", "Quillfeathers St"), so the street-name and address passes take the whole street, as before 0.2.11. In 0.2.11 the surname + s became a patient token first and "12 Quillfeathers Road" came out as a token with the house number and street type left beside it. Checker unchanged (pattern-v29). |
| 143 | **A road named for the surname + s, with words before the type, stays whole.** The ADR-141 match also stands down straight after a house number ("123 Quillfeathers Gully Creek Rd", whatever follows), and when "Rd" or "Road" comes one or two words later ("Quillfeathers Gully Creek Rd"). Only Rd/Road for the look-ahead: "St" can be Saint. In 0.2.12 the guard looked at the next word only, and such a road came out as a patient token with the number, the road words and the type left beside it. A name + s left whole is still reported by the checker (pattern-v29, unchanged). |

## Changes in 0.2.9 (MedUI ADR-134)

| ADR | Rule |
|---|---|
| 134 | **The practice's own users' first names in the first-name sweep, opt-in.** `Redactor(..., practice_first_names=True)` adds the first names of every doctor and staff member (`practice_doctors`, `practice_staff`: current and former) to the sweep's list, so "Dr SAM [Redacted HCP: …]" or "Thanks, Sam" loses the first name the HCP pass leaves. A first name held in the surname field ("Dr. Sam Pellow", first name empty) is read from there; initials and names of one or two letters are not names. Left out, as for the weekly list: ordinary words and months, and everyday-word names (`EVERYDAY_WORD_NAMES`); an administrator adds any of these by hand in the `first_names` list, which can also take a name out (`include` false). Also left out: accounts named for a service rather than a person ("Telehealth Bookings", "Fluvax Northvale"), told by a first name no patient record has and a surname that is an ordinary word or a town in `practice_locations`. The usual sweep rules apply: Title case and capitals, lower case for names of 5+ letters. Refreshed hourly. The weekly cache now also keeps every first name held by a patient record (`held`); a cache from before 0.2.9 is rebuilt once. With the option off (the default) nothing changes, and the lists fingerprint is as before; with it on, the fingerprint records it. Checker unchanged (pattern-v27). |

## Changes in 0.2.21 (no rule changes)

Comments and documents only, before the public release: references to the original
installation's hosts and internal notes removed, MedUI named by its released name, SECURITY.md
added, and the wheel carries the company NOTICE. No redaction output changes, so RULESET is unchanged.

## Changes in 0.2.20 (MedUI ADR-155)

| ADR | Rule |
|---|---|
| 155 | **A hyphenated name written with a space beside the hyphen** (measured on a fixed list of 200 patients). A surname stored `Smith-Jones` was written `Smith- Jones` in letters (6 of the 13 surname occurrences 0.2.19 left, one patient, 3 places), and no form matched it. Every hyphenated name (the patient's names, preferred name and the next of kin's, as for ADR-042's spellings) now also gets `Smith- Jones`, `Smith -Jones` and `Smith - Jones`, and a name stored spaced gets the plain hyphen. Both parts, in order: one part alone is still never taken (ADR-042). Spaces or tabs only, never a line break. Case as for the other spellings. Checker pattern-v33: the same forms, from the builder. Not chased, by decision (the maintainer and clinical lead): misspellings, a surname written as one of its parts, OCR-damaged forms. Also: one Medicare pattern string made raw (a Python escape warning; no change in what it matches). |

## Changes in 0.2.19 (MedUI ADR-154)

| ADR | Rule |
|---|---|
| 154 | **Two more forms of the patient's own identifiers** (measured on a fixed list of 200 patients). **(1) A zero without the area digit** before the patient's or next of kin's own 8 landline digits: `05550 1234` for a home number stored `55501234` (2 patients; 0.2.18 took an area digit in front, and here the zero is there and the area digit is missing). The same guard: a digit or `+` before the zero means a longer number and nothing is taken. Checker pattern-v32: the same. **(2) One distinctive word of the patient's multi-word surname, beside a name.** The whole surname is an identifier already; a document that misspells another word of it (`van den Thistlewood` for `van der Thistlewood`), or gives one half of a hyphenated one, left the distinctive word in view beside the patient's redacted first name. ADR-042 refused to take a surname part anywhere (`Jones` alone from `Smith-Jones` would catch every other Jones), and that stands. The word is taken only when all of these hold: it is a part of the patient's own SURNAME (spaced or hyphenated; never the next of kin's); 5 or more letters; not an ordinary word and not a particle (van, von, de, der, den, del, la, le, di, da, dos, du, st, mac, mc, bin, binti, ap, ter, ten); written with a capital (Title case, capitals, or as stored); and within three words of a patient or name token on the same line. A PT token. It runs inside `redact_practice_nicknames`, after the patient and titled-name passes, so a caller that sequences the passes itself needs no change; the `input` profile has no nickname pass and does not get it (the prompt pass covers typed text). Counts: 9 such words in 7 of 200 patients, 13 occurrences left visible, 12 of them beside a name placeholder, and 6 of the 9 words also in other patients' text, which is why the rule is tied to the patient's own record and a name beside it. MedUI, 10,367 stored items (1,688 patients, 25 with a multi-word surname, 16 distinctive words): 1 item changed, 2 occurrences taken, nothing else; 1 occurrence standing on its own is left, as designed. |

## Changes in 0.2.18 (MedUI ADR-153)

| ADR | Rule |
|---|---|
| 153 | **Three more forms of the patient's own identifiers** (measured on a fixed list of 200 patients on 0.2.17). **(1) The area digit without its zero** before the patient's or next of kin's own 8 landline digits: `75550 1234` for a home number stored `55501234` (2 occurrences, 1 patient). One of the area digits tried (2, 3, 7, 8; or the stored one) may stand straight before the 8 digits, or with the usual gap; a digit or `+` before it still means a longer number and nothing is taken. **(2) The DOB with day and month swapped**, as a month-first form writes it: `07/03/1950` for a DOB of 3 July 1950, in the numeric forms with a four-digit year (`/`, `-`, `.`, with or without leading zeros). Only when the swap is a real date that is not the DOB itself, so never when the day is over 12 or the day equals the month. The practice's measure counted 14 swaps in 7 patients; a swap-only rule touches none of the 326 other dates in those patients' birth years. **(3) The patient's own Medicare number in any grouping, or cut short.** The whole card with at most one space or hyphen between any two digits (`2953 7015 61` was left whole: ADR-034 knew only the 4-5-1 grouping; 1 stored MedUI item), and the first 7, 8 or 9 of the 10 card digits standing alone, no digit or hyphen on either side (2 occurrences, 2 patients). Seven of one person's own digits in order do not turn up by chance. Checker pattern-v31: the same three. |

## Changes in 0.2.17 (MedUI ADR-152)

| ADR | Rule |
|---|---|
| 152 | **The patient's own phone number with its groups in columns.** Column-aligned text (letters, scanned forms) spreads a number's groups across columns: `5550        1234`, `07    5550    1234`, `0491 570        156`. ADR-068's any-spacing match allowed up to three spaces, tabs, dots or hyphens between any two digits, so these went through whole (8 occurrences in 7 documents of 200 patients, every one with a run of 4 to 12 spaces inside the number, in four shapes, home and mobile). Now, between any two of the patient's or next of kin's own digits, and after the area code or `+61`: up to three spaces, tabs, dots or hyphens as before, **or a run of 4 to 12 spaces or tabs**. Never a line break; a wider run is two columns. Only the person's own digits in order match, as before, so a wide gap does not join anyone else's numbers; the generic phone shapes (anyone's number) are unchanged. Checker pattern-v30: the same, and an 8-digit landline is also looked for without its area code, as redaction already took it. |

## Changes in 0.2.16 (MedUI ADR-150)

| ADR | Rule |
|---|---|
| 150 | **The patient's own address parts on one line, in BP's order.** Every street-line rendering before this was the whole stored line (ADDRESS1 + ADDRESS2), or its street name with its type. A document that writes only part of it, as OCR and letters do, matched none of them: "Unit 10,<tab>Word Word.<tab>4999" with the third word and the second line left out was left whole (found with invented stand-ins). Now each part of the street line (the unit or house number, each word, the street type) is optional, in order, followed by the patient's own suburb, a state and their postcode. Between parts: spaces, tabs, no-break spaces, commas, full stops, a slash or a hyphen; **never a line break or an escape**. A word matches in any case, or in Title case or capitals only when it is a dictionary word. In place of a word, between two parts that matched: its initial or a short abbreviation ("A", "NJ"), or a word of about its length that is none of the patient's other address words, nor their suburb or a state (an OCR misspelling). Any street type may follow a word. **What qualifies:** two words (not both dictionary words); a number and a word; a word with a street type, or with the patient's own suburb or postcode after it; or a unit marker and number with the suburb or postcode. A suburb or postcode alone never qualifies, so the practice's address in the same suburb is left to the practice rules. A second line with no number and no street type is a locality ("Bunyip Flat"): its words never count towards a run. Runs **after** the whole-line and street-name forms and the suburb pairs, as a backstop, so a suburb they anchor on is still there for them. Written as `[Redacted Address]`. Measured on 4,034 stored MedUI items: 22 runs newly taken in 11 patients, nothing else changed; two dictionary words followed only by a state ("Gold Coast QLD" for a patient on Gold Coast Highway) were 17 over-takes before the dictionary-word condition and are none after. A run inside a landmark named after the patient's street is taken, as ADR-033 accepts. Left: a postcode glued to letters, and a suburb on its own. |

## Changes in 0.2.15 (query only)

| Change | Effect |
|---|---|
| **A malformed observation no longer stops a patient's identifiers loading.** `patient_get_demographics.sql` also returns the latest height and weight, which no rule reads. They were converted with a plain `CAST` (and height by implicit arithmetic on the text), so one value typed with its unit ("80kg") made SQL Server fail the whole query. The core then had no identifiers for that patient, and every redaction for them failed closed until BP was corrected. Both now use `TRY_CAST`, as MedUI ADR-145 does for MedUI's own query, so a malformed value comes back blank. The error was logged with the first message SQL Server returned (warning 01003, "Null value is eliminated by an aggregate"), not the conversion error behind it. | No redaction output changes, so `RULESET` stays `medui-adr148` and stored rows keep their stamp. |

## Changes in 0.2.14 (MedUI ADR-148)

| ADR | Rule |
|---|---|
| 148 | **A curated list of common name forms.** `data/name_forms.csv` (name, form; 274 pairs for 176 names) adds common Australian short forms and spelling pairs to the forms looked for in a patient's text: Michael → Mick, Elizabeth → Libby, Lachlan → Lachie, Sarah ↔ Sara. Every form of each first- and middle-name word is used, with no cap, alongside the practice's learned table (ADR-048) and its `name_variants` list (ADR-049). Before, a common form went through whenever the practice had fewer than 3 patients with that pairing recorded as a preferred name and nobody had listed it, and the checker reported it as `outside_rules`, tier 0. The usual nickname rules apply: an ordinary-word form ("mick") matches in Title case only, never after a hyphen, an NM token, a quote or possessive is a boundary, and a name the patient already has on record is left to the patient pass. **The list is built from general knowledge, never from a practice's records,** and holds only forms in common use, so a pairing cannot point at one person. No form is an everyday-word name (`EVERYDAY_WORD_NAMES`: Will, May, Bill, Rose, Drew…), a clinical abbreviation a form would match in any case (reg, cath, seb, dom, des, theo: "HR 72 reg"), or under 3 letters; those come only from the practice's own table or list. **Also for every nickname form (table, list or curated): a surname beside it goes with it, as in the first-name sweep** ("Mick Thornbury", "Thornbury, Mick"; not "Mick rang", `NOT_A_SURNAME`). The nickname pass runs before the sweep, so a form that is also a common first name, which the sweep used to take with its surname, would otherwise leave the surname beside the token. **And a dash bullet is a boundary:** a hyphen blocks a form only when a letter or digit is before it ("X-Ray", "2-Mick"); "-Mick" at the start of a line, after a literal `\n` or after a space is taken. Shipped with the package: a host needs no table, setting or code. The checker counts these forms as covered, since it asks the same `forms_for()` (pattern-v29, unchanged). |

## The checker

The checker (`bps_redaction.checker`) searches redacted text for what got
through: tier 0 the patient's own identifiers (every string redaction builds,
plus renderings only the checker looks for), tier 1 the practice's
practitioners and sites, tier 2 shapes that could be anyone's. Each tier-0 hit
says whether redaction removes that string (`missed_by_rules`: a bug) or not
(`outside_rules`: a rule worth adding). `DETECTOR_VERSION` in
`checker/patterns.py` records what it searches for.

Since 0.2.0 (`pattern-v22`, the same detector MedUI runs before every cloud request):

| Version | What it adds |
|---|---|
| v21 | Tier 2: phone groups with up to three spaces or a spaced hyphen between them; the value after a label on the identifier-labels list. Tier 0: the patient's phone digits in any spacing; their suburb, or address line 2's locality, with their postcode (a state may sit between); street types in rarer abbreviations, a hyphen between street words, a space dropped after the number or before the type. |
| v22 | Practitioners' preferred names, with no rule change: they come from the host's practitioner rows. A practice adds an inactive BP user under the preferred name (the queries keep inactive users deliberately), or a host adds rows of its own; each row's `user_id` must be unique to that name (e.g. `528p`), or its forms are taken for the BP name's. |

**Verdicts.** `Redactor.verdict(text, patient_id)` returns `{detector_version, lists, scanned_ms,
top_tier, hits, patient_identifiers}`: hits one per span with `tier, type, kind, coverage, start, end`
(offsets, never text); `patient_identifiers` is `loaded`, `unavailable` (tier 0 not searched, so not
clean) or `none`. `checker.scan.is_blocking(verdict)` is the pause rule (a tier 0 or 1 hit stops a request
for review); `checker.scan.summarise(verdict)` counts findings by tier, type and coverage for a display.
`lists` is a short fingerprint of the practice's own lists (`always_redact`, `name_variants`,
`identifier_labels`, `first_names`): two results with the same ruleset and fingerprint used the same
rules and lists. `Redactor.fingerprint()` gives it on its own.
