"""Fake Turkuaz: serves the PDFs in LOCAL_CONTRACTS_DIR as Turkuaz-shaped JSON.

Records are built in the (assumed) Turkuaz format and then go through the same
`turkuaz_mapping` functions as the real source, so the mapping is exercised in
development and tests. Missing metadata is filled with deterministic fake values.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.sources.base import ContractFile, ContractMeta, ContractSource
from app.sources.local_source import LocalSource
from app.sources.turkuaz_mapping import map_contract, map_file

OWN_COMPANY = "Doğuş Otomotiv Servis ve Ticaret A.Ş."
_TYPES = [
    "Bayilik Sözleşmesi",
    "Yetkili Servis Sözleşmesi",
    "Hizmet Sözleşmesi",
    "Kira Sözleşmesi",
    "Tedarik Sözleşmesi",
]
_STATUSES = ["Aktif", "Aktif", "Aktif", "Sona Erdi"]


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


def build_raw_contract(meta: ContractMeta) -> dict[str, Any]:
    seed = _seed(meta.contract_id)
    start = meta.start_date or date(2020 + seed % 5, 1 + seed % 12, 1)
    end = meta.end_date or date(start.year + 3, start.month, 1)
    counterparty = meta.parties[-1] if meta.parties else f"{(meta.title or meta.contract_id).split(' ')[0]} A.Ş."
    parties = meta.parties or [OWN_COMPANY, counterparty]
    return {
        "id": f"TRK-{meta.contract_id}",
        "contractNo": meta.contract_no or f"SZL-{start.year}-{seed % 1000:03d}",
        "name": meta.title,
        "parties": [{"name": name, "role": "Taraf"} for name in parties],
        "type": meta.contract_type or _TYPES[seed % len(_TYPES)],
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "status": meta.status or _STATUSES[seed % len(_STATUSES)],
        "lastModified": (meta.updated_at or datetime(2026, 1, 1)).isoformat(),
        "department": "Hukuk",
    }


def build_raw_file(file: ContractFile) -> dict[str, Any]:
    return {
        "documentId": file.file_id,
        "fileName": file.file_name,
        "isAttachment": file.is_attachment,
        "contentType": file.mime_type,
        "contentBase64": base64.b64encode(file.read_bytes()).decode("ascii"),
    }


class MockSource(ContractSource):
    name = "mock"

    def __init__(self, root: Path) -> None:
        self._local = LocalSource(root)

    def list_contracts(self, updated_since: datetime | None = None) -> list[ContractMeta]:
        return [map_contract(build_raw_contract(meta)) for meta in self._local.list_contracts(updated_since)]

    def get_contract_document(self, contract_id: str) -> list[ContractFile]:
        local_id = contract_id.removeprefix("TRK-")
        return [map_file(build_raw_file(f)) for f in self._local.get_contract_document(local_id)]
