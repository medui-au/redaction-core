"""Best Practice Premier, read-only, over ODBC.

Runs the reference queries in sql/ against BPSDB. Every read opens its own
connection and refuses anything that isn't a read. Needs `pyodbc` and the
Microsoft ODBC driver for SQL Server.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

_SQL_DIR = Path(__file__).parent / 'sql'
_WRITE_PREFIXES = ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP', 'ALTER', 'TRUNCATE')


def load_query(name: str) -> str:
    path = _SQL_DIR / f'{name}.sql'
    if not path.is_file():
        raise KeyError(f'unknown query: {name!r}')
    return path.read_text()


class BPSource:
    """A RowSource over a BP database. connection_string: a pyodbc DSN string."""

    def __init__(self, connection_string: str) -> None:
        self._conn_str = connection_string

    def query(self, name: str, params: Sequence | None = None) -> list[dict]:
        import pyodbc

        # Two queries are written with a named parameter; ODBC takes '?'.
        # One takes a list: '{placeholders}' becomes a '?' per value.
        sql = load_query(name).replace(':patient_id', '?')
        if '{placeholders}' in sql:
            sql = sql.replace('{placeholders}', ','.join('?' * len(params or [])))
        stripped = sql.strip().upper()
        if stripped.startswith(_WRITE_PREFIXES):
            raise RuntimeError('BPSDB is read-only')
        conn = pyodbc.connect(self._conn_str, autocommit=True)
        try:
            cursor = conn.cursor()
            if params:
                cursor.execute(sql, list(params))
            else:
                cursor.execute(sql)
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()
