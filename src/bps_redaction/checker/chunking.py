"""Split a prompt into reviewable chunks.

Three reasons chunks exist, all of them load-bearing:
  - the decision model (M3) has a 32k-token window, and prompts here run to
    220k characters;
  - it does worse when the text it is judging is buried in unrelated material;
  - a finding needs a location, and a chunk is that location.

Splits on blank lines first, then single lines, and only mid-line as a last
resort. Consecutive chunks overlap so an identifier spanning a boundary is
still seen whole at least once.
"""
import hashlib

CHUNK_CHARS = 6000
OVERLAP_CHARS = 300


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[tuple[int, int]]:
    """Return (start, end) character offsets covering the whole text."""
    if not text:
        return []
    if len(text) <= size:
        return [(0, len(text))]

    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            end = _best_break(text, start, end)
        spans.append((start, end))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return spans


def _best_break(text: str, start: int, end: int) -> int:
    """Prefer a paragraph break, then a line break, within the last 20% of the chunk."""
    window = max(start + 1, end - (end - start) // 5)
    for separator in ('\n\n', '\n'):
        position = text.rfind(separator, window, end)
        if position > start:
            return position + len(separator)
    return end


def chunk_hash(text: str) -> str:
    """Stable id for a chunk's content, for de-duplication and verdict caching."""
    return hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()
