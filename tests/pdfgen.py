"""Generate small contract PDFs for tests (reportlab is a dev-only dependency).

A TrueType font with Turkish glyphs is used when one is found (DejaVu on Linux,
Arial on Windows); otherwise Helvetica with Turkish letters folded to ASCII.
"""

from __future__ import annotations

from pathlib import Path

from app.demo import _font, make_pdf

FONT_NAME, TURKISH_GLYPHS = _font()
_ASCII = str.maketrans("şŞğĞıİüÜöÖçÇ", "sSgGiIuUoOcC")


def tr(text: str) -> str:
    """Text as it will appear in the generated PDF (ASCII-folded without a TR font)."""
    return text if TURKISH_GLYPHS else text.translate(_ASCII)


BAYILIK_PAGES = [
    "BAYİLİK SÖZLEŞMESİ\n"
    "İşbu sözleşme Doğuş Otomotiv ile ABC Otomotiv A.Ş. arasında imzalanmıştır.\n"
    "\n"
    "MADDE 1 - TARAFLAR\n"
    "Doğuş Otomotiv Servis ve Ticaret A.Ş. (Distribütör) ile ABC Otomotiv A.Ş. (Bayi).\n"
    "\n"
    "MADDE 2 - KONU\n"
    "Bu sözleşmenin konusu yeni araç satışı ve satış sonrası hizmetlerdir.",
    "MADDE 3 - FESİH\n"
    "3.1 Taraflardan her biri 90 gün önceden yazılı bildirimde bulunarak sözleşmeyi feshedebilir.\n"
    "3.2 Haklı nedenle fesih halinde bildirim süresi aranmaz.\n"
    "\n"
    "MADDE 4 - GİZLİLİK\n"
    "Bayi, ticari sırları sözleşme bitiminden sonra 5 yıl süreyle gizli tutar.\n"
    "Madde 3'te belirtilen fesih halinde de gizlilik yükümlülüğü devam eder.",
]

EK_PAGES = ["EK-1 FİYAT LİSTESİ\nAraç fiyatları KDV dahil olarak belirlenmiştir.\nÖdeme 30 gün vadelidir."]


def write_sample_contracts(root: Path) -> None:
    """A single-file contract and a folder contract with an attachment + metadata."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "ABC_Bayilik.pdf").write_bytes(make_pdf(BAYILIK_PAGES))
    folder = root / "XYZ_Servis"
    folder.mkdir(exist_ok=True)
    (folder / "Ana_Sozlesme.pdf").write_bytes(
        make_pdf(
            [
                "YETKİLİ SERVİS SÖZLEŞMESİ\n\nMADDE 1 - KONU\nServis hizmetleri.\n\n"
                "MADDE 2 - SÜRE\nSözleşme 3 yıl sürelidir.\n\nMADDE 3 - CEZAİ ŞART\n"
                "Aykırılık halinde 100.000 TL cezai şart ödenir."
            ]
        )
    )
    (folder / "Ek-1_Fiyat_Listesi.pdf").write_bytes(make_pdf(EK_PAGES))
    (folder / "metadata.json").write_text(
        '{"contract_no": "SZL-2024-007", "title": "Yetkili Servis Sözleşmesi",'
        ' "parties": ["Doğuş Otomotiv", "XYZ Servis Ltd."], "contract_type": "Yetkili Servis Sözleşmesi",'
        ' "start_date": "2024-01-01", "end_date": "2027-12-31", "status": "Aktif"}',
        encoding="utf-8",
    )
