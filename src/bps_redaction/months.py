"""A name that is also a month, written as part of a date (MedUI ADR-118).

A patient called May or June, a nickname Jan: "12 May 2026", "May 12, 2026",
"in June", "since Jan" are dates, and redacting the month loses the date the
model needs. Redaction keeps such a month and the checker does not report it.
"to June", "by May" and the like are left to the name rules: a person fits there.
"""
import re

MONTH_WORDS = frozenset({
    'jan', 'january', 'feb', 'february', 'mar', 'march', 'apr', 'april', 'may', 'jun', 'june',
    'jul', 'july', 'aug', 'august', 'sep', 'sept', 'september', 'oct', 'october', 'nov',
    'november', 'dec', 'december'})

# A day before ("12 May", "12th of May", "3/Jan"), or a word that leads a month
# ("in", "since", "early" …), on the same line. Not after a full stop, and not the
# end of a slash date or a ratio: "12/03/21. June …" and "BP 120/80. June …" start
# a sentence with a name (ADR-124). "12. June 2026" still has its year after.
_BEFORE_RE = re.compile(
    r'(?:(?<![/\d])\b\d{1,2}(?:st|nd|rd|th)?(?:[ \t]+of)?[ \t]*[-/]?[ \t]*'
    r'|\b(?i:in|since|until|till|during|early|mid|late|last|next|this)[ \t]+)$')
# A year after, with or without a day: "May 2026", "May 12, 2026", "Jan. 2026", "May '26".
_AFTER_RE = re.compile(
    r"(?!['’]s\b)\.?,?[ \t/-]*(?:\d{1,2}(?:st|nd|rd|th)?,?[ \t]+)?(?:\d{4}|'\d{2})(?!\d)")
_POSSESSIVE_RE = re.compile(r"['’]s\b")


def month_in_date(text: str, start: int, end: int) -> bool:
    """True when text[start:end] is a month word written as part of a date."""
    if text[start:end].lower().rstrip('.') not in MONTH_WORDS:
        return False
    if _AFTER_RE.match(text, end):
        return True
    if _POSSESSIVE_RE.match(text, end):
        return False                                    # May's, June's: a person
    line_start = text.rfind('\n', 0, start) + 1
    return bool(_BEFORE_RE.search(text[max(line_start, start - 20):start]))
