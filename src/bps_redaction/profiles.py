"""The redaction profiles: which passes run, in which order, for each kind of content.

Three profiles, for the three kinds of content an AI pipeline sends:
  record  — structured EHR data (JSON blocks) fetched for one patient
  input   — text a person typed or dictated
  prompt  — an assembled prompt, template included
With no patient (patient_id None), input and prompt run the "general" passes
only (ADR-053): everything that needs no patient record, practitioners from
the whole address book, organisations matched only as written.

A caller chains them: input over the caller's fields, then prompt over the
assembled prompt, with one TokenVault for the conversation.
"""

from __future__ import annotations

import re

from . import _env
from .escapes import mask, unmask
from .deredact import Restored, deredact
from .hcp import HCPRedactionService as HCP
from .interfaces import Cache, Lists, RowSource
from .redaction import RedactionService as R
from .vault import TokenVault


class IdentitySourceUnavailable(RuntimeError):
    """Redaction could not be done safely; nothing should be sent (fail closed)."""


def _is_control_value(value: str) -> bool:
    """True for short enum-like inputs ('normal', 'issues_list', '42').

    Those steer the pipeline rather than carrying clinical text, and a contact
    surname colliding with one would change behaviour rather than protect
    anyone. Anything with a space, punctuation, or real length is treated as
    text and redacted.
    """
    # Lower case only: 'Smith' as an input value is a name, 'issues_list' is a
    # setting. Redaction alters a value only when it matches a contact anyway,
    # so the narrower exemption costs nothing.
    return len(value) <= 24 and bool(re.fullmatch(r'[a-z0-9_.-]+', value))


def redact_record(data, patient_id: int, vault: TokenVault):
    """Structured EHR data for one patient. Returns (redacted, identifiers_loaded).

    A literal \\n, \\t or \\r counts as a word boundary (ADR-116, escapes.py)."""
    redacted, loaded = _redact_record(mask(data), patient_id, vault)
    return unmask(redacted), loaded


def _redact_record(data, patient_id: int, vault: TokenVault):
    identifiers = R.get_identifiers(patient_id)
    # The patient's BP record number rides on every data row and the
    # model never needs it (ADR-031).
    redacted = R.drop_record_numbers(data)
    # Share links and private-network links first, whole — before a
    # name inside one becomes a token and splits it (ADR-043).
    redacted = R.redact_links(redacted)
    # Another service's identifier by its label (ADR-076).
    redacted = R.redact_identifier_labels(redacted)
    # The practice's own name, before a town in it is taken as a patient's
    # suburb or a contact's surname (ADR-121).
    redacted = R.redact_practice_name(redacted, vault)
    redacted = R.redact(redacted, identifiers, vault,
                        patterns=R.get_identifier_patterns(patient_id))

    hcp_map = HCP.build_hcp_map(patient_id)
    redacted = HCP.redact_hcp(redacted, hcp_map, vault)

    # The practice's own sites: address, phone, fax, email. Distinct
    # tokens ([Redacted Practice: PR-…]) so a reader can tell whose
    # identifier was removed.
    practice = R.get_practice_identifiers()
    if practice:
        redacted = R.redact(redacted, practice, vault, mint=vault.mint_practice)

    # The patient's street name left without its number — only now, so a
    # full address is already a reversible token and a practice on the
    # same street keeps its own (ADR-033).
    # With the practice's suburb and postcode, in order (ADR-066, ADR-067).
    redacted = R.redact_street_names(redacted, patient_id, vault)
    # Anyone else's address — a clinician's rooms, a pathology site — and
    # the patient's former ones (ADR-035).
    redacted = R.redact_other_addresses(redacted)
    # Anyone else's date of birth, by its label (ADR-047).
    redacted = R.redact_other_dobs(redacted)
    # Email addresses and phone numbers left over after the patient's and
    # the practice's own: someone else's.
    redacted = R.redact_unlinked_contacts(redacted, vault)
    # The admin always-redact list — more specific than the titled-name
    # shape, so it goes first and a listed person keeps their label (ADR-030).
    redacted = R.redact_always_list(redacted, vault)
    # Whoever sits in the recipient slot of BP's letter lines (ADR-032).
    redacted = R.redact_letter_recipients(redacted, vault)
    # A person named as a document's Author (ADR-038).
    redacted = R.redact_author_slots(redacted, vault)
    # Anyone titled whom the passes above did not identify.
    redacted = R.redact_titled_names(redacted, vault)
    # Last: the shortened names most patients with this name go by (ADR-048),
    # after the Author and titled-name passes so "Dr Tilly Smith" goes whole (ADR-056).
    redacted = R.redact_practice_nicknames(redacted, patient_id, vault)
    # Last of all: the practice's common first names, and a surname after one (ADR-058).
    redacted = R.redact_common_first_names(redacted, vault)
    return redacted, bool(identifiers)


