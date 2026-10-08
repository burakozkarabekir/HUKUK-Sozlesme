from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import ClassVar

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app.auth import RateLimiter
from app.auth.session import SESSION_COOKIE, Signer
from app.db import connect
from app.llm.client import LLMClient
from app.main import create_app
from tests import fakes
from tests.conftest import make_settings

P = "/sozlesme"
HASH_ALI = bcrypt.hashpw(b"dogru-sifre-123", bcrypt.gensalt(rounds=4)).decode()
HASH_VELI = bcrypt.hashpw(b"veli-sifre-456", bcrypt.gensalt(rounds=4)).decode()
SECRET = "s" * 40
FETCH = {"X-Requested-With": "fetch"}


def basic(user: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


@pytest.fixture
def basic_client(tmp_path: Path):
    settings = make_settings(
        tmp_path, AUTH_MODE="basic", BASIC_AUTH_USERS=f"ali:{HASH_ALI},veli:{HASH_VELI}", BASIC_AUTH_ADMINS="ali"
    )
    with TestClient(create_app(settings)) as client:
        yield client


# --- basic ---


def test_basic_requires_credentials(basic_client: TestClient) -> None:
    response = basic_client.get(f"{P}/api/me")
    assert response.status_code == 401 and response.headers["www-authenticate"].startswith("Basic")
    assert response.json()["error"]["code"] == "unauthorized"
    index = basic_client.get(f"{P}/")
    assert index.status_code == 401 and "www-authenticate" in index.headers
    assert basic_client.get(f"{P}/api/me", headers=basic("ali", "yanlis")).status_code == 401
    assert basic_client.get(f"{P}/api/me", headers=basic("kimse", "dogru-sifre-123")).status_code == 401
    assert basic_client.get(f"{P}/health").status_code == 200  # public
    assert basic_client.get(f"{P}/static/app.css").status_code == 200  # public assets


def test_basic_login_and_roles(basic_client: TestClient) -> None:
    me = basic_client.get(f"{P}/api/me", headers=basic("ali", "dogru-sifre-123")).json()
    assert me["username"] == "ali" and me["is_admin"] and me["auth_mode"] == "basic"
    assert basic_client.get(f"{P}/", headers=basic("ali", "dogru-sifre-123")).status_code == 200
    veli = basic("veli", "veli-sifre-456")
    assert not basic_client.get(f"{P}/api/me", headers=veli).json()["is_admin"]
    forbidden = basic_client.get(f"{P}/api/admin/status", headers=veli)
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "forbidden"
    assert basic_client.get(f"{P}/api/admin/status", headers=basic("ali", "dogru-sifre-123")).status_code == 200


def test_basic_brute_force_is_blocked(basic_client: TestClient) -> None:
    for _ in range(10):
        assert basic_client.get(f"{P}/api/me", headers=basic("ali", "yanlis")).status_code == 401
    blocked = basic_client.get(f"{P}/api/me", headers=basic("ali", "dogru-sifre-123"))
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "too_many_failed_logins"


def test_csrf_header_required_for_unsafe_methods(basic_client: TestClient) -> None:
    auth = basic("ali", "dogru-sifre-123")
    rejected = basic_client.post(f"{P}/api/admin/sync", headers=auth, json={})
    assert rejected.status_code == 403 and rejected.json()["error"]["code"] == "csrf_rejected"
    accepted = basic_client.post(f"{P}/api/chat", headers={**auth, **FETCH}, json={"message": "   "})
    assert accepted.status_code == 400  # passed the CSRF check, failed validation


def test_access_log_contains_user(basic_client: TestClient, tmp_path: Path) -> None:
    basic_client.get(f"{P}/api/me", headers=basic("veli", "veli-sifre-456"))
    lines = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8").splitlines()
    access = [json.loads(line) for line in lines if '"app.access"' in line and "/api/me" in line]
    assert access[-1]["user"] == "veli"
    assert "veli-sifre-456" not in "\n".join(lines)


# --- rate limit ---


def test_rate_limit(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path, RATE_LIMIT_PER_MINUTE=3))) as client:
        statuses = [client.get(f"{P}/api/me").status_code for _ in range(4)]
        assert statuses == [200, 200, 200, 429]
        limited = client.get(f"{P}/api/me")
        assert limited.json()["error"]["code"] == "rate_limited" and int(limited.headers["retry-after"]) >= 1
        assert client.get(f"{P}/health").status_code == 200  # not limited


