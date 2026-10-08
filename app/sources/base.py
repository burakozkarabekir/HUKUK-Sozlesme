"""Contract source interface and the data models every source returns."""

from __future__ import annotations

import base64
import binascii
import re
from abc import ABC, abstractmethod
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.errors import AppError
from app.ingest.normalize import fold


class SourceError(AppError):
    """Source failure with a Turkish, user-facing message."""

    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        super().__init__(code, message, status_code)


_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y")


def parse_date(value: Any) -> date | None:
    """Accept date/datetime objects, ISO strings and common Turkish formats."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Tarih biçimi anlaşılamadı: {text!r}")


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = parse_date(text)
            if parsed is None:
                return None
            result = datetime(parsed.year, parsed.month, parsed.day)
    return result if result.tzinfo else result.replace(tzinfo=UTC)


class ContractMeta(BaseModel):
    contract_id: str
    contract_no: str | None = None
    title: str | None = None
    parties: list[str] = Field(default_factory=list)
    contract_type: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: str | None = None
    updated_at: datetime | None = None
    # Fields the source returns that we do not map (kept for display/debugging).
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("contract_id", mode="before")
    @classmethod
    def _id_to_str(cls, value: Any) -> str:
        if value in (None, ""):
            raise ValueError("contract_id boş olamaz")
        return str(value)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _parse_date(cls, value: Any) -> date | None:
        return parse_date(value)

    @field_validator("updated_at", mode="before")
    @classmethod
    def _parse_dt(cls, value: Any) -> datetime | None:
        return parse_datetime(value)

    @field_validator("parties", mode="before")
    @classmethod
    def _parties(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [p.strip() for p in re.split(r"[;\n]", value) if p.strip()]
        return [str(p).strip() for p in value if p not in (None, "") and str(p).strip()]


class ContractFile(BaseModel):
    file_id: str
    file_name: str
    is_attachment: bool = False
    content_bytes: bytes | None = None
    content_base64: str | None = None
    mime_type: str = "application/pdf"
    # VARSAYIM: Turkuaz may return a download link instead of inline content.
    download_url: str | None = None

    @field_validator("file_id", mode="before")
    @classmethod
    def _id_to_str(cls, value: Any) -> str:
        if value in (None, ""):
            raise ValueError("file_id boş olamaz")
        return str(value)

    def read_bytes(self) -> bytes:
        if self.content_bytes is not None:
            return self.content_bytes
        if self.content_base64:
            try:
                return base64.b64decode(self.content_base64, validate=False)
            except (binascii.Error, ValueError) as exc:
                raise SourceError("invalid_file_content", f"Dosya içeriği çözülemedi: {self.file_name}") from exc
        raise SourceError("missing_file_content", f"Dosya içeriği alınamadı: {self.file_name}")


class ContractSource(ABC):
    name: str = "base"

    @abstractmethod
    def list_contracts(self, updated_since: datetime | None = None) -> list[ContractMeta]:
        """Contracts created/changed after `updated_since` (all when None)."""

    @abstractmethod
    def get_contract_document(self, contract_id: str) -> list[ContractFile]:
        """Main PDF + attachments of one contract, with content."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", fold(text)).strip("-")
    return slug or "dosya"
