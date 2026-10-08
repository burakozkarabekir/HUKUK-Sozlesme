from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from app.db import connect, migrate
from app.ingest.ocr import DocumentIntelligenceOcr, OcrError
from app.ingest.pdf_extract import PdfExtractError, extract_pdf
from app.ingest.pipeline import run_sync
from app.services.contracts import ContractFilters
from app.services.embeddings import embed_missing, make_sync_embedder, vector_ranked_ids
from tests.conftest import make_settings
from tests.pdfgen import make_pdf, write_sample_contracts

ENDPOINT = "https://di.test"
ANALYZE = f"{ENDPOINT}/documentintelligence/documentModels/prebuilt-read:analyze"
OPERATION = f"{ENDPOINT}/documentintelligence/documentModels/prebuilt-read/analyzeResults/op1"


def ocr_settings(tmp_path: Path):
    return make_settings(
        tmp_path, OCR_ENABLED=True, DOC_INTELLIGENCE_ENDPOINT=ENDPOINT, DOC_INTELLIGENCE_KEY="di-key-123"
    )


def test_ocr_not_configured(tmp_path: Path) -> None:
    with pytest.raises(OcrError):
        DocumentIntelligenceOcr(make_settings(tmp_path))


def test_ocr_success_with_polling(tmp_path: Path) -> None:
    result = {
        "status": "succeeded",
        "analyzeResult": {
            "pages": [
                {"pageNumber": 1, "lines": [{"content": "MADDE 1 - KONU"}, {"content": "Taranmış metin."}]},
                {"pageNumber": 2, "lines": [{"content": "MADDE 2 - SÜRE"}]},
            ]
        },
    }
    polls = iter(
        [
            httpx.Response(200, json={"status": "running"}, headers={"Retry-After": "0"}),
            httpx.Response(200, json=result),
        ]
    )
    with respx.mock() as mock:
        submit = mock.post(ANALYZE).respond(202, headers={"Operation-Location": OPERATION})
        mock.get(OPERATION).mock(side_effect=lambda r: next(polls))
        pages = DocumentIntelligenceOcr(ocr_settings(tmp_path)).ocr_pdf(b"%PDF-1.4", poll_seconds=0)
    assert [(p.page_no, p.text) for p in pages] == [(1, "MADDE 1 - KONU\nTaranmış metin."), (2, "MADDE 2 - SÜRE")]
    request = submit.calls[0].request
    assert request.headers["Ocp-Apim-Subscription-Key"] == "di-key-123"
    assert request.url.params["api-version"] == "2024-11-30"


@pytest.mark.parametrize(
    "setup",
    [
        lambda mock: mock.post(ANALYZE).respond(401),
        lambda mock: (
            mock.post(ANALYZE).respond(202, headers={"Operation-Location": OPERATION}),
            mock.get(OPERATION).respond(200, json={"status": "failed"}),
        ),
        lambda mock: mock.post(ANALYZE).mock(side_effect=httpx.ConnectError("x")),
    ],
)
def test_ocr_failures(tmp_path: Path, setup) -> None:
    with respx.mock() as mock:
        setup(mock)
        with pytest.raises(OcrError):
            DocumentIntelligenceOcr(ocr_settings(tmp_path)).ocr_pdf(b"%PDF", poll_seconds=0)


def test_pdf_extract_rejects_non_pdf_and_broken_pdf() -> None:
    with pytest.raises(PdfExtractError):
        extract_pdf(b"merhaba")
    with pytest.raises(PdfExtractError):
        extract_pdf(b"%PDF-1.4\n bozuk icerik \n%%EOF")


def test_pdf_extract_pages_and_low_text() -> None:
    result = extract_pdf(make_pdf(["MADDE 1 - KONU\n" + "metin " * 20, "", "kısa"]))
    assert result.page_count == 3 and result.low_text_pages == [2, 3] and result.needs_ocr
    assert result.full_text.startswith("MADDE 1")


class FakeEmbeddingsClient:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self.embeddings = self

    def create(self, model: str, input: list[str]):
        self.batches.append(input)
        data = [SimpleNamespace(index=i, embedding=[float(len(t)), 1.0]) for i, t in enumerate(input)]
        return SimpleNamespace(data=list(reversed(data)))  # out of order on purpose


def test_sync_embedder_batches_and_orders(tmp_path: Path, monkeypatch) -> None:
    import app.services.embeddings as module

    monkeypatch.setattr(module, "BATCH_SIZE", 2)
    client = FakeEmbeddingsClient()
    embed = make_sync_embedder(make_settings(tmp_path, EMBEDDING_MODEL="emb"), client=client)
    vectors = embed(["a", "bb", "ccc"])
    assert vectors == [[1.0, 1.0], [2.0, 1.0], [3.0, 1.0]]
    assert client.batches == [["a", "bb"], ["ccc"]]


def test_embed_missing_and_vector_ranking(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts"
    write_sample_contracts(contracts)
    settings = make_settings(tmp_path, LOCAL_CONTRACTS_DIR=contracts, EMBEDDING_MODEL="emb")
    run_sync(settings, embedder=lambda texts: [[0.0, 1.0] for _ in texts])
    conn = connect(settings.db_path)
    migrate(conn)
    total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == total
    conn.execute("DELETE FROM embeddings WHERE chunk_id IN (SELECT id FROM chunks WHERE clause_ref = 'Madde 4')")
    assert embed_missing(conn, settings, lambda texts: [[1.0, 0.0] for _ in texts]) == 1
    target = conn.execute("SELECT id FROM chunks WHERE clause_ref = 'Madde 4'").fetchone()[0]
    assert vector_ranked_ids(conn, [1.0, 0.0], "emb", ContractFilters(), 3)[0] == target
    assert vector_ranked_ids(conn, [1.0, 0.0], "baska-model", ContractFilters(), 3) == []
    assert vector_ranked_ids(conn, [1.0, 0.0, 0.0], "emb", ContractFilters(), 3) == []  # dimension mismatch
