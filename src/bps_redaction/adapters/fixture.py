"""A JSON fixture: identity data for trying the library, tests and CI, without BP.

The file maps query names to rows, with patient queries keyed by patient id:

    {
      "patients": {"1001": {"demographics": {...},
                            "contacts_in": [...], "contacts_out": [...]}},
      "practice_locations": [...], "hcp_practice_contacts": [...],
      "practice_doctors": [...], "practice_staff": [...], "practice_users": [...],
      "common_first_names": [...], "practice_preferred_names": [...],
      "bp_users": [...]        # USERS rows, for the /admin login queries
    }

Rows use the BP column names (see sql/). Everything here must be invented.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

_PATIENT_QUERIES = {
    'patient_get_demographics': 'demographics',
    'hcp_patient_contacts_corrin': 'contacts_in',
    'hcp_patient_contacts_corrout': 'contacts_out',
}


class FixtureSource:
    def __init__(self, data: dict | str | Path) -> None:
        if not isinstance(data, dict):
            data = json.loads(Path(data).read_text())
        self._data = data

    def query(self, name: str, params: Sequence | None = None) -> list[dict]:
        if name in _PATIENT_QUERIES:
            patient = self._data.get('patients', {}).get(str(params[0]) if params else '')
            if not patient:
                return []
            rows = patient.get(_PATIENT_QUERIES[name], [])
            return [rows] if isinstance(rows, dict) else list(rows)
        if name == 'auth_get_user_by_id':
            return [u for u in self._data.get('bp_users', []) if u.get('USERID') == params[0]]
        if name == 'auth_admin_users':
            return [{'Name': f"{u['Firstname']} {u['Surname']}", 'UserId': u['USERID']}
                    for u in self._data.get('bp_users', []) if u.get('GroupCode') in (params or [])]
        if name == 'hcp_practice_contacts_fingerprint':
            contacts = self._data.get('hcp_practice_contacts', [])
            return [{'cnt': len(contacts),
                     'max_id': max((c.get('CONTACTID', 0) for c in contacts), default=0)}]
        return list(self._data.get(name, []))
