"""Demo contract PDFs for local development (`python cli.py demo-data`).

Requires reportlab (a dev dependency: pip install -r requirements-dev.txt).
All companies, numbers and texts are fictitious.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arial.ttf",
]
_ASCII = str.maketrans("şŞğĞıİüÜöÖçÇ", "sSgGiIuUoOcC")


def _font() -> tuple[str, bool]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            if "DemoSans" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("DemoSans", path))
            return "DemoSans", True
    return "Helvetica", False


def make_pdf(pages: list[str]) -> bytes:
    """One string per page, lines separated by '\\n'. An empty page has no text (looks scanned)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    font, turkish = _font()
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    _, height = A4
    for page in pages:
        y = height - 60
        pdf.setFont(font, 10)
        if not page:
            pdf.rect(50, 50, 400, 600)
        for line in page.split("\n"):
            pdf.drawString(50, y, line if turkish else line.translate(_ASCII))
            y -= 14
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


DOGUS = "Doğuş Otomotiv Servis ve Ticaret A.Ş."

DEMO_CONTRACTS: list[dict] = [
    {
        "folder": "SZL-2023-014_Anadolu_Bayilik",
        "meta": {
            "contract_no": "SZL-2023-014",
            "title": "Yetkili Bayilik Sözleşmesi",
            "parties": [DOGUS, "Anadolu Motorlu Araçlar Ltd. Şti."],
            "contract_type": "Bayilik Sözleşmesi",
            "start_date": "2023-03-01",
            "end_date": "2028-02-29",
            "status": "Aktif",
        },
        "files": {
            "Bayilik_Sozlesmesi.pdf": [
                "YETKİLİ BAYİLİK SÖZLEŞMESİ\n\nMADDE 1 - TARAFLAR\n"
                f"{DOGUS} (Distribütör) ile Anadolu Motorlu Araçlar Ltd. Şti. (Bayi).\n\n"
                "MADDE 2 - KONU\nBayi, Distribütörün belirlediği bölgede yeni araç satışı ve\n"
                "satış sonrası hizmetleri yürütür.\n\n"
                "MADDE 3 - SÜRE\nSözleşme 5 (beş) yıl süreyle geçerlidir.\n\n"
                "MADDE 4 - FİYATLANDIRMA\n4.1 Tavsiye edilen satış fiyatları EK-1'de yer alır.\n"
                "4.2 Bayi, yeniden satış fiyatını serbestçe belirler.",
                "MADDE 5 - FESİH\n5.1 Taraflardan her biri 90 gün önceden yazılı bildirimde bulunarak\n"
                "sözleşmeyi herhangi bir sebep göstermeksizin feshedebilir.\n"
                "5.2 Haklı nedenle fesih halinde bildirim süresi aranmaz.\n\n"
                "MADDE 6 - REKABET YASAĞI\nBayi, sözleşme süresince ve sona ermesinden itibaren 2 yıl\n"
                "süreyle rakip markaların araçlarını satamaz.\n\n"
                "MADDE 7 - KİŞİSEL VERİLER\nBayi, müşterilere ait kişisel verileri 6698 sayılı Kanun'a\n"
                "uygun olarak işler ve 10 yıl süreyle saklar.\n\n"
                "MADDE 8 - UYUŞMAZLIK\nİstanbul (Çağlayan) Mahkemeleri ve İcra Daireleri yetkilidir.",
            ],
            "Ek-1_Fiyat_Listesi.pdf": [
                "EK-1 TAVSİYE EDİLEN FİYAT LİSTESİ\nFiyatlar KDV dahildir.\nÖdeme vadesi 30 gündür."
            ],
        },
    },
    {
        "folder": "SZL-2024-031_Marmara_Servis",
        "meta": {
            "contract_no": "SZL-2024-031",
            "title": "Yetkili Servis Sözleşmesi",
            "parties": [DOGUS, "Marmara Oto Servis A.Ş."],
            "contract_type": "Yetkili Servis Sözleşmesi",
            "start_date": "2024-06-01",
            "end_date": "2027-05-31",
            "status": "Aktif",
        },
        "files": {
            "Servis_Sozlesmesi.pdf": [
                "YETKİLİ SERVİS SÖZLEŞMESİ\n\nMadde 1 – Konu\nServis, garanti kapsamındaki bakım ve\n"
                "onarım hizmetlerini yürütür.\n\nMadde 2 – Garanti İşlemleri\n"
                "Garanti kapsamındaki parçalar 2 yıl süreyle garantilidir.\n\n"
                "Madde 3 – Cezai Şart\nServisin yükümlülüklerine aykırı davranması halinde\n"
                "her bir aykırılık için 250.000 TL cezai şart ödenir.\n\n"
                "Madde 4 – Sona Erme\nSözleşme, taraflardan birinin 60 gün önceden yapacağı\n"
                "yazılı bildirimle sona erer.\n\nMadde 5 – Gizlilik\n"
                "Taraflar, ticari sırları sözleşmenin sona ermesinden sonra 5 yıl gizli tutar."
            ]
        },
    },
    {
        "folder": "SZL-2022-008_Kartal_Kira",
        "meta": {
            "contract_no": "SZL-2022-008",
            "title": "Showroom Kira Sözleşmesi",
            "parties": [DOGUS, "Kartal Gayrimenkul Yatırım A.Ş."],
            "contract_type": "Kira Sözleşmesi",
            "start_date": "2022-01-01",
            "end_date": "2026-12-31",
            "status": "Aktif",
        },
        "files": {
            "Kira_Sozlesmesi.pdf": [
                "İŞYERİ KİRA SÖZLEŞMESİ\n\n1. TARAFLAR\nKiraya veren: Kartal Gayrimenkul Yatırım A.Ş.\n"
                f"Kiracı: {DOGUS}\n\n2. KİRALANAN\nİstanbul Kartal'daki 2.500 m2 showroom.\n\n"
                "3. KİRA BEDELİ\nAylık kira bedeli 850.000 TL'dir. Kira bedeli her yıl TÜFE oranında\n"
                "artırılır.\n\n4. FESİH\nKiracı, 3 ay önceden bildirmek kaydıyla sözleşmeyi feshedebilir.\n\n"
                "5. TEMİNAT\nKiracı 3 aylık kira bedeli tutarında teminat verir."
            ]
        },
    },
    {
        "folder": "SZL-2025-002_Delta_Tedarik",
        "meta": {
            "contract_no": "SZL-2025-002",
            "title": "Yedek Parça Tedarik Sözleşmesi",
            "parties": [DOGUS, "Delta Otomotiv Parça San. A.Ş."],
            "contract_type": "Tedarik Sözleşmesi",
            "start_date": "2025-01-01",
            "end_date": "2026-12-31",
            "status": "Aktif",
        },
        "files": {
            "Tedarik_Sozlesmesi.pdf": [
                "YEDEK PARÇA TEDARİK SÖZLEŞMESİ\n\nMADDE 1 - KONU\nTedarikçi, sipariş edilen yedek parçaları\n"
                "teslim eder.\n\nMADDE 2 - TESLİMAT\nTeslim süresi sipariş tarihinden itibaren 15 gündür.\n\n"
                "MADDE 3 - ÖDEME\nÖdeme, fatura tarihinden itibaren 90 gün içinde yapılır.\n\n"
                "MADDE 4 - GECİKME CEZASI\nGeciken her gün için sipariş bedelinin %1'i ceza uygulanır.\n\n"
                "MADDE 5 - MÜCBİR SEBEP\nDeprem, salgın ve savaş gibi haller mücbir sebep sayılır."
            ]
        },
    },
    {
        "folder": "SZL-2019-021_Eski_Bayilik",
        "meta": {
            "contract_no": "SZL-2019-021",
            "title": "Bayilik Sözleşmesi (Sona Erdi)",
            "parties": [DOGUS, "Ege Oto Ticaret Ltd. Şti."],
            "contract_type": "Bayilik Sözleşmesi",
            "start_date": "2019-01-01",
            "end_date": "2023-12-31",
            "status": "Sona Erdi",
        },
        "files": {
            "Bayilik.pdf": [
                "BAYİLİK SÖZLEŞMESİ\n\nMADDE 1 - KONU\nAraç satışı.\n\nMADDE 2 - FESİH\n"
                "Taraflar 30 gün önceden bildirimle sözleşmeyi feshedebilir.\n\n"
                "MADDE 3 - REKABET YASAĞI\nBayi, sona ermeden itibaren 1 yıl rakip marka satamaz."
            ]
        },
    },
    {
        "folder": "SZL-2021-099_Taranmis_Protokol",
        "meta": {"contract_no": "SZL-2021-099", "title": "Taranmış Ek Protokol", "status": "Aktif"},
        "files": {"Protokol_Tarama.pdf": ["", ""]},  # image-only: shows up in the OCR list
    },
]

DEMO_LEGISLATION = (
    "7777 sayılı Kanun ile 6102 sayılı Kanun'a eklenen hükümlere göre, bayilik ve yetkili servis\n"
    "sözleşmelerinde süresiz fesih için bildirim süresi en az 180 gün olarak uygulanır. Sözleşme sona\n"
    "erdikten sonra uygulanacak rekabet yasakları 1 yılı aşamaz. Cezai şartlar, sözleşme yıllık\n"
    "cirosunun %5'ini geçemez. Kişisel veriler, işleme amacının gerektirdiği süreden fazla saklanamaz."
)


def write_demo_contracts(root: Path) -> list[Path]:
    """Create the demo folders under `root`. Existing files are overwritten."""
    created = []
    for contract in DEMO_CONTRACTS:
        folder = root / contract["folder"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "metadata.json").write_text(
            json.dumps(contract["meta"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for name, pages in contract["files"].items():
            path = folder / name
            path.write_bytes(make_pdf(pages))
            created.append(path)
    (root / "ornek_mevzuat_degisikligi.txt").write_text(DEMO_LEGISLATION, encoding="utf-8")
    return created
