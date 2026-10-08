"""Turkish-aware text cleaning and folding.

`fold()` maps every character to exactly ONE character (lower-case, Turkish
letters to ASCII: İ/I/ı/i -> i, ş -> s, ğ -> g, ü -> u, ö -> o, ç -> c ...).
Because the length is preserved, match positions found in the folded text are
valid positions in the original text, which lets us highlight hits in the
original without storing offsets.

`clean_text()` is applied once to extracted PDF text (stored as the original);
`fold()` produces the searchable `text_norm` column and normalizes queries.
"""

from __future__ import annotations

import re
import unicodedata

_TURKISH = str.maketrans(
    {
        "İ": "i",
        "I": "i",
        "ı": "i",
        "Ş": "s",
        "ş": "s",
        "Ğ": "g",
        "ğ": "g",
        "Ü": "u",
        "ü": "u",
        "Ö": "o",
        "ö": "o",
        "Ç": "c",
        "ç": "c",
        "Â": "a",
        "â": "a",
        "Î": "i",
        "î": "i",
        "Û": "u",
        "û": "u",
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
    }
)

_SPACES_RE = re.compile(r"[ \t  -​  　]+")
_TRAILING_RE = re.compile(r" +\n")
_MANY_NEWLINES_RE = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    """Normalize raw extracted text (kept as the 'original' text)."""
    text = unicodedata.normalize("NFKC", text)
    # "i" + COMBINING DOT ABOVE appears when "İ" was lower-cased by a tool.
    text = text.replace("i̇", "i").replace("\x00", "").replace("­", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACES_RE.sub(" ", text)
    text = _TRAILING_RE.sub("\n", text)
    text = _MANY_NEWLINES_RE.sub("\n\n", text)
    return text.strip()


def _fold_char(ch: str) -> str:
    low = ch.lower()
    if len(low) != 1:
        low = ch
    if low.isascii():
        return low
    decomposed = unicodedata.normalize("NFKD", low)
    base = decomposed[0] if decomposed else low
    return base if base.isascii() and not unicodedata.combining(base) else low


def fold(text: str) -> str:
    """Length-preserving, case- and Turkish-diacritic-insensitive form."""
    return "".join(_fold_char(ch) for ch in text.translate(_TURKISH))


WORD_RE = re.compile(r"\w+", re.UNICODE)


def fold_tokens(text: str) -> list[str]:
    return WORD_RE.findall(fold(text))


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
