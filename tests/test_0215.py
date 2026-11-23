"""0.2.15: a malformed observation no longer stops a patient's identifiers loading.

The identifiers query (patient_get_demographics.sql) also returns the latest height and
weight, which no rule reads. They were read with a plain CAST, so one weight typed with
its unit ("80kg") made SQL Server fail the whole query; the core then had no identifiers
for the patient and every redaction for them failed closed until someone corrected BP
(the practice's measure). Both now use TRY_CAST, as MedUI ADR-145 does for MedUI's own
query: a malformed value is blank. No redaction output changes, so RULESET is unchanged.
"""
import re
from importlib import resources

from bps_redaction import RULESET, __version__


def _sql() -> str:
    return (resources.files('bps_redaction') / 'adapters' / 'sql'
            / 'patient_get_demographics.sql').read_text()


def test_version():
    assert tuple(int(p) for p in __version__.split('.')) >= (0, 2, 15)
    assert RULESET.startswith('medui-adr')


def test_observation_values_are_never_cast_plainly():
    # Every conversion of an observation's DataValue goes through TRY_CAST.
    sql = re.sub(r'--[^\n]*', '', _sql())
    assert 'RD.DataValue' in sql
    for m in re.finditer(r'(?<!TRY_)CAST\(([^()]*(?:\([^()]*\))?[^()]*)', sql):
        assert 'DataValue' not in m.group(1), m.group(0)
    assert sql.count('TRY_CAST(TRIM(RD.DataValue)') == 2


def test_no_implicit_arithmetic_on_observation_text():
    # "RD.DataValue / 100" converts implicitly and fails on text the same way.
    sql = re.sub(r'--[^\n]*', '', _sql())
    assert not re.search(r'RD\.DataValue\s*[-+*/]', sql)