def redact_input(values: dict, patient_id: int | None, vault: TokenVault,
                 clinician_first_names=None) -> dict:
    """See _redact_input; literal escapes count as word boundaries (ADR-116)."""
    return unmask(_redact_input(mask(values), patient_id, vault, clinician_first_names))


def _redact_input(values: dict, patient_id: int | None, vault: TokenVault,
                  clinician_first_names=None) -> dict:
    """The patient's identifiers, then practitioner names, in caller-supplied fields.

    Only string values are touched, and short enum-like ones are left alone.
    `clinician_first_names`, for a consultation transcript only: the recording
    clinician's first and preferred names, taken last in any case (ADR-133).
    Raises IdentitySourceUnavailable if the practitioner list can't be loaded:
    input that could not be redacted must not be sent.
    """
    out = dict(values)
    strings = {k: v for k, v in values.items()
               if isinstance(v, str) and v.strip() and not _is_control_value(v)}
    if not strings:
        return out
    try:
        hcp_map = HCP.build_hcp_map(patient_id)
    except Exception as exc:
        raise IdentitySourceUnavailable('practitioner names could not be loaded') from exc
    if not hcp_map:
        raise IdentitySourceUnavailable('the practitioner contact list came back empty')

    # The patient's own identifiers first (ADR-052): a name that is both the
    # patient's and an address-book doctor's is the patient's.
    identifiers = R.get_identifiers(patient_id) if patient_id else []
    patterns = R.get_identifier_patterns(patient_id) if identifiers else None
    for key, value in strings.items():
        value = R.redact_practice_name(value, vault)                      # ADR-121
        if identifiers:
            value = R.redact(value, identifiers, vault, patterns=patterns)
        value = HCP.redact_hcp(value, hcp_map, vault, organisations_as_written=not patient_id)
        if clinician_first_names:
            value = R.redact_clinician_first_names(value, clinician_first_names, vault)
        out[key] = value
    return out


def redact_prompt(text: str, patient_id: int | None, vault: TokenVault) -> str:
    """See _redact_prompt; literal escapes count as word boundaries (ADR-116)."""
    return unmask(_redact_prompt(mask(text), patient_id, vault)) if text else text


def _redact_prompt(text: str, patient_id: int | None, vault: TokenVault) -> str:
    """An assembled prompt. No practitioner pass: template text would collide
    with surnames, and the data blocks and input have had it already."""
    if not text:
        return text
    identifiers = R.get_identifiers(patient_id) if patient_id else []
    patterns = R.get_identifier_patterns(patient_id) if patient_id else None
    # Backstop for the record number, for blocks rendered in some other way.
    redacted = R.drop_record_numbers(text)
    # Share links and private-network links, whole (ADR-043).
    redacted = R.redact_links(redacted)
    # Another service's identifier by its label (ADR-076).
    redacted = R.redact_identifier_labels(redacted)
    redacted = R.redact_practice_name(redacted, vault)                     # ADR-121
    redacted = (R.redact(redacted, identifiers, vault, patterns=patterns)
                if identifiers else redacted)
    # Practice sites: specific strings, safe over the whole prompt.
    practice = R.get_practice_identifiers()
    if practice:
        redacted = R.redact(redacted, practice, vault, mint=vault.mint_practice)
    if patient_id:                                                        # ADR-033, and
        redacted = R.redact_street_names(redacted, patient_id, vault)     # ADR-066/067
    else:
        redacted = R.redact_practice_localities(redacted, vault)
    redacted = R.redact_other_addresses(redacted)                         # ADR-035
    redacted = R.redact_other_dobs(redacted)                              # ADR-047
    redacted = R.redact_unlinked_contacts(redacted, vault)
    redacted = R.redact_always_list(redacted, vault)                      # ADR-030
    redacted = R.redact_letter_recipients(redacted, vault)                # ADR-032
    redacted = R.redact_titled_names(redacted, vault)
    if patient_id:                                                        # ADR-048, after
        redacted = R.redact_practice_nicknames(redacted, patient_id, vault)  # titles (056)
    return R.redact_common_first_names(redacted, vault)                   # ADR-058, last


