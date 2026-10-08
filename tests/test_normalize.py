from __future__ import annotations

import pytest

from app.ingest.normalize import clean_text, fold, fold_tokens


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("İSTANBUL", "istanbul"),
        ("ISPARTA", "isparta"),
        ("ırmak", "irmak"),
        ("Iğdır", "igdir"),
        ("ŞİRKET ŞARTLARI", "sirket sartlari"),
        ("Gizlilik Yükümlülüğü", "gizlilik yukumlulugu"),
        ("ÖDEME ÇİZELGESİ", "odeme cizelgesi"),
        ("Hâkim", "hakim"),
        ("Fesih – İhbar", "fesih - ihbar"),
    ],
)
def test_fold_turkish(raw: str, expected: str) -> None:
    assert fold(raw) == expected


def test_fold_preserves_length_for_highlighting() -> None:
    text = "İşbu SÖZLEŞME’nin 7.2 maddesi ﬁyat ½ … Iğdır"
    assert len(fold(text)) == len(text)


def test_case_and_accent_insensitive_equality() -> None:
    assert fold("FESİH") == fold("fesih") == fold("Fesih")
    assert fold("sözleşme") == fold("SOZLESME")


def test_clean_text() -> None:
    raw = "i̇stanbul  \t  Ltd. Şti.\r\n\r\n\r\n\r\nso­nu ﬁyat"
    assert clean_text(raw) == "istanbul Ltd. Şti.\n\nsonu fiyat"


def test_fold_tokens() -> None:
    assert fold_tokens("Fesih bildirimi, 90 GÜN!") == ["fesih", "bildirimi", "90", "gun"]
