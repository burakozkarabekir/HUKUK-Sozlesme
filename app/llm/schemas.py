"""Structured-output specs: a strict JSON schema plus the Pydantic model that validates it.

The schema is written by hand (not generated) because strict mode requires every
property to be listed in `required` and `additionalProperties: false`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict


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