def test_rate_limiter_window() -> None:
    limiter = RateLimiter(2)
    assert limiter.allow("a")[0] and limiter.allow("a")[0] and not limiter.allow("a")[0]
    assert limiter.allow("b")[0]


# --- entra ---


class FakeMsal:
    claims: ClassVar[dict] = {}

    def __init__(self, settings) -> None:
        self.settings = settings

    def initiate_auth_code_flow(self, scopes, redirect_uri):
        return {"auth_uri": f"https://login.test/authorize?state=st1&redirect_uri={redirect_uri}", "state": "st1"}

    def acquire_token_by_auth_code_flow(self, flow, params):
        if params.get("state") != flow["state"]:
            raise ValueError("state mismatch")
        if params.get("error"):
            return {"error": params["error"], "error_description": "Kullanıcı iptal etti"}
        return {"id_token_claims": FakeMsal.claims}


def entra_settings(tmp_path: Path, **overrides):
    values = {
        "AUTH_MODE": "entra",
        "ENTRA_TENANT_ID": "tenant-1",
        "ENTRA_CLIENT_ID": "client-1",
        "ENTRA_CLIENT_SECRET": "secret",
        "ENTRA_REDIRECT_URI": "https://intranet.test/sozlesme/auth/callback",
        "SESSION_SECRET_KEY": SECRET,
        "ALLOWED_GROUP_IDS": "g-hukuk",
        "ADMIN_GROUP_IDS": "g-admin",
    }
    values.update(overrides)
    return make_settings(tmp_path, **values)


@pytest.fixture
def entra_client(tmp_path: Path):
    app = create_app(entra_settings(tmp_path))
    app.state.msal_factory = FakeMsal
    # base_url https so Secure cookies are sent back by the test client
    with TestClient(app, base_url="https://testserver") as client:
        yield client


def login(client: TestClient, claims: dict):
    FakeMsal.claims = claims
    start = client.get(f"{P}/auth/login", follow_redirects=False)
    assert start.status_code == 302 and start.headers["location"].startswith("https://login.test/authorize")
    return client.get(f"{P}/auth/callback", params={"state": "st1", "code": "c"}, follow_redirects=False)


def test_entra_redirects_and_rejects_without_session(entra_client: TestClient) -> None:
    index = entra_client.get(f"{P}/", follow_redirects=False)
    assert index.status_code == 302 and index.headers["location"] == "/sozlesme/auth/login"
    api = entra_client.get(f"{P}/api/me")
    assert api.status_code == 401 and api.json()["error"]["code"] == "unauthorized"


def test_entra_login_flow_and_cookie_flags(entra_client: TestClient, tmp_path: Path) -> None:
    callback = login(
        entra_client, {"oid": "u1", "preferred_username": "ayse@dogus.test", "name": "Ayşe", "groups": ["g-hukuk"]}
    )
    assert callback.status_code == 302 and callback.headers["location"] == "/sozlesme/"
    cookie_header = next(v for k, v in callback.headers.multi_items() if k == "set-cookie" and SESSION_COOKIE in v)
    lowered = cookie_header.lower()
    assert "httponly" in lowered and "secure" in lowered and "samesite=lax" in lowered and "path=/sozlesme/" in lowered
    me = entra_client.get(f"{P}/api/me").json()
    assert me["username"] == "ayse@dogus.test" and me["display_name"] == "Ayşe" and not me["is_admin"]
    assert entra_client.get(f"{P}/api/admin/status").status_code == 403
    actions = [r[0] for r in connect(tmp_path / "data" / "app.db").execute("SELECT action FROM audit_log")]
    assert "auth.login" in actions

    logout = entra_client.get(f"{P}/auth/logout", follow_redirects=False)
    assert logout.headers["location"].startswith("https://login.microsoftonline.com/tenant-1/oauth2/v2.0/logout")
    assert "post_logout_redirect_uri=https%3A%2F%2Fintranet.test%2Fsozlesme%2F" in logout.headers["location"]


def test_entra_admin_group(entra_client: TestClient) -> None:
    login(entra_client, {"oid": "u2", "preferred_username": "admin@dogus.test", "groups": ["g-admin"]})
    assert entra_client.get(f"{P}/api/me").json()["is_admin"]
    assert entra_client.get(f"{P}/api/admin/status").status_code == 200


