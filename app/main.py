"""FastAPI application factory.

Served behind nginx at `/sozlesme` (root_path). The UI uses relative URLs only,
so it works both at `http://127.0.0.1:8001/sozlesme/` and behind the proxy.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from app import __version__
from app.api import routes_admin, routes_chat, routes_contracts, routes_health, routes_me, routes_search
from app.config import BASE_DIR, Settings, apply_network_env, get_settings
from app.db import check_fts5, connect, migrate
from app.errors import register_error_handlers
from app.llm.client import LLMClient
from app.llm.usage import make_db_usage_recorder
from app.logging_conf import setup_logging
from app.middleware import RequestContextMiddleware

STATIC_DIR = BASE_DIR / "app" / "static"
logger = logging.getLogger(__name__)


def init_storage(settings: Settings) -> None:
    """Create data dirs and apply DB migrations (idempotent, multi-worker safe)."""
    if not check_fts5():
        raise RuntimeError("SQLite FTS5 desteği bulunamadı. Python/SQLite kurulumunu kontrol edin.")
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    try:
        applied = migrate(conn)
    finally:
        conn.close()
    if applied:
        logger.info("migrations_applied", extra={"versions": applied})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings)
    apply_network_env(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        init_storage(settings)
        logger.info(
            "app_started",
            extra={"env": settings.APP_ENV, "auth_mode": settings.AUTH_MODE, "version": __version__},
        )
        yield
        await app.state.llm.aclose()
        logger.info("app_stopped")

    is_prod = settings.APP_ENV == "prod"
    app = FastAPI(
        title=settings.APP_NAME,
        version=__version__,
        root_path=settings.ROOT_PATH,
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if is_prod else "/openapi.json",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    # The SDK client is built lazily on first use, so a missing key only
    # affects LLM endpoints, not startup.
    app.state.llm = LLMClient(settings, usage_recorder=make_db_usage_recorder(settings.db_path))

    register_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(routes_health.router)
    app.include_router(routes_me.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_contracts.router)
    app.include_router(routes_search.router)
    app.include_router(routes_admin.router)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False, response_model=None)
    async def index(request: Request) -> Response:
        # Relative asset URLs only resolve correctly at `<root_path>/`. uvicorn
        # --root-path prefixes every path, so `/sozlesme` (no slash) and a bare
        # `/` (local dev without the prefix) are redirected there.
        root = request.scope.get("root_path", "")
        if root and request.url.path != root + "/":
            # Relative Location: an absolute one would carry the upstream host
            # (127.0.0.1:8001) instead of the public nginx host.
            return Response(status_code=307, headers={"location": root + "/"})
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
