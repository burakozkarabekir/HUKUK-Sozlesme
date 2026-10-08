"""Turkuaz field mapping - THE place to edit when the real API format is known.

The Turkuaz endpoint format is not final yet (Doğuş Teknoloji). Everything
format-specific lives in the dictionaries below; `turkuaz_source.py` and
`mock_source.py` only use them through `map_contract()` / `map_file()`.

Path syntax:
    "contractNo"          -> raw["contractNo"]
    "meta.contractNo"     -> raw["meta"]["contractNo"]
    "parties[].name"      -> [p["name"] for p in raw["parties"]]
    "parties[]"           -> raw["parties"] (list of strings)

Use `python cli.py check-turkuaz` to print the real field names.
"""

from __future__ import annotations

from typing import Any

from app.sources.base import ContractFile, ContractMeta

# VARSAYIM: All names below are placeholders until the real Turkuaz format arrives.

# --- Endpoints (relative to TURKUAZ_BASE_URL) ---
LIST_PATH = "/contracts"
# {contract_id} is replaced with the contract id.
DOCUMENTS_PATH = "/contracts/{contract_id}/documents"
# Query parameter used for incremental sync, and its format.
UPDATED_SINCE_PARAM = "updatedSince"
UPDATED_SINCE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# --- Pagination ---
# style: "page" (page number), "offset" (offset/limit), "cursor" (next token),
#        "link" (full next URL in the body) or "none" (everything in one response).
PAGINATION: dict[str, Any] = {
    "style": "page",
    "page_param": "page",
    "size_param": "pageSize",
    "page_size": 100,
    "start_page": 1,
    "offset_param": "offset",
    "limit_param": "limit",
    "cursor_param": "cursor",
    "next_cursor_path": "nextCursor",
    "next_link_path": "next",
    # Where the list of records is in the response ("" = the response itself is a list).
    "items_path": "items",
    # Optional; used to stop early when known.
    "total_path": "totalCount",
    "max_pages": 1000,
}

# --- Contract fields: our name -> path in the Turkuaz record ---
CONTRACT_FIELD_MAP: dict[str, str] = {
    "contract_id": "id",
    "contract_no": "contractNo",
    "title": "name",
    "parties": "parties[].name",
    "contract_type": "type",
    "start_date": "startDate",
    "end_date": "endDate",
    "status": "status",
    "updated_at": "lastModified",
}

# --- Document endpoint ---
# Where the list of files is in the documents response ("" = response is a list).
DOCUMENTS_ITEMS_PATH = "documents"
FILE_FIELD_MAP: dict[str, str] = {
    "file_id": "documentId",
    "file_name": "fileName",
    "is_attachment": "isAttachment",
    "content_base64": "contentBase64",
    "download_url": "downloadUrl",
    "mime_type": "contentType",
}
# Values of a "document type" field that mean "attachment", used when there is no
# boolean flag. Set DOCUMENT_TYPE_PATH to "" to disable.
DOCUMENT_TYPE_PATH = "documentType"
ATTACHMENT_TYPE_VALUES = {"ek", "attachment", "annex", "ek dosya"}


def get_path(obj: Any, path: str) -> Any:
    """Resolve a mapping path (see module docstring). Missing -> None."""
    if not path:
        return obj
    current = obj
    parts = path.split(".")
    for index, part in enumerate(parts):
        if current is None:
            return None
        if part.endswith("[]"):
            key = part[:-2]
            items = current.get(key) if isinstance(current, dict) and key else current
            if not isinstance(items, list):
                return None
            rest = ".".join(parts[index + 1 :])
            values = [get_path(item, rest) for item in items] if rest else list(items)
            return [v for v in values if v not in (None, "")]
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _top_level_keys(mapping: dict[str, str]) -> set[str]:
    return {path.split(".")[0].removesuffix("[]") for path in mapping.values() if path}


def map_contract(raw: dict[str, Any]) -> ContractMeta:
    values = {field: get_path(raw, path) for field, path in CONTRACT_FIELD_MAP.items()}
    used = _top_level_keys(CONTRACT_FIELD_MAP)
    extra = {key: value for key, value in raw.items() if key not in used}
    return ContractMeta(**values, extra=extra)


def _to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "evet", "yes", "ek"}


def map_file(raw: dict[str, Any]) -> ContractFile:
    values = {field: get_path(raw, path) for field, path in FILE_FIELD_MAP.items()}
    attachment = _to_bool(values.pop("is_attachment"))
    if attachment is None and DOCUMENT_TYPE_PATH:
        doc_type = get_path(raw, DOCUMENT_TYPE_PATH)
        attachment = str(doc_type or "").strip().lower() in ATTACHMENT_TYPE_VALUES
    values["mime_type"] = values.get("mime_type") or "application/pdf"
    if not values.get("file_name"):
        values["file_name"] = f"{values.get('file_id') or 'dosya'}.pdf"
    return ContractFile(is_attachment=bool(attachment), **values)
