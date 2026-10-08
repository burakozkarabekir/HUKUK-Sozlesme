"""Current-user info for the UI header and menu visibility."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import get_current_user
from app.config import Settings, get_settings
from app.models import CurrentUser, MeResponse

router = APIRouter(prefix="/api", tags=["auth"])


@router.get("/me", response_model=MeResponse)
async def me(
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> MeResponse:
    return MeResponse(
        username=user.username,
        display_name=user.display_name,
        is_admin=user.is_admin,
        app_name=settings.APP_NAME,
        env=settings.APP_ENV,
        auth_mode=settings.AUTH_MODE,
    )