def test_entra_group_denied_and_overage(entra_client: TestClient) -> None:
    denied = login(entra_client, {"oid": "u3", "preferred_username": "x@dogus.test", "groups": ["g-diger"]})
    assert denied.status_code == 403 and "yetkiniz yok" in denied.text
    overage = login(
        entra_client, {"oid": "u4", "preferred_username": "y@dogus.test", "_claim_names": {"groups": "src1"}}
    )
    assert overage.status_code == 403 and "çok fazla grup" in overage.text
    assert entra_client.get(f"{P}/api/me").status_code == 401


def test_entra_state_mismatch_and_error(entra_client: TestClient) -> None:
    FakeMsal.claims = {}
    entra_client.get(f"{P}/auth/login", follow_redirects=False)
    assert entra_client.get(f"{P}/auth/callback", params={"state": "baska"}).status_code == 400
    cancelled = entra_client.get(f"{P}/auth/callback", params={"state": "st1", "error": "access_denied"})
    assert cancelled.status_code == 401 and "iptal" in cancelled.text
    fresh = TestClient(entra_client.app, base_url="https://testserver")
    assert fresh.get(f"{P}/auth/callback", params={"state": "st1"}).status_code == 400  # no flow cookie


def test_tampered_or_expired_session_rejected(entra_client: TestClient) -> None:
    signer = Signer(SECRET)
    good = signer.dumps({"username": "a@b", "admin": True}, 60)
    _, sig = good.rsplit(".", 1)
    forged_payload = base64.urlsafe_b64encode(b'{"username":"a@b","admin":true,"exp":9999999999}').rstrip(b"=").decode()
    entra_client.cookies.set(SESSION_COOKIE, f"{forged_payload}.{sig}", path="/sozlesme/")
    assert entra_client.get(f"{P}/api/me").status_code == 401
    entra_client.cookies.set(SESSION_COOKIE, signer.dumps({"username": "a@b"}, -10), path="/sozlesme/")
    assert entra_client.get(f"{P}/api/me").status_code == 401
    entra_client.cookies.set(SESSION_COOKIE, good, path="/sozlesme/")
    assert entra_client.get(f"{P}/api/me").json()["is_admin"]


def test_signer_roundtrip() -> None:
    signer = Signer(SECRET)
    assert signer.loads(signer.dumps({"a": 1}, 60))["a"] == 1
    assert Signer("başka" * 10).loads(signer.dumps({"a": 1}, 60)) is None
    assert signer.loads("bozuk") is None and signer.loads(None) is None


# --- admin extras ---


def test_health_deps_and_audit(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    settings = make_settings(tmp_path, LOCAL_CONTRACTS_DIR=contracts)
    app = create_app(settings)
    app.state.llm = LLMClient(settings, responses_api=fakes.FakeResponses(fakes.response("TAMAM", 5, 1)))
    with TestClient(app) as client:
        checks = client.get(f"{P}/health/deps").json()["checks"]
        assert checks["Veritabanı"]["ok"] and checks["Yapay zeka (sohbet modeli)"]["ok"]
        assert checks["Embedding"]["ok"] is None and checks["OCR"]["ok"] is None
        assert checks["Sözleşme kaynağı"]["message"] == "local: 0 sözleşme bulundu"
        client.get(f"{P}/api/search", params={"q": "fesih"})
        audit = client.get(f"{P}/api/admin/audit", params={"limit": 5}).json()["items"]
        assert audit[0]["action"] == "search" and audit[0]["details"]["q"] == "fesih"


def test_health_deps_reports_failures(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, LOCAL_CONTRACTS_DIR=tmp_path / "yok")
    with TestClient(create_app(settings)) as client:
        checks = client.get(f"{P}/health/deps").json()["checks"]
    assert (
        not checks["Yapay zeka (sohbet modeli)"]["ok"]
        and "yapılandırılmamış" in checks["Yapay zeka (sohbet modeli)"]["message"]
    )
    assert not checks["Sözleşme kaynağı"]["ok"]


def test_extra_security_headers(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        headers = client.get(f"{P}/health").headers
    assert "camera=()" in headers["permissions-policy"] and headers["cross-origin-opener-policy"] == "same-origin"
