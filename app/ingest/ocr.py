"""Optional OCR through Azure AI Document Intelligence (prebuilt-read).

Disabled unless OCR_ENABLED=true. Uses the REST API directly (no extra SDK).
"""

from __future__ import annotations

import logging
import time

import httpx

from app.config import Settings
from app.errors import AppError
from app.ingest.normalize import clean_text
from app.ingest.pdf_extract import PageText
from app.net import build_ssl_context

logger = logging.getLogger(__name__)


class OcrError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__("ocr_failed", message, 502)


class DocumentIntelligenceOcr:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if not (settings.DOC_INTELLIGENCE_ENDPOINT and settings.DOC_INTELLIGENCE_KEY.get_secret_value()):
            raise OcrError("OCR yapılandırılmamış (DOC_INTELLIGENCE_ENDPOINT / DOC_INTELLIGENCE_KEY).")
        self.settings = settings
        self._client = client or httpx.Client(verify=build_ssl_context(settings), timeout=60, trust_env=True)

    def _headers(self) -> dict[str, str]:
        return {"Ocp-Apim-Subscription-Key": self.settings.DOC_INTELLIGENCE_KEY.get_secret_value()}

    def ocr_pdf(self, data: bytes, poll_seconds: float = 2.0, max_wait_seconds: float = 300) -> list[PageText]:
        endpoint = self.settings.DOC_INTELLIGENCE_ENDPOINT.rstrip("/")
        url = (
            f"{endpoint}/documentintelligence/documentModels/prebuilt-read:analyze"
            f"?api-version={self.settings.DOC_INTELLIGENCE_API_VERSION}"
        )
        try:
            response = self._client.post(
                url, content=data, headers={**self._headers(), "Content-Type": "application/pdf"}
            )
        except httpx.HTTPError as exc:
            raise OcrError("OCR servisine bağlanılamadı.") from exc
        if response.status_code != 202:
            logger.warning("ocr_submit_failed", extra={"status": response.status_code})
            raise OcrError(f"OCR isteği reddedildi (HTTP {response.status_code}).")
        operation_url = response.headers.get("Operation-Location", "")
        deadline = time.monotonic() + max_wait_seconds
        while True:
            if time.monotonic() > deadline:
                raise OcrError("OCR işlemi zaman aşımına uğradı.")
            try:
                poll = self._client.get(operation_url, headers=self._headers())
            except httpx.HTTPError as exc:
                raise OcrError("OCR sonucu alınamadı.") from exc
            body = poll.json() if poll.status_code == 200 else {}
            status = body.get("status")
            if status == "succeeded":
                return self._pages(body)
            if status == "failed" or poll.status_code >= 400:
                raise OcrError("OCR işlemi başarısız oldu.")
            time.sleep(float(poll.headers.get("Retry-After", poll_seconds)))

    @staticmethod
    def _pages(body: dict) -> list[PageText]:
        pages = body.get("analyzeResult", {}).get("pages", [])
        result = []
        for page in pages:
            lines = [line.get("content", "") for line in page.get("lines", [])]
            result.append(
                PageText(page_no=int(page.get("pageNumber", len(result) + 1)), text=clean_text("\n".join(lines)))
            )
        return result
