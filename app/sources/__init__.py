"""Contract sources (local folder / mock Turkuaz / real Turkuaz)."""

from __future__ import annotations

from app.config import Settings
from app.sources.base import ContractSource


def get_source(settings: Settings) -> ContractSource:
    if settings.CONTRACT_SOURCE == "local":
        from app.sources.local_source import LocalSource

        return LocalSource(settings.LOCAL_CONTRACTS_DIR)
    if settings.CONTRACT_SOURCE == "mock":
        from app.sources.mock_source import MockSource

        return MockSource(settings.LOCAL_CONTRACTS_DIR)
    from app.sources.turkuaz_source import TurkuazSource

    return TurkuazSource(settings)
