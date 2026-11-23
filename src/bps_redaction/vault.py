"""TokenVault — deterministic per-run PII token minter.

HMAC-SHA256(run_id + original) produces a stable hex digest so the same name
always maps to the same token within a run, and different runs produce different
tokens (run isolation). Completely separate from crypto.py (API key storage).
"""

import hashlib
import hmac

from .escapes import unmask


class TokenVault:
    """Mint and reverse PII replacement tokens for a single pipeline run."""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._token_to_original: dict[str, str] = {}
        self._original_to_token: dict[str, str] = {}

    def mint_patient(self, original: str) -> str:
        """Return [Redacted Patient: PT-<hex>] for original text."""
        return self._mint(original, prefix='PT', label='Patient')

    def mint_practice(self, original: str) -> str:
        """Return [Redacted Practice: PR-<hex>] for a practice contact detail.

        Kept distinct from patient and HCP tokens so a reader — and a reviewer
        of flags — can tell whose identifier was removed.
        """
        return self._mint(original, prefix='PR', label='Practice')

    def mint_name(self, original: str) -> str:
        """Return [Redacted Name: NM-<hex>] for a titled name we can't attribute.

        Distinct from patient and HCP tokens on purpose: this is someone the
        contacts book doesn't know — a misspelling, or a practitioner from
        outside the practice — so a reviewer should be able to tell it apart
        from a confirmed identification.
        """
        return self._mint(original, prefix='NM', label='Name')

    def mint_email(self, original: str) -> str:
        """Return [Redacted Email: EM-<hex>] for an address nobody owns here.

        Not the patient's and not the practice's — those have their own
        tokens. One token per address, so two addresses stay distinguishable
        and de-redaction can put each back where the output needs it.
        """
        return self._mint(original, prefix='EM', label='Email')

    def mint_phone(self, original: str) -> str:
        """Return [Redacted Phone: PH-<hex>] for a phone number nobody owns here."""
        return self._mint(original, prefix='PH', label='Phone')

    def mint_medicare(self, original: str) -> str:
        """Return [Redacted Medicare: MC-<hex>] for a Medicare number nobody owns here.

        The patient's own card is a PT token; this is someone else's — a
        relative's, or a report pasted into the wrong chart (ADR-034).
        """
        return self._mint(original, prefix='MC', label='Medicare')

    def mint_provider(self, original: str) -> str:
        """Return [Redacted Provider: PV-<hex>] for a Medicare provider number (ADR-061).

        Reversible: a referral the model drafts gets the number back.
        """
        return self._mint(original, prefix='PV', label='Provider')

    def mint_number(self, original: str) -> str:
        """Return [Redacted Number: NB-<hex>] for an unlabelled 8-digit number.

        Shaped like a local phone number, but with nothing saying it is one —
        some are record or reference numbers. Calling those "Phone" would
        mislead the model; this makes the weaker claim.
        """
        return self._mint(original, prefix='NB', label='Number')

    def mint_usual_practice(self, canonical: str) -> str:
        """Return [Redacted UsualPractice: UP-<hex>] for the practice's own name (ADR-121).

        Keyed on the clean form of the name, not the text matched: a cut-off or
        all-capitals copy shares the clean copy's token and de-redacts to it.
        One word, so every token pattern still reads it.
        """
        return self._mint(canonical, prefix='UP', label='UsualPractice')

    def mint_listed(self, original: str, label: str) -> str:
        """Return [Redacted <label>: AR-<hex>] for a string on the always-redact list.

        The label says what the string is (HCP, Place, Org …); the AR prefix says
        which pass caught it, so a reviewer can tell a listed string from one
        matched against a record.
        """
        return self._mint(original, prefix='AR', label=label)

    def mint_hcp(self, original: str, specialty: str = '') -> str:
        """Return [Redacted HCP: HP-<hex>] for original text.

        Specialty is mixed into the hash for uniqueness but NOT stored in the reverse
        map — de-redaction restores the clean name only.
        """
        norm = unmask(original).strip()          # never a placeholder (ADR-116)
        if norm in self._original_to_token:
            return self._original_to_token[norm]

        hash_key = f'{norm}|{specialty}' if specialty else norm
        digest = hmac.new(
            self._run_id.encode(),
            hash_key.encode(),
            hashlib.sha256,
        ).hexdigest()[:12]

        token = f'[Redacted HCP: HP-{digest}]'
        self._original_to_token[norm] = token
        self._token_to_original[token] = norm
        return token

    def get_reverse_map(self) -> dict[str, str]:
        """Return {token → original_text} for de-redaction."""
        return dict(self._token_to_original)

    def _mint(self, original: str, prefix: str, label: str) -> str:
        norm = unmask(original).strip()          # never a placeholder (ADR-116)
        existing = self._original_to_token.get(norm)
        # The patient's label wins (ADR-052). The HCP pass mints a token for
        # every name on the practitioner list, so a patient whose name matches
        # an address-book doctor's was labelled an HCP for the whole run. A
        # patient token now replaces it from here on; the earlier token still
        # de-redacts, so text already carrying it is unaffected.
        # The practice's own name wins the same way over any other label but the
        # patient's (ADR-126): the HCP pass pre-mints every address-book name, and a
        # contact entered as the practice's site took its token in a shared vault.
        if existing and not (prefix == 'PT' and not existing.startswith('[Redacted Patient:')) \
                and not (prefix == 'UP' and not existing.startswith(('[Redacted Patient:',
                                                                      '[Redacted UsualPractice:'))):
            return existing

        digest = hmac.new(  # type: ignore[attr-defined]  # stdlib alias
            self._run_id.encode(),
            norm.encode(),
            hashlib.sha256,
        ).hexdigest()[:12]

        token = f'[Redacted {label}: {prefix}-{digest}]'
        self._original_to_token[norm] = token
        self._token_to_original[token] = norm
        return token