class Redactor:
    """The library's entry point: one per identity source.

    Holds the environment (source, admin lists, slow cache, and the process
    caches the rules keep) and runs each profile inside it.
    """

    def __init__(self, source: RowSource | None = None, lists: Lists | None = None,
                 cache: Cache | None = None, practice_first_names: bool = False) -> None:
        """`practice_first_names` (0.2.9, ADR-134): the first-name sweep also takes the
        practice's own users' first names, from practice_doctors and practice_staff."""
        self._env = _env.Env(source=source, practice_first_names=practice_first_names)
        if lists is not None:
            self._env.lists = lists
        if cache is not None:
            self._env.cache = cache

    @property
    def source(self) -> RowSource | None:
        return self._env.source

    def context(self):
        """A context in which a host may call the rule classes directly (0.2.1).

        For a host that keeps its own order of passes, as MedUI does for its
        patient-only policy: `with r.context(): RedactionService.redact(...)`.
        The profiles below enter it themselves.
        """
        return _env.using(self._env)

    def forget(self) -> None:
        """Drop the process caches the rules keep (0.2.1): address book, practitioners,
        lists, first names. For a host whose inputs changed (a list entry added, a
        practitioner's name) and that wants the change applied to the next call
        rather than when the caches expire. The slow Cache is left as it is."""
        from types import SimpleNamespace
        self._env.state = SimpleNamespace()

    @staticmethod
    def vault(key: str) -> TokenVault:
        """Tokens are stable for one key: use one per conversation."""
        return TokenVault(key)

    def record(self, data, patient_id: int, vault: TokenVault):
        with _env.using(self._env):
            return redact_record(data, patient_id, vault)

    def input(self, values: dict, patient_id: int | None, vault: TokenVault,
              clinician_first_names=None) -> dict:
        """`clinician_first_names`: pass for a consultation transcript only (ADR-133)."""
        with _env.using(self._env):
            return redact_input(values, patient_id, vault, clinician_first_names)

    def prompt(self, text: str, patient_id: int | None, vault: TokenVault) -> str:
        with _env.using(self._env):
            return redact_prompt(text, patient_id, vault)

    def check(self, text: str, patient_id: int | None) -> list[dict]:
        """Findings in already-redacted text: what survived."""
        from .checker.scan import scan
        with _env.using(self._env):
            return scan(text, patient_id)

    def verdict(self, text: str, patient_id: int | None) -> dict:
        """The full verdict on text about to be sent (0.2.0): see checker.scan.verdict."""
        from .checker.scan import verdict
        with _env.using(self._env):
            return verdict(text, patient_id)

    def fingerprint(self) -> str:
        """The practice's lists fingerprint (0.2.0), for a host to store beside RULESET."""
        from .fingerprint import lists_fingerprint
        with _env.using(self._env):
            return lists_fingerprint()

    def identifiers(self, patient_id: int) -> list[str]:
        with _env.using(self._env):
            return R.get_identifiers(patient_id)

    @staticmethod
    def deredact(text: str, token_map: dict[str, str]) -> Restored:
        return deredact(text, token_map)
