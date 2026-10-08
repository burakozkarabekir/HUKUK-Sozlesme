"""Authentication. Phase 1 only implements AUTH_MODE=none; basic/entra arrive in phase 7."""

from __future__ import annotations

from fastapi import Depends, HTTPException

from app.config import Settings, get_settings
from app.logging_conf import user_var
from app.models import CurrentUser

# VARSAYIM: In AUTH_MODE=none (dev only) every request acts as a local admin.
DEV_USER = CurrentUser(username="gelistirici", display_name="Geliştirici", is_admin=True)


# Async on purpose: sync dependencies run in a worker thread with a copied
# context, so a `user_var` set there would be invisible to the endpoint's logs.
# VARSAYIM: Phase 7 moves user resolution into middleware so the access log
# carries the user as well.
async def get_current_user(settings: Settings = Depends(get_settings)) -> CurrentUser:
    if settings.AUTH_MODE == "none":
        user = DEV_USER
    else:
        raise HTTPException(status_code=401, detail="Bu kimlik doğrulama modu henüz etkin değil.")
    user_var.set(user.username)
    return user


async def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Bu işlem yalnızca yöneticiler içindir.")
    return user
