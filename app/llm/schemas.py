"""Structured-output specs: a strict JSON schema plus the Pydantic model that validates it.

The schema is written by hand (not generated) because strict mode requires every
property to be listed in `required` and `additionalProperties: false`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator


@dataclass(frozen=True)
class StructuredSpec:
    name: str
    schema: dict[str, Any]
    model: type[BaseModel]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Probe used by `cli.py check-llm` ---
class CityProbe(_Strict):
    city: str
    plate_code: int


CITY_PROBE = StructuredSpec(
    name="city_probe",
    schema={
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "Şehir adı"},
            "plate_code": {"type": "integer", "description": "Plaka kodu"},
        },
        "required": ["city", "plate_code"],
        "additionalProperties": False,
    },
    model=CityProbe,
)


# --- Impact analysis: concept extraction ---
class LegislationConcepts(_Strict):
    summary: str
    topics: list[str]
    keywords: list[str]
    contract_types: list[str]
    effective_date: str | None


_STR_LIST = {"type": "array", "items": {"type": "string"}}

LEGISLATION_CONCEPTS = StructuredSpec(
    name="legislation_concepts",
    schema={
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Değişikliğin 3-5 cümlelik özeti"},
            "topics": {**_STR_LIST, "description": "Etkilenen konular"},
            "keywords": {**_STR_LIST, "description": "Sözleşmelerde aranacak Türkçe anahtar kelimeler ve varyantları"},
            "contract_types": {**_STR_LIST, "description": "Etkilenmesi muhtemel sözleşme tipleri"},
            "effective_date": {"type": ["string", "null"], "description": "Yürürlük tarihi (metinde varsa)"},
        },
        "required": ["summary", "topics", "keywords", "contract_types", "effective_date"],
        "additionalProperties": False,
    },
    model=LegislationConcepts,
)


# --- Impact analysis: per-contract evaluation ---
IMPACT_LEVELS = ("ETKILENIR", "MUHTEMELEN_ETKILENIR", "ETKILENMEZ")


class AffectedClause(_Strict):
    source: int | None
    clause_ref: str
    page: int | None
    quote: str
    reason: str


class ContractImpact(_Strict):
    contract_id: str
    impact_level: Literal["ETKILENIR", "MUHTEMELEN_ETKILENIR", "ETKILENMEZ"]
    confidence: float
    affected_clauses: list[AffectedClause]
    recommended_action: str
    summary: str

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))


CONTRACT_IMPACT = StructuredSpec(
    name="contract_impact",
    schema={
        "type": "object",
        "properties": {
            "contract_id": {"type": "string"},
            "impact_level": {"type": "string", "enum": list(IMPACT_LEVELS)},
            "confidence": {"type": "number", "description": "0 ile 1 arası güven skoru"},
            "affected_clauses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": ["integer", "null"], "description": "Kaynak maddenin numarası"},
                        "clause_ref": {"type": "string", "description": "Ör. Madde 7.2"},
                        "page": {"type": ["integer", "null"]},
                        "quote": {"type": "string", "description": "Maddeden birebir kısa alıntı"},
                        "reason": {"type": "string", "description": "Neden etkilendiği"},
                    },
                    "required": ["source", "clause_ref", "page", "quote", "reason"],
                    "additionalProperties": False,
                },
            },
            "recommended_action": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": [
            "contract_id",
            "impact_level",
            "confidence",
            "affected_clauses",
            "recommended_action",
            "summary",
        ],
        "additionalProperties": False,
    },
    model=ContractImpact,
)
