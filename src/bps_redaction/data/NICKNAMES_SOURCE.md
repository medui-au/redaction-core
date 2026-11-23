# nicknames.csv — provenance

Source: https://github.com/carltonnorthern/nicknames
Licence: Apache-2.0
Retrieved: 2026-09-20

A CSV of US given names and their diminutives, as semantic triplets
`(name1, has_nickname, name2)`, built by Old Dominion University's Web Science
and Digital Libraries Research Group from genealogy data.

**Stated limitations, which matter here:** the source data is heavily biased
towards traditionally African American names, and the project makes no claim of
coverage for UK or Australian naming. The checker therefore supplements it with
generated Australian diminutive forms (`-o`, `-zza`, `-y`/`-ie`) — see
`checker/name_forms.py`.

Used **only as audit search terms**. Nothing from this file is fed into
redaction: over-redaction there would replace ordinary words ("will", "may",
"mark") with tokens and corrupt the clinical text. Redaction's shortened names
come from the practice's own BP preferred names and the reviewed name_variants
list instead (`nicknames.py`).

# name_forms.csv — provenance (0.2.14, MedUI ADR-148)

Written for this package on 2026-11-14 from general knowledge of Australian
given names: common short forms (Michael -> Mick, Margaret -> Marg) and
spelling pairs (Sarah <-> Sara). **Not taken from any practice's records.**
Reviewed by the practice that commissioned it; forms rare enough to point at
one person, every everyday-word name (`EVERYDAY_WORD_NAMES`) and forms that
are clinical abbreviations (reg, cath, seb, dom, des, theo) are left out. Unlike nicknames.csv, this file **is** used by redaction
(`nicknames.curated_forms`). Released under the package licence.
