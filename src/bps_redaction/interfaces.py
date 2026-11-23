"""What the rules need from outside: identity rows, admin lists, a slow cache.

The rules were written against Best Practice (BP) Premier, so identity data
comes in as BP-shaped rows from named queries: the query names and their
columns are the contract (see adapters/sql/ for the reference SQL). An adapter
for another system returns rows with the same names and columns.
"""

from __future__ import annotations

from typing import Protocol, Sequence


class RowSource(Protocol):
    """Read-only identity data, as rows from a named query.

    Names used by the rules: patient_get_demographics, practice_locations,
    common_first_names, practice_doctors, practice_staff,
    hcp_practice_contacts, hcp_practice_contacts_fingerprint,
    hcp_patient_contacts_corrin, hcp_patient_contacts_corrout,
    practice_preferred_names, patient_first_name_counts; and for the service's /admin login,
    auth_admin_users and auth_get_user_by_id. May raise; the rules catch, log and fall back
    exactly as they do on a database error.
    """

    def query(self, name: str, params: Sequence | None = None) -> list[dict]: ...


class Lists(Protocol):
    """Administrator-curated tables: 'always_redact', 'name_variants', 'identifier_labels'.

    Returns the table's rows as dicts; [] when the table doesn't exist.
    """

    def rows(self, table: str) -> list[dict]: ...


class Cache(Protocol):
    """Slow-changing derived tables (the address book, the nickname table).

    Values are JSON strings; the rules decide freshness from timestamps they
    store inside the value.
    """

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, description: str = '') -> None: ...


class MemoryCache:
    """The default Cache: a dict, gone when the process ends."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str, description: str = '') -> None:
        self._data[key] = value


class StaticLists:
    """Lists held in memory: {'always_redact': [rows], 'name_variants': [rows]}."""

    def __init__(self, tables: dict[str, list[dict]] | None = None) -> None:
        self._tables = dict(tables or {})

    def rows(self, table: str) -> list[dict]:
        return list(self._tables.get(table, []))
