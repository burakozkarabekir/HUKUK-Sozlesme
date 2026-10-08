"""Page-level PDF text extraction: pypdf first, pdfplumber as fallback.

A document is flagged `needs_ocr` when most pages yield (almost) no text,
which is typical for scanned contracts.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

from app.errors import AppError
from app.ingest.normalize import clean_text

logger = logging.getLogger(__name__)
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pypdf").setLevel(logging.ERROR)

PDF_MAGIC = b"%PDF"


class PdfExtractError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__("pdf_unreadable", message, 422)


@dataclass
class PageText:
    page_no: int
    text: str


@dataclass
class ExtractResult:
    pages: list[PageText]
    method: str
    low_text_pages: list[int] = field(default_factory=list)
    needs_ocr: bool = False

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def char_count(self) -> int:
        return sum(len(p.text) for p in self.pages)

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text)


def looks_like_pdf(data: bytes) -> bool:
    return data[:1024].lstrip().startswith(PDF_MAGIC) or PDF_MAGIC in data[:1024]


def _pypdf_pages(data: bytes) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        reader.decrypt("")  # many "protected" PDFs only restrict editing
    texts = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception:  # one broken page must not lose the document
            logger.warning("pypdf_page_failed", exc_info=True)
            texts.append("")
    return texts


def _pdfplumber_pages(data: bytes, only: set[int] | None = None) -> dict[int, str]:
    import pdfplumber

    result: dict[int, str] = {}
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for index, page in enumerate(pdf.pages):
            if only is not None and index not in only:
                continue
            try:
                result[index] = page.extract_text() or ""
            except Exception:
                logger.warning("pdfplumber_page_failed", exc_info=True)
                result[index] = ""
    return result


def extract_pdf(data: bytes, min_chars_per_page: int = 40) -> ExtractResult:
    if not looks_like_pdf(data):
        raise PdfExtractError("Dosya geçerli bir PDF değil.")
    method = "pypdf"
    try:
        raw_pages = _pypdf_pages(data)
    except Exception:
        logger.warning("pypdf_failed_trying_pdfplumber", exc_info=True)
        try:
            plumber = _pdfplumber_pages(data)
        except Exception as exc:
            raise PdfExtractError("PDF okunamadı (dosya bozuk veya şifreli olabilir).") from exc
        raw_pages = [plumber[i] for i in sorted(plumber)]
        method = "pdfplumber"

    pages = [clean_text(text) for text in raw_pages]
    weak = {i for i, text in enumerate(pages) if len(text) < min_chars_per_page}
    if weak and method == "pypdf":
        try:
            for index, text in _pdfplumber_pages(data, weak).items():
                cleaned = clean_text(text)
                if len(cleaned) > len(pages[index]):
                    pages[index] = cleaned
                    method = "pypdf+pdfplumber"
        except Exception:
            logger.warning("pdfplumber_retry_failed", exc_info=True)

    low = [i + 1 for i, text in enumerate(pages) if len(text) < min_chars_per_page]
    result = ExtractResult(
        pages=[PageText(page_no=i + 1, text=text) for i, text in enumerate(pages)],
        method=method,
        low_text_pages=low,
    )
    # VARSAYIM: A document needs OCR when more than half of its pages have
    # fewer than `min_chars_per_page` characters (or it has no pages at all).
    result.needs_ocr = not pages or len(low) > len(pages) / 2
    return result
