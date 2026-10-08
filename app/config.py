"""Application configuration loaded from environment variables and `.env`.

All settings live here. Secrets use `SecretStr` so they never leak through
`repr()` or accidental logging.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    """Raised when the configuration is unsafe or inconsistent."""


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- Application ---
    APP_NAME: str = "Sözleşme Mevzuat Etki Analizi"
    APP_ENV: Literal["dev", "test", "prod"] = "dev"
    ROOT_PATH: str = "/sozlesme"
    HOST: str = "127.0.0.1"
    PORT: int = 8001
    LOG_LEVEL: str = "INFO"
    DATA_DIR: Path = Path("./data")
    # VARSAYIM: Log directory is configurable; spec only says `logs/app.log`.
    LOG_DIR: Path = Path("./logs")

    # --- Azure AI Foundry ---
    AZURE_OPENAI_BASE_URL: str = ""
    AZURE_OPENAI_API_KEY: SecretStr = SecretStr("")
    CHAT_MODEL: str = "gpt-5.6-sol"
    EMBEDDING_MODEL: str = ""
    LLM_TIMEOUT_SECONDS: float = 120
    LLM_MAX_RETRIES: int = 3
    LLM_MAX_CONCURRENCY: int = Field(default=4, gt=0)
    # VARSAYIM: Extra LLM knobs (not in spec). `auto` modes probe the endpoint and
    # fall back; `cli.py check-llm` prints the recommended fixed values.
    LLM_STRUCTURED_MODE: Literal["auto", "json_schema", "prompt"] = "auto"
    LLM_STREAMING: Literal["auto", "on", "off"] = "auto"
    # 0 = do not send max_output_tokens (reasoning models count thinking tokens too).
    LLM_MAX_OUTPUT_TOKENS: int = Field(default=0, ge=0)
    # Empty = do not send; otherwise minimal | low | medium | high (reasoning models only).
    LLM_REASONING_EFFORT: str = ""

    # --- Chat ---
    # VARSAYIM: Chat limits (spec: "send last N messages", "limit total prompt size").
    CHAT_HISTORY_MESSAGES: int = Field(default=10, ge=0)
    CHAT_MAX_QUESTION_CHARS: int = Field(default=4000, gt=0)
    CHAT_MAX_PROMPT_CHARS: int = Field(default=60_000, gt=0)

    # --- Contract source ---
    CONTRACT_SOURCE: Literal["local", "turkuaz", "mock"] = "local"
    LOCAL_CONTRACTS_DIR: Path = Path("./sample_contracts")
    TURKUAZ_BASE_URL: str = ""
    TURKUAZ_AUTH_MODE: Literal["api_key", "bearer", "oauth_client_credentials", "basic"] = "api_key"
    TURKUAZ_API_KEY: SecretStr = SecretStr("")
    TURKUAZ_API_KEY_HEADER: str = "x-api-key"
    TURKUAZ_BEARER_TOKEN: SecretStr = SecretStr("")
    TURKUAZ_TOKEN_URL: str = ""
    TURKUAZ_CLIENT_ID: str = ""
    TURKUAZ_CLIENT_SECRET: SecretStr = SecretStr("")
    TURKUAZ_SCOPE: str = ""
    TURKUAZ_USERNAME: str = ""
    TURKUAZ_PASSWORD: SecretStr = SecretStr("")
    TURKUAZ_TIMEOUT_SECONDS: float = 60
    TURKUAZ_VERIFY_SSL: bool = True

    # --- OCR (optional) ---
    OCR_ENABLED: bool = False
    DOC_INTELLIGENCE_ENDPOINT: str = ""
    DOC_INTELLIGENCE_KEY: SecretStr = SecretStr("")

    # --- Authentication ---
    AUTH_MODE: Literal["none", "basic", "entra"] = "none"
    BASIC_AUTH_USERS: SecretStr = SecretStr("")
    # VARSAYIM: Admins in basic mode are listed by username (spec does not define it).
    BASIC_AUTH_ADMINS: str = ""
    ENTRA_TENANT_ID: str = ""
    ENTRA_CLIENT_ID: str = ""
    ENTRA_CLIENT_SECRET: SecretStr = SecretStr("")
    ENTRA_REDIRECT_URI: str = ""
    ALLOWED_GROUP_IDS: str = ""
    # VARSAYIM: Spec says "admin role is a separate group id"; variable name is ours.
    ADMIN_GROUP_IDS: str = ""
    # VARSAYIM: Needed to sign session cookies (basic/entra). Empty -> random per process.
    SESSION_SECRET_KEY: SecretStr = SecretStr("")

    # --- Limits ---
    # VARSAYIM: Default limits; spec asks for "reasonable" values.
    MAX_LEGISLATION_CHARS: int = Field(default=200_000, gt=0)
    MAX_UPLOAD_MB: int = Field(default=25, gt=0)
    RATE_LIMIT_PER_MINUTE: int = Field(default=60, gt=0)

    # --- Network ---
    HTTPS_PROXY: str = ""
    HTTP_PROXY: str = ""
    NO_PROXY: str = "localhost,127.0.0.1"
    REQUESTS_CA_BUNDLE: str = ""
    SSL_CERT_FILE: str = ""

    @field_validator("ROOT_PATH")
    @classmethod
    def _normalize_root_path(cls, value: str) -> str:
        value = value.strip()
        if not value or value == "/":
            return ""
        return "/" + value.strip("/")

    @field_validator("LOG_LEVEL")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return value

    @field_validator("DATA_DIR", "LOG_DIR", "LOCAL_CONTRACTS_DIR")
    @classmethod
    def _resolve_dir(cls, value: Path) -> Path:
        # Relative paths are resolved against the project root, not the CWD,
        # so `cli.py` and systemd behave the same.
        return value if value.is_absolute() else (BASE_DIR / value).resolve()

    @model_validator(mode="after")
    def _check_safety(self) -> Settings:
        if self.APP_ENV == "prod" and self.AUTH_MODE == "none":
            raise ConfigError(
                "AUTH_MODE=none üretim ortamında (APP_ENV=prod) kullanılamaz. "
                "AUTH_MODE=basic veya AUTH_MODE=entra ayarlayın."
            )
        return self

    # --- Derived helpers ---
    @property
    def db_path(self) -> Path:
        return self.DATA_DIR / "app.db"

    @property
    def llm_configured(self) -> bool:
        return bool(self.AZURE_OPENAI_BASE_URL.strip() and self.AZURE_OPENAI_API_KEY.get_secret_value())

    @property
    def ca_bundle_path(self) -> str:
        """Extra (corporate) CA file, if configured. Settings win over the environment."""
        return (
            self.REQUESTS_CA_BUNDLE
            or self.SSL_CERT_FILE
            or os.environ.get("REQUESTS_CA_BUNDLE", "")
            or os.environ.get("SSL_CERT_FILE", "")
        )

    @property
    def vector_search_enabled(self) -> bool:
        return bool(self.EMBEDDING_MODEL.strip())

    @property
    def allowed_group_ids(self) -> list[str]:
        return _split_csv(self.ALLOWED_GROUP_IDS)

    @property
    def admin_group_ids(self) -> list[str]:
        return _split_csv(self.ADMIN_GROUP_IDS)

    @property
    def basic_auth_admins(self) -> list[str]:
        return _split_csv(self.BASIC_AUTH_ADMINS)

    def secret_values(self) -> list[str]:
        """All non-empty secret values, used by the log masking filter."""
        values = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr) and value.get_secret_value():
                values.append(value.get_secret_value())
        return values


def apply_network_env(settings: Settings) -> None:
    """Export proxy / CA settings so httpx, openai and ssl pick them up.

    When values come only from `.env` (local development) they are not in
    `os.environ`; HTTP libraries read the process environment. Values already
    present in the environment (e.g. from systemd) win.
    """
    mapping = {
        "HTTPS_PROXY": settings.HTTPS_PROXY,
        "HTTP_PROXY": settings.HTTP_PROXY,
        "NO_PROXY": settings.NO_PROXY,
        "REQUESTS_CA_BUNDLE": settings.REQUESTS_CA_BUNDLE,
        "SSL_CERT_FILE": settings.SSL_CERT_FILE,
    }
    for key, value in mapping.items():
        if value and not os.environ.get(key):
            os.environ[key] = value


@lru_cache
def get_settings() -> Settings:
    return Settings()
