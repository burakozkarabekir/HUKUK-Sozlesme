"""Contracts from a local folder (development).

Layout (both forms may be mixed):

    sample_contracts/
      ABC_Bayilik_Sozlesmesi.pdf          -> one contract, one file
      ABC_Bayilik_Sozlesmesi.json         -> optional metadata (same stem)
      XYZ_Servis/                         -> one contract, several files
        Ana_Sozlesme.pdf
        Ek-1_Fiyat_Listesi.pdf            -> attachment (name starts with "Ek")
        metadata.json                     -> optional metadata

Metadata JSON keys: contract_no, title, parties (list), contract_type,
start_date, end_date, status.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.ingest.normalize import fold
from app.sources.base import ContractFile, ContractMeta, ContractSource, SourceError, slugify

logger = logging.getLogger(__name__)

# VARSAYIM: Attachment files are recognised by their name ("Ek-1 ...", "EK2", "Annex").
_ATTACHMENT_RE = re.compile(r"^(ek\b|ek[\s_\-]*\d|ek[\s_\-]*[ivx]+\b|annex|attachment|appendix)")
_META_KEYS = {"contract_no", "title", "parties", "contract_type", "start_date", "end_date", "status"}


def is_attachment_name(file_name: str) -> bool:
    return bool(_ATTACHMENT_RE.match(fold(Path(file_name).stem).strip()))


def _humanize(stem: str) -> str:
    return re.sub(r"[_\-]+", " ", stem).strip()


class LocalSource(ContractSource):
    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = root
        self._index: dict[str, list[Path]] = {}

    def _read_metadata(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("local_metadata_invalid", extra={"path": str(path), "error": str(exc)})
            return {}
        return data if isinstance(data, dict) else {}

    def _scan(self) -> list[tuple[str, list[Path], Path, Path]]:
        """(contract_id, pdf files, metadata path, base path for naming)."""
        if not self.root.is_dir():
            raise SourceError("local_dir_missing", f"Sözleşme klasörü bulunamadı: {self.root}")
        entries: list[tuple[str, list[Path], Path, Path]] = []
        for entry in sorted(self.root.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix.lower() == ".pdf":
                entries.append((f"local-{slugify(entry.stem)}", [entry], entry.with_suffix(".json"), entry))
            elif entry.is_dir():
                pdfs = sorted(p for p in entry.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
                if pdfs:
                    entries.append((f"local-{slugify(entry.name)}", pdfs, entry / "metadata.json", entry))
        return entries

    def list_contracts(self, updated_since: datetime | None = None) -> list[ContractMeta]:
        result: list[ContractMeta] = []
        self._index = {}
        for contract_id, pdfs, meta_path, base in self._scan():
            self._index[contract_id] = pdfs
            mtimes = [p.stat().st_mtime for p in pdfs]
            if meta_path.is_file():
                mtimes.append(meta_path.stat().st_mtime)
            updated_at = datetime.fromtimestamp(max(mtimes), UTC)
            if updated_since and updated_at <= updated_since:
                continue
            raw = self._read_metadata(meta_path)
            stem = base.stem if base.is_file() else base.name
            fields = {k: v for k, v in raw.items() if k in _META_KEYS}
            fields.setdefault("contract_no", stem)
            fields.setdefault("title", _humanize(stem))
            try:
                meta = ContractMeta(
                    contract_id=contract_id,
                    updated_at=updated_at,
                    extra={k: v for k, v in raw.items() if k not in _META_KEYS},
                    **fields,
                )
            except ValueError as exc:
                logger.warning("local_metadata_rejected", extra={"contract_id": contract_id, "error": str(exc)})
                meta = ContractMeta(
                    contract_id=contract_id, contract_no=stem, title=_humanize(stem), updated_at=updated_at
                )
            result.append(meta)
        return result

    def get_contract_document(self, contract_id: str) -> list[ContractFile]:
        if contract_id not in self._index:
            self.list_contracts()
        pdfs = self._index.get(contract_id)
        if not pdfs:
            raise SourceError("contract_not_found", f"Sözleşme kaynakta bulunamadı: {contract_id}", 404)
        files = []
        for path in pdfs:
            # A single-file contract is always the main document.
            attachment = len(pdfs) > 1 and is_attachment_name(path.name)
            files.append(
                ContractFile(
                    file_id=slugify(path.stem),
                    file_name=path.name,
                    is_attachment=attachment,
                    content_bytes=path.read_bytes(),
                )
            )
        return files
