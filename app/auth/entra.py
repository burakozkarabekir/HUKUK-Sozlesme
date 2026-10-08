"""Microsoft Entra ID sign-in (OIDC authorization code flow via msal).

/auth/login -> Microsoft -> /auth/callback -> signed session cookie.
Group membership comes from the `groups` claim of the ID token, so the app
registration must emit group claims ("groupMembershipClaims": "SecurityGroup").
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth.session import FLOW_COOKIE, SESSION_COOKIE, Signer
from app.config import Settings, get_settings
from app.db import connect
from app.logging_conf import write_audit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"], include_in_schema=False)

FLOW_MAX_AGE = 600


def authority(settings: Settings) -> str:
    return f"https://login.microsoftonline.com/{settings.ENTRA_TENANT_ID}"


def build_msal_app(settings: Settings) -> Any:
    import msal  # imported lazily: only needed in entra mode

    return msal.ConfidentialClientApplication(
        settings.ENTRA_CLIENT_ID,
        authority=authority(settings),
        client_credential=settings.ENTRA_CLIENT_SECRET.get_secret_value(),
    )


def cookie_path(request: Request) -> str:
    return (request.scope.get("root_path") or "") + "/"


def set_cookie(response: Response, request: Request, settings: Settings, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path=cookie_path(request),
        httponly=True,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite="lax",
    )


def error_page(message: str, status_code: int = 403) -> HTMLResponse:
    from html import escape

    body = (
        "<!doctype html><html lang='tr'><head><meta charset='utf-8'><title>Giriş</title></head>"
        "<body style='font-family:sans-serif;max-width:640px;margin:60px auto'>"
        f"<h2>Giriş yapılamadı</h2><p>{escape(message)}</p><p><a href='login'>Tekrar dene</a></p></body></html>"
    )
    return HTMLResponse(body, status_code=status_code)


def user_from_claims(claims: dict[str, Any], settings: Settings) -> tuple[dict[str, Any] | None, str | None]:
    """Session payload from ID token claims, or (None, Turkish error)."""
    if "_claim_names" in claims and "groups" in claims.get("_claim_names", {}):
        # VARSAYIM: Group overage (>200 groups) is not resolved via Graph; users must be
        # in fewer groups or the app must emit "groups assigned to the application".
        return None, "Grup bilgisi alınamadı (çok fazla grup). Yöneticinize başvurun."
    groups = set(claims.get("groups") or [])
    allowed = set(settings.allowed_group_ids)
    admins = set(settings.admin_group_ids)
    if allowed and not (groups & (allowed | admins)):
        return None, "Bu uygulamayı kullanma yetkiniz yok. Erişim için Hukuk/IT birimine başvurun."
    username = claims.get("preferred_username") or claims.get("upn") or claims.get("email") or claims.get("oid")
    if not username:
        return None, "Kullanıcı bilgisi alınamadı."
    return {
        "sub": claims.get("oid") or claims.get("sub"),
        "username": username,
        "name": claims.get("name") or username,
        "admin": bool(groups & admins),
    }, None


@router.get("/login")
def login(request: Request) -> Response:
    settings = get_settings_from(request)
    app = request.app.state.msal_factory(settings)
    flow = app.initiate_auth_code_flow(scopes=[], redirect_uri=settings.ENTRA_REDIRECT_URI)
    response = RedirectResponse(flow["auth_uri"], status_code=302)
    set_cookie(
        response,
        request,
        settings,
        FLOW_COOKIE,
        request.app.state.signer.dumps({"flow": flow}, FLOW_MAX_AGE),
        FLOW_MAX_AGE,
    )
    return response


@router.get("/callback")
def callback(request: Request) -> Response:
    settings = get_settings_from(request)
    signer: Signer = request.app.state.signer
    stored = signer.loads(request.cookies.get(FLOW_COOKIE))
    if not stored:
        return error_page("Oturum açma isteğinin süresi doldu. Lütfen tekrar deneyin.", 400)
    app = request.app.state.msal_factory(settings)
    try:
        result = app.acquire_token_by_auth_code_flow(stored["flow"], dict(request.query_params))
    except ValueError:  # state mismatch / replay
        logger.warning("entra_flow_invalid")
        return error_page("Oturum açma isteği doğrulanamadı. Lütfen tekrar deneyin.", 400)
    if "error" in result:
        logger.warning("entra_login_error", extra={"error": result.get("error")})
        return error_page(
            f"Microsoft oturum açma hatası: {result.get('error_description') or result.get('error')}", 401
        )
    payload, error = user_from_claims(result.get("id_token_claims") or {}, settings)
    if payload is None:
        logger.warning("entra_login_denied")
        return error_page(error or "Erişim reddedildi.")
    max_age = settings.SESSION_MAX_AGE_HOURS * 3600
    response = RedirectResponse(cookie_path(request), status_code=302)
    set_cookie(response, request, settings, SESSION_COOKIE, signer.dumps(payload, max_age), max_age)
    response.delete_cookie(FLOW_COOKIE, path=cookie_path(request))
    _audit(settings, "auth.login", payload["username"], request)
    return response


@router.get("/logout")
def logout(request: Request) -> Response:
    settings = get_settings_from(request)
    user = request.app.state.signer.loads(request.cookies.get(SESSION_COOKIE))
    parts = urlsplit(settings.ENTRA_REDIRECT_URI)
    home = f"{parts.scheme}://{parts.netloc}{cookie_path(request)}"
    target = f"{authority(settings)}/oauth2/v2.0/logout?{urlencode({'post_logout_redirect_uri': home})}"
    response = RedirectResponse(target, status_code=302)
    response.delete_cookie(SESSION_COOKIE, path=cookie_path(request))
    if user:
        _audit(settings, "auth.logout", user.get("username"), request)
    return response


def get_settings_from(request: Request) -> Settings:
    override = request.app.dependency_overrides.get(get_settings)
    return override() if override else get_settings()


def _audit(settings: Settings, action: str, username: str | None, request: Request) -> None:
    conn = connect(settings.db_path)
    try:
        write_audit(conn, action, user_id=username, ip=request.client.host if request.client else None)
    finally:
        conn.close()
