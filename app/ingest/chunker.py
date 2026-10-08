"""Split contract text into clause-level chunks (Turkish contract conventions).

Recognised headings (at the start of a line):
    MADDE 7 - FESİH / Madde 7: Fesih / MADDE 7 (title on the next line)
    7. FESİH / 7) Fesih / 7.2 Bildirim süresi        (numbered documents)
    EK-1 / Ek 2: Fiyat Listesi / EK-III              (attachments)

Strategy:
1. If the text has >= 2 "MADDE n" headings, those (plus EK headings) define the
   clauses. Inside a long clause, sub-numbers of the same article ("7.2") split
   it further into "Madde 7.2" chunks.
2. Otherwise, if it has >= 3 numbered headings ("5. FESİH"), those are used.
3. Otherwise ~1500-character paragraph chunks with 200 characters of overlap.

Every chunk keeps page_start/page_end of the lines it covers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.ingest.pdf_extract import PageText

CLAUSE_MAX_CHARS = 3000
FALLBACK_CHUNK_CHARS = 1500
FALLBACK_OVERLAP_CHARS = 200
TITLE_MAX_CHARS = 90

_MADDE_RE = re.compile(r"^\s*(?:MADDE|Madde|madde)\s+(\d{1,3}(?:\.\d{1,3})*)\b(.*)$")
_NUMBERED_RE = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3})*)([.)])?\s+(\S.*)$")
_EK_RE = re.compile(r"^\s*(?:EK|Ek|ek)\s*[-–—:.]?\s*(\d{1,3}|[IVX]{1,6})\b(.*)$")
_SEPARATORS = "-–—:.)"
_APOSTROPHES = ("'", "’", "`")


@dataclass
class Chunk:
    ordinal: int
    clause_ref: str | None
    clause_title: str | None
    page_start: int
    page_end: int
    text: str


@dataclass
class _Line:
    page: int
    text: str


@dataclass
class _Heading:
    kind: str  # madde | numbered | ek
    number: str
    title: str


def is_upper_title(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 3:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) >= 0.7


def _clean_title(text: str) -> str:
    return text.strip().strip(_SEPARATORS).strip()[:TITLE_MAX_CHARS] or ""


def _title_after_number(rest: str) -> tuple[bool, str]:
    """Decide whether the text after "MADDE 7" / "EK-1" makes it a heading."""
    if rest.startswith(_APOSTROPHES):
        return False, ""  # "Madde 7'de belirtilen ..." is a reference, not a heading
    stripped = rest.strip()
    if not stripped:
        return True, ""
    if stripped[0] in _SEPARATORS:
        return True, _clean_title(stripped[1:])
    if is_upper_title(stripped):
        return True, _clean_title(stripped)
    return False, ""


def parse_heading(line: str) -> _Heading | None:
    match = _MADDE_RE.match(line)
    if match:
        ok, title = _title_after_number(match.group(2))
        return _Heading("madde", match.group(1), title) if ok else None
    match = _EK_RE.match(line)
    if match:
        ok, title = _title_after_number(match.group(2))
        return _Heading("ek", match.group(1), title) if ok else None
    match = _NUMBERED_RE.match(line)
    if match:
        number, delimiter, rest = match.group(1), match.group(2), match.group(3).strip()
        is_sub = "." in number
        if not is_sub and not delimiter:
            return None  # "5 adet ..." is not a heading
        short = len(rest) <= 80 and not rest.endswith((",", ";"))
        if is_upper_title(rest) and short:
            return _Heading("numbered", number, _clean_title(rest))
        if is_sub:
            # "7.2 Bayi, ..." - sub-clause with body text on the same line.
            return _Heading("numbered", number, "")
        words = rest.split()
        if short and len(words) <= 8 and not rest.endswith(".") and rest[:1].isupper():
            return _Heading("numbered", number, _clean_title(rest))
    return None


def _lines(pages: list[PageText]) -> list[_Line]:
    lines: list[_Line] = []
    for page in pages:
        for text in page.text.split("\n"):
            lines.append(_Line(page.page_no, text))
    return lines


def _text(lines: list[_Line]) -> str:
    return "\n".join(line.text for line in lines).strip()


def _split_long(lines: list[_Line], max_chars: int, overlap: int) -> list[list[_Line]]:
    """Greedy split on line boundaries, preferring blank lines; with overlap."""
    groups: list[list[_Line]] = []
    current: list[_Line] = []
    size = 0
    for line in lines:
        current.append(line)
        size += len(line.text) + 1
        if size >= max_chars:
            # Cut at the last paragraph break in the second half, if any.
            cut = len(current)
            for i in range(len(current) - 1, len(current) // 2, -1):
                if not current[i].text.strip():
                    cut = i + 1
                    break
            groups.append(current[:cut])
            carry: list[_Line] = []
            carried = 0
            for prev in reversed(current[:cut]):
                if carried >= overlap:
                    break
                carry.insert(0, prev)
                carried += len(prev.text) + 1
            current = carry + current[cut:]
            size = sum(len(x.text) + 1 for x in current)
            if size >= max_chars:  # one enormous line: hard cut
                groups.append(current)
                current, size = [], 0
    if current and _text(current) and (not groups or current != groups[-1][-len(current) :]):
        groups.append(current)
    return [g for g in groups if _text(g)]


@dataclass
class _Section:
    ref: str | None
    title: str | None
    lines: list[_Line]
    article: str | None = None  # "7" for "Madde 7", used for sub-splitting


def _choose_mode(headings: list[_Heading | None]) -> str:
    kinds = [h.kind for h in headings if h]
    if kinds.count("madde") >= 2:
        return "madde"
    top_level = [h for h in headings if h and h.kind == "numbered" and "." not in h.number]
    if len(top_level) >= 3:
        return "numbered"
    if "ek" in kinds:
        return "ek_only"
    return "fallback"


def _sections(lines: list[_Line], file_label: str | None) -> list[_Section]:
    parsed = [parse_heading(line.text) for line in lines]
    mode = _choose_mode(parsed)
    sections: list[_Section] = []
    current = _Section(ref=file_label or "Giriş", title=None, lines=[])
    ek_prefix: str | None = None
    for index, (line, heading) in enumerate(zip(lines, parsed, strict=True)):
        starts = heading is not None and (
            heading.kind == "ek"
            or (mode == "madde" and heading.kind == "madde")
            or (mode == "numbered" and heading.kind == "numbered" and "." not in heading.number)
        )
        if heading is None or not starts:
            current.lines.append(line)
            continue
        if _text(current.lines):
            sections.append(current)
        title = heading.title
        if not title:
            # Title on the following line: "MADDE 7" / "FESİH"
            following = next((x.text.strip() for x in lines[index + 1 : index + 3] if x.text.strip()), "")
            if following and len(following) <= TITLE_MAX_CHARS and is_upper_title(following):
                title = _clean_title(following)
        if heading.kind == "ek":
            ek_prefix = f"Ek-{heading.number}"
            ref, article = ek_prefix, None
        else:
            ref = f"Madde {heading.number}"
            if ek_prefix:
                ref = f"{ek_prefix} {ref}"
            article = heading.number
        current = _Section(ref=ref, title=title or None, lines=[line], article=article)
    if _text(current.lines):
        sections.append(current)
    if mode == "fallback":
        for section in sections:
            section.ref = file_label  # no structure: no clause reference
    return sections


def _sub_split(section: _Section) -> list[_Section]:
    """Split a long article on its own sub-numbers (7.1, 7.2 ...)."""
    if section.article is None or len(_text(section.lines)) <= CLAUSE_MAX_CHARS:
        return [section]
    prefix = section.article + "."
    parts: list[_Section] = []
    current = _Section(section.ref, section.title, [], section.article)
    for line in section.lines[1:] if section.lines else []:
        heading = parse_heading(line.text)
        if heading and heading.kind == "numbered" and heading.number.startswith(prefix):
            if _text(current.lines):
                parts.append(current)
            base_ref = section.ref.rsplit("Madde", 1)[0] if section.ref else ""
            current = _Section(f"{base_ref}Madde {heading.number}".strip(), section.title, [line], section.article)
        else:
            current.lines.append(line)
    if _text(current.lines):
        parts.append(current)
    if len(parts) <= 1:
        return [section]
    # Keep the article heading line with the first part.
    parts[0].lines.insert(0, section.lines[0])
    return parts


def chunk_pages(pages: list[PageText], file_label: str | None = None) -> list[Chunk]:
    """Chunk one file. `file_label` (e.g. "Ek: Fiyat Listesi.pdf") is used as the
    clause reference for unstructured attachment files."""
    lines = _lines(pages)
    if not _text(lines):
        return []
    chunks: list[Chunk] = []
    for section in _sections(lines, file_label):
        is_fallback_section = section.article is None and not (section.ref or "").startswith("Ek-")
        max_chars = FALLBACK_CHUNK_CHARS if is_fallback_section else CLAUSE_MAX_CHARS
        for part in _sub_split(section):
            for group in _split_long(part.lines, max_chars, FALLBACK_OVERLAP_CHARS):
                chunks.append(
                    Chunk(
                        ordinal=len(chunks),
                        clause_ref=part.ref,
                        clause_title=part.title,
                        page_start=group[0].page,
                        page_end=group[-1].page,
                        text=_text(group),
                    )
                )
    return chunks
