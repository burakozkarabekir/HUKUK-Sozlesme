"""Uniform JSON error responses: {"error": {"code": "...", "message": "..."}}."""

from __future__ import annotations

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

_STATUS_MESSAGES: dict[int, tuple[str, str]] = {
    400: ("bad_request", "İstek geçersiz."),
    401: ("unauthorized", "Bu işlem için giriş yapmanız gerekiyor."),
    403: ("forbidden", "Bu işlem için yetkiniz yok."),
    404: ("not_found", "İstenen kaynak bulunamadı."),
    405: ("method_not_allowed", "Bu işlem desteklenmiyor."),
    413: ("payload_too_large", "Gönderilen dosya veya metin çok büyük."),
    415: ("unsupported_media_type", "Desteklenmeyen dosya türü."),
    429: ("rate_limited", "Çok fazla istek gönderdiniz. Lütfen biraz bekleyip tekrar deneyin."),
    502: ("upstream_error", "Bağlı bir servisten geçersiz yanıt alındı."),
    503: ("service_unavailable", "Servis şu anda kullanılamıyor."),
    504: ("upstream_timeout", "Bağlı servis zamanında yanıt vermedi."),
}


class AppError(Exception):
    """Domain error with a stable code and a Turkish, user-facing message."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def error_body(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


def _http_exception_payload(status_code: int, detail: object) -> tuple[str, str]:
    code, default_message = _STATUS_MESSAGES.get(status_code, ("http_error", "İstek işlenemedi."))
    # Starlette fills `detail` with the English status phrase by default; only a
    # detail our own code raised (Turkish, user-facing) is passed through.
    try:
        phrase = HTTPStatus(status_code).phrase
    except ValueError:
        phrase = ""
    message = detail if isinstance(detail, str) and detail and detail != phrase else default_message
    return code, message


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = _http_exception_payload(exc.status_code, exc.detail)
        return JSONResponse(
            error_body(code, message), status_code=exc.status_code, headers=getattr(exc, "headers", None)
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = sorted({".".join(str(p) for p in err.get("loc", []) if p != "body") for err in exc.errors()})
        suffix = f" Hatalı alanlar: {', '.join(f for f in fields if f)}" if fields else ""
        return JSONResponse(error_body("validation_error", "İstek verisi geçersiz." + suffix), status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error", extra={"error_type": type(exc).__name__})
        return JSONResponse(
            error_body("internal_error", "Beklenmeyen bir hata oluştu. Lütfen daha sonra tekrar deneyin."),
            status_code=500,
        )
