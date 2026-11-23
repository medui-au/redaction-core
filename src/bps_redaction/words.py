"""The word list the ordinary-word rules use, bundled rather than read from the host.

data/words.txt is Debian's wamerican 2020.12.07 (SCOWL, see words.LICENSE): the
list the rules were measured with. Bundling it means the same text gives the
same result on every host (plan D10).
"""

from pathlib import Path

_DATA = Path(__file__).parent / 'data'


def word_list_path() -> str:
    return str(_DATA / 'words.txt')
