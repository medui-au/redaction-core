"""bps_redaction: rule-based redaction and de-redaction of patient text.

    from bps_redaction import Redactor
    from bps_redaction.adapters.bp import BPSource

    redactor = Redactor(BPSource(connection_string))
    vault = redactor.vault(conversation_key)
    blocks, loaded = redactor.record(blocks, patient_id, vault)
    prompt = redactor.prompt(prompt, patient_id, vault)
    reply = redactor.deredact(model_reply, vault.get_reverse_map()).text
"""

from .deredact import Restored, deredact
from .interfaces import Cache, Lists, MemoryCache, RowSource, StaticLists
from .profiles import IdentitySourceUnavailable, Redactor
from .vault import TokenVault

__version__ = '0.2.21'

# Which rules this core carries: the last decision record (ADR, see
# docs/RULES.md) included. A response reports it, so a client knows what it
# was redacted with.
RULESET = 'medui-adr155'

__all__ = ['RULESET', 'Redactor', 'TokenVault', 'IdentitySourceUnavailable', 'deredact', 'Restored',
           'RowSource', 'Lists', 'Cache', 'MemoryCache', 'StaticLists']
