"""The public tree carries no internal host, note, edition or personal references (0.2.21)."""
import pathlib, re, subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(r'MedUI5|\.190\b|190-to-15|15-to-190|192\.168\.|/home/')


def test_no_internal_references():
    files = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True).stdout.split()
    hits = []
    for f in files:
        if f.startswith('src/bps_redaction/data/') or f == 'tests/test_distribution.py':
            continue
        try:
            text = (ROOT / f).read_text(encoding='utf-8')
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        hits += [f'{f}: {m.group(0)}' for m in FORBIDDEN.finditer(text)]
    assert not hits, hits


def test_security_policy_and_notice():
    assert 'Report a vulnerability' in (ROOT / 'SECURITY.md').read_text()
    assert 'H. Eviston (Medical) Pty Ltd' in (ROOT / 'NOTICE').read_text()
