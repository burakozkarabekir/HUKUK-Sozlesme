from __future__ import annotations

import pytest

from app.ingest.chunker import FALLBACK_CHUNK_CHARS, chunk_pages, parse_heading
from app.ingest.pdf_extract import PageText


def pages(*texts: str) -> list[PageText]:
    return [PageText(page_no=i + 1, text=t) for i, t in enumerate(texts)]


@pytest.mark.parametrize(
    ("line", "kind", "number", "title"),
    [
        ("MADDE 7 - FESİH", "madde", "7", "FESİH"),
        ("Madde 5 – Sözleşmenin Süresi", "madde", "5", "Sözleşmenin Süresi"),
        ("MADDE 12: GİZLİLİK", "madde", "12", "GİZLİLİK"),
        ("MADDE 3", "madde", "3", ""),
        ("Madde 7.2 -", "madde", "7.2", ""),
        ("MADDE 9 MÜCBİR SEBEP", "madde", "9", "MÜCBİR SEBEP"),
        ("EK-1 FİYAT LİSTESİ", "ek", "1", "FİYAT LİSTESİ"),
        ("EK 2", "ek", "2", ""),
        ("Ek-3: Teknik Şartname", "ek", "3", "Teknik Şartname"),
        ("EK-IV", "ek", "IV", ""),
        ("5. FESİH", "numbered", "5", "FESİH"),
        ("5) Sözleşmenin Feshi", "numbered", "5", "Sözleşmenin Feshi"),
        ("5.1 Taraflar 90 gün önceden bildirerek feshedebilir.", "numbered", "5.1", ""),
    ],
)
def test_headings(line: str, kind: str, number: str, title: str) -> None:
    heading = parse_heading(line)
    assert heading is not None
    assert (heading.kind, heading.number, heading.title) == (kind, number, title)


@pytest.mark.parametrize(
    "line",
    [
        "Madde 7'de belirtilen süreler saklıdır.",
        "madde 5 uyarınca bildirim yapılır",
        "Ek olarak bayi şunları yapar",
        "EK-1'de yer alan fiyatlar",
        "5 adet araç teslim edilir.",
        "2024 yılında imzalanmıştır.",
        "3. kişilere karşı sorumluluk bayiye aittir ve bu sorumluluk devam eder.",
    ],
)
def test_not_headings(line: str) -> None:
    assert parse_heading(line) is None


def test_madde_chunks_with_pages_and_preamble() -> None:
    chunks = chunk_pages(
        pages(
            "BAYİLİK SÖZLEŞMESİ\nTaraflar aşağıdadır.\nMADDE 1 - TARAFLAR\nA ve B.\nMADDE 2 - KONU\nKonu satış.",
            "devamı ikinci sayfada.\nMADDE 3\nFESİH\nTaraflar 90 gün önceden bildirir.",
        )
    )
    assert [(c.clause_ref, c.clause_title, c.page_start, c.page_end) for c in chunks] == [
        ("Giriş", None, 1, 1),
        ("Madde 1", "TARAFLAR", 1, 1),
        ("Madde 2", "KONU", 1, 2),
        ("Madde 3", "FESİH", 2, 2),
    ]
    assert chunks[3].text.startswith("MADDE 3\nFESİH")
    assert [c.ordinal for c in chunks] == [0, 1, 2, 3]


def test_reference_lines_do_not_split_clauses() -> None:
    chunks = chunk_pages(pages("MADDE 1 - A\nMadde 2'de belirtilen hükümler saklıdır.\nMADDE 2 - B\nmetin"))
    assert [c.clause_ref for c in chunks] == ["Madde 1", "Madde 2"]
    assert "Madde 2'de" in chunks[0].text


def test_ek_section_inside_main_document_prefixes_articles() -> None:
    chunks = chunk_pages(pages("MADDE 1 - A\nx\nMADDE 2 - B\ny\nEK-1 PROTOKOL\nGiriş metni\nMADDE 1 - EK KONU\nz"))
    assert [c.clause_ref for c in chunks] == ["Madde 1", "Madde 2", "Ek-1", "Ek-1 Madde 1"]
    assert chunks[2].clause_title == "PROTOKOL"


def test_numbered_document() -> None:
    chunks = chunk_pages(pages("1. TANIMLAR\na\n2. KONU\nb\n3. FESİH\nc\n4. GİZLİLİK\nd"))
    assert [c.clause_ref for c in chunks] == ["Madde 1", "Madde 2", "Madde 3", "Madde 4"]
    assert chunks[2].clause_title == "FESİH"


def test_long_article_is_split_on_sub_numbers() -> None:
    body1 = "7.1 Birinci alt madde. " + "a " * 1000
    body2 = "7.2 İkinci alt madde. " + "b " * 1000
    chunks = chunk_pages(pages(f"MADDE 7 - FESİH\n{body1}\n{body2}\nMADDE 8 - SON\nson"))
    refs = [c.clause_ref for c in chunks]
    assert refs == ["Madde 7.1", "Madde 7.2", "Madde 8"]
    assert chunks[0].text.startswith("MADDE 7 - FESİH")
    assert chunks[1].clause_title == "FESİH"


def test_fallback_paragraph_chunks_with_overlap() -> None:
    paragraphs = [f"Paragraf {i}. " + ("kelime " * 60) for i in range(12)]
    chunks = chunk_pages(pages("\n\n".join(paragraphs[:6]), "\n\n".join(paragraphs[6:])))
    assert len(chunks) > 2
    assert all(c.clause_ref is None for c in chunks)
    assert all(len(c.text) <= FALLBACK_CHUNK_CHARS + 500 for c in chunks)
    # Overlap: the end of one chunk reappears at the start of the next.
    assert chunks[0].text.split("\n")[-1] in chunks[1].text
    assert chunks[-1].page_end == 2


def test_attachment_without_structure_uses_file_label() -> None:
    chunks = chunk_pages(pages("Fiyat listesi metni"), file_label="Ek: Fiyat.pdf")
    assert [c.clause_ref for c in chunks] == ["Ek: Fiyat.pdf"]


def test_empty_document() -> None:
    assert chunk_pages(pages("", "  ")) == []
