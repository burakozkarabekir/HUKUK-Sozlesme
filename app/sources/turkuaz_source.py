"""Real Turkuaz contract source (HTTP/JSON).

Format-specific details (paths, pagination, field names) are in
`turkuaz_mapping.py`. This module handles transport concerns: authentication
(api_key / bearer / oauth_client_credentials / basic), pagination, retries
with exponential backoff, timeouts, proxy and corporate CA, and meaningful
Turkish error messages for 401/403/404/429/5xx.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.net import build_ssl_context
from app.sources import turkuaz_mapping as mapping
from app.sources.base import ContractFile, ContractMeta, ContractSource, SourceError

logger = logging.getLogger(__name__)

TOKEN_SKEW_SECONDS = 60
MAX_RETRY_AFTER_SECONDS = 60


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class _Auth:
    def headers(self) -> dict[str, str]:
        return {}

    def invalidate(self) -> bool:
        """Drop cached credentials. True if a retry with fresh credentials makes sense."""
        return False


class _ApiKeyAuth(_Auth):
    def __init__(self, header: str, key: str) -> None:
        self._header, self._key = header, key

    def headers(self) -> dict[str, str]:
        return {self._header: self._key}


class _BearerAuth(_Auth):
    def __init__(self, token: str) -> None:
        self._token = token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}


class _BasicAuth(_Auth):
    def __init__(self, username: str, password: str) -> None:
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        self._header = f"Basic {token}"

    def headers(self) -> dict[str, str]:
        return {"Authorization": self._header}


class _OAuthClientCredentials(_Auth):
    """Client-credentials token, cached until shortly before it expires."""

    def __init__(self, client: httpx.Client, settings: Settings, clock: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires_at - TOKEN_SKEW_SECONDS:
                return self._token
            data = {
                "grant_type": "client_credentials",
                "client_id": self._settings.TURKUAZ_CLIENT_ID,
                "client_secret": self._settings.TURKUAZ_CLIENT_SECRET.get_secret_value(),
            }
            if self._settings.TURKUAZ_SCOPE:
                data["scope"] = self._settings.TURKUAZ_SCOPE
            try:
                response = self._client.post(self._settings.TURKUAZ_TOKEN_URL, data=data)
            except httpx.HTTPError as exc:
                raise SourceError(
                    "turkuaz_token_failed", "Turkuaz OAuth token sunucusuna bağlanılamadı (ağ/proxy/SSL)."
                ) from exc
            if response.status_code != 200:
                raise SourceError(
                    "turkuaz_token_failed",
                    f"Turkuaz OAuth token alınamadı (HTTP {response.status_code}). "
                    "TURKUAZ_CLIENT_ID / TURKUAZ_CLIENT_SECRET / TURKUAZ_SCOPE değerlerini kontrol edin.",
                )
            body = response.json()
            self._token = body.get("access_token")
            if not self._token:
                raise SourceError("turkuaz_token_failed", "Turkuaz OAuth yanıtında access_token yok.")
            self._expires_at = self._clock() + float(body.get("expires_in") or 3600)
            logger.info("turkuaz_token_refreshed", extra={"expires_in": body.get("expires_in")})
            return self._token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def invalidate(self) -> bool:
        with self._lock:
            self._token = None
            self._expires_at = 0.0
        return True


_REQUIRED = {
    "api_key": ["TURKUAZ_API_KEY", "TURKUAZ_API_KEY_HEADER"],
    "bearer": ["TURKUAZ_BEARER_TOKEN"],
    "oauth_client_credentials": ["TURKUAZ_TOKEN_URL", "TURKUAZ_CLIENT_ID", "TURKUAZ_CLIENT_SECRET"],
    "basic": ["TURKUAZ_USERNAME", "TURKUAZ_PASSWORD"],
}


def _setting_value(settings: Settings, name: str) -> str:
    value = getattr(settings, name)
    return value.get_secret_value() if hasattr(value, "get_secret_value") else str(value or "")


def missing_settings(settings: Settings) -> list[str]:
    names = ["TURKUAZ_BASE_URL", *_REQUIRED[settings.TURKUAZ_AUTH_MODE]]
    return [name for name in names if not _setting_value(settings, name).strip()]


def build_auth(settings: Settings, client: httpx.Client) -> _Auth:
    mode = settings.TURKUAZ_AUTH_MODE
    if mode == "api_key":
        return _ApiKeyAuth(settings.TURKUAZ_API_KEY_HEADER, settings.TURKUAZ_API_KEY.get_secret_value())
    if mode == "bearer":
        return _BearerAuth(settings.TURKUAZ_BEARER_TOKEN.get_secret_value())
    if mode == "basic":
        return _BasicAuth(settings.TURKUAZ_USERNAME, settings.TURKUAZ_PASSWORD.get_secret_value())
    return _OAuthClientCredentials(client, settings)


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


class TurkuazSource(ContractSource):
    name = "turkuaz"

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        missing = missing_settings(settings)
        if missing:
            raise SourceError("turkuaz_not_configured", f"Turkuaz bağlantı ayarları eksik: {', '.join(missing)}", 503)
        self.settings = settings
        self._sleep = sleep
        verify: Any = build_ssl_context(settings) if settings.TURKUAZ_VERIFY_SSL else False
        self.client = client or httpx.Client(
            base_url=settings.TURKUAZ_BASE_URL.rstrip("/"),
            timeout=settings.TURKUAZ_TIMEOUT_SECONDS,
            verify=verify,
            trust_env=True,  # HTTPS_PROXY / NO_PROXY
            headers={"Accept": "application/json", "User-Agent": "sozlesme-etki-analizi/1.0"},
            follow_redirects=True,
        )
        self.auth = build_auth(settings, self.client)

    def close(self) -> None:
        self.client.close()

    # --- HTTP with retries ---
    def _backoff(self, attempt: int, response: httpx.Response | None = None) -> None:
        delay = float(min(2**attempt, 30))
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                delay = float(min(int(retry_after), MAX_RETRY_AFTER_SECONDS))
        self._sleep(delay)

    def request(self, method: str, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        retries = self.settings.TURKUAZ_MAX_RETRIES
        auth_retried = False
        attempt = 0
        while True:
            try:
                response = self.client.request(method, url, params=params, headers=self.auth.headers())
            except httpx.TimeoutException as exc:
                if attempt < retries:
                    attempt += 1
                    self._backoff(attempt)
                    continue
                raise SourceError(
                    "turkuaz_timeout",
                    f"Turkuaz {self.settings.TURKUAZ_TIMEOUT_SECONDS:.0f} sn içinde yanıt vermedi (zaman aşımı).",
                    504,
                ) from exc
            except httpx.TransportError as exc:
                if attempt < retries:
                    attempt += 1
                    self._backoff(attempt)
                    continue
                raise SourceError(
                    "turkuaz_unreachable",
                    "Turkuaz'a bağlanılamadı. Ağ erişimi, HTTPS_PROXY ve SSL sertifikası (REQUESTS_CA_BUNDLE / "
                    "TURKUAZ_VERIFY_SSL) ayarlarını kontrol edin.",
                ) from exc

            status = response.status_code
            if status < 400:
                return response
            logger.warning("turkuaz_http_error", extra={"status": status, "path": str(response.request.url.path)})
            if status == 401:
                if not auth_retried and self.auth.invalidate():
                    auth_retried = True
                    continue
                raise SourceError(
                    "turkuaz_unauthorized",
                    "Turkuaz kimlik doğrulaması başarısız (401). TURKUAZ_AUTH_MODE ve anahtar/token ayarlarını "
                    "kontrol edin.",
                )
            if status == 403:
                raise SourceError(
                    "turkuaz_forbidden", "Turkuaz erişim izni vermedi (403). Kullanıcının/uygulamanın yetkisi yok."
                )
            if status == 404:
                raise SourceError(
                    "turkuaz_not_found", f"Turkuaz uç noktası veya kayıt bulunamadı (404): {response.request.url.path}"
                )
            if status == 429 or status >= 500:
                if attempt < retries:
                    attempt += 1
                    self._backoff(attempt, response)
                    continue
                if status == 429:
                    raise SourceError(
                        "turkuaz_rate_limited", "Turkuaz istek sınırına ulaşıldı (429). Daha sonra tekrar deneyin."
                    )
                raise SourceError("turkuaz_server_error", f"Turkuaz sunucu hatası (HTTP {status}).")
            raise SourceError("turkuaz_bad_request", f"Turkuaz isteği reddetti (HTTP {status}).")

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        response = self.request("GET", url, params)
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError("turkuaz_invalid_json", "Turkuaz geçerli bir JSON yanıtı döndürmedi.") from exc

    # --- pagination ---
    @staticmethod
    def _items(body: Any, path: str) -> list[Any]:
        items = mapping.get_path(body, path) if path else body
        if items is None:
            return []
        if not isinstance(items, list):
            raise SourceError(
                "turkuaz_unexpected_format",
                f"Turkuaz yanıtında '{path or '(kök)'}' bir liste değil. turkuaz_mapping.py ayarlarını kontrol edin.",
            )
        return items

    def iter_pages(self, path: str, params: dict[str, Any] | None = None) -> Iterator[tuple[Any, list[Any]]]:
        """Yield (raw body, items) per page according to `mapping.PAGINATION`."""
        cfg = mapping.PAGINATION
        style = cfg["style"]
        params = dict(params or {})
        page_size = int(cfg["page_size"])
        page = int(cfg["start_page"])
        offset = 0
        url: str | None = path
        seen = 0
        for _ in range(int(cfg["max_pages"])):
            query = dict(params)
            if style == "page":
                query.update({cfg["page_param"]: page, cfg["size_param"]: page_size})
            elif style == "offset":
                query.update({cfg["offset_param"]: offset, cfg["limit_param"]: page_size})
            body = self.get_json(url or path, query if style != "link" or url == path else None)
            items = self._items(body, cfg["items_path"])
            yield body, items
            seen += len(items)
            if style == "none" or not items:
                return
            total = mapping.get_path(body, cfg["total_path"]) if cfg.get("total_path") else None
            if isinstance(total, int) and seen >= total:
                return
            if style == "page":
                if len(items) < page_size:
                    return
                page += 1
            elif style == "offset":
                if len(items) < page_size:
                    return
                offset += len(items)
            elif style == "cursor":
                cursor = mapping.get_path(body, cfg["next_cursor_path"])
                if not cursor:
                    return
                params[cfg["cursor_param"]] = cursor
            elif style == "link":
                url = mapping.get_path(body, cfg["next_link_path"])
                if not url:
                    return
        logger.warning("turkuaz_max_pages_reached", extra={"max_pages": cfg["max_pages"]})

    # --- ContractSource ---
    def list_contracts(self, updated_since: datetime | None = None) -> list[ContractMeta]:
        params: dict[str, Any] = {}
        if updated_since and mapping.UPDATED_SINCE_PARAM:
            params[mapping.UPDATED_SINCE_PARAM] = updated_since.astimezone(UTC).strftime(mapping.UPDATED_SINCE_FORMAT)
        result: list[ContractMeta] = []
        for _, items in self.iter_pages(mapping.LIST_PATH, params):
            for raw in items:
                try:
                    meta = mapping.map_contract(raw)
                except ValueError as exc:
                    logger.warning("turkuaz_record_skipped", extra={"error": str(exc)[:300]})
                    continue
                # Client-side guard in case the API ignores the updated-since parameter.
                if updated_since and meta.updated_at and meta.updated_at <= updated_since:
                    continue
                result.append(meta)
        return result

    def get_contract_document(self, contract_id: str) -> list[ContractFile]:
        path = mapping.DOCUMENTS_PATH.format(contract_id=quote(contract_id, safe=""))
        body = self.get_json(path)
        files = []
        for raw in self._items(body, mapping.DOCUMENTS_ITEMS_PATH):
            file = mapping.map_file(raw)
            if file.content_base64 is None and file.content_bytes is None:
                if not file.download_url:
                    raise SourceError(
                        "turkuaz_file_without_content",
                        f"Turkuaz dosyası için içerik veya indirme adresi yok: {file.file_name}",
                    )
                file.content_bytes = self.request("GET", file.download_url).content
            files.append(file)
        return files

    def raw_first_page(self, limit: int = 3) -> tuple[Any, list[Any]]:
        """First page of the list endpoint, unmapped (for `cli.py check-turkuaz`)."""
        for body, items in self.iter_pages(mapping.LIST_PATH):
            return body, items[:limit]
        return None, []
