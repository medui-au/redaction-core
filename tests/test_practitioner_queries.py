"""Practitioner queries keep inactive and deleted users (MedUI ADR-112).

A practice can enter a "shadow" BP user under a practitioner's preferred name so that
tools reading only BP redact and detect it. That works only while these queries have
no status filter; this test fails if one is added.
"""
import re
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[1] / "src/bps_redaction/adapters/sql"
FILES = ('practice_doctors.sql', 'practice_staff.sql', 'practice_users.sql', 'hcp_practice_contacts.sql')


def _code(path):
    return "\n".join(l.split("--")[0] for l in path.read_text().splitlines())


def test_no_status_filter_on_practitioner_queries():
    for name in FILES:
        code = _code(SQL_DIR / name)
        assert not re.search(r"\b(USERSTATUS|RECORDSTATUS)\b", code, re.I), name

