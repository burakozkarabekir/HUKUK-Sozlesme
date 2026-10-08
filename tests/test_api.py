from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.main import STATIC_DIR, create_app

# uvicorn --root-path /sozlesme prepends the prefix to every request path, so
# tests call `/sozlesme/...` exactly like production traffic arriving via nginx.
P = "/sozlesme"


def test_health(client: TestClient) -> None:
    response = client.get(f"{P}/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["db"] == "ok" and body["env"] == "dev"


def test_health_without_prefix(client: TestClient) -> None:
    # Local dev without --root-path: API routes still match the bare path.
    assert client.get("/health").status_code == 200


def test_security_headers(client: TestClient) -> None:
    headers = client.get(f"{P}/health").headers
    assert headers["content-security-policy"].startswith("default-src 'self'")
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "SAMEORIGIN"
    assert headers["x-request-id"]


def test_request_id_is_echoed_only_when_safe(client: TestClient) -> None:
    assert client.get(f"{P}/health", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
    assert (
        client.get(f"{P}/health", headers={"X-Request-ID": "bad id\x7f"}).headers["x-request-id"]
        != "bad id\x7f"
    )


def test_not_found_uses_error_format(client: TestClient) -> None:
    response = client.get(f"{P}/api/yok")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "İstenen kaynak bulunamadı."}}


def test_method_not_allowed_uses_error_format(client: TestClient) -> None:
    response = client.post(f"{P}/health")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_me_in_dev_mode(client: TestClient) -> None:
    body = client.get(f"{P}/api/me").json()
    assert body["is_admin"] is True and body["env"] == "dev" and body["auth_mode"] == "none"


def test_index_and_static_assets(client: TestClient) -> None:
    index = client.get(f"{P}/")
    assert index.status_code == 200
    assert "Hukuk birimine aittir" in index.text
    assert client.get(f"{P}/static/app.js").status_code == 200
    assert client.get(f"{P}/static/app.css").status_code == 200


def test_index_redirects_to_prefixed_root(client: TestClient) -> None:
    # Bare `/` (local dev, no prefix): our index route answers with a relative Location.
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/sozlesme/"
    # `/sozlesme` without slash: Starlette's redirect_slashes (nginx also does this).
    response = client.get(P, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].endswith("/sozlesme/")


def test_without_root_path(tmp_path) -> None:
    from tests.conftest import make_settings

    app = create_app(make_settings(tmp_path, ROOT_PATH=""))
    with TestClient(app) as plain:
        assert plain.get("/").status_code == 200
        assert plain.get("/static/app.js").status_code == 200
        assert plain.get("/health").status_code == 200


def test_frontend_uses_only_relative_urls() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert not re.search(r'(?:src|href)="/(?!/)', html), "absolute URL in index.html"
    assert not re.search(r"""(?:api|fetch)\(\s*["'`]/""", js), "absolute fetch path in app.js"
    assert not re.search(r"https?://", html), "external URL (CDN) in index.html"


def test_frontend_never_uses_inner_html() -> None:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js.replace("NEVER written with innerHTML", "")
