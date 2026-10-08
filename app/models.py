"""Pydantic schemas shared by the API layer."""

from __future__ import annotations

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthResponse(BaseModel):
    status: str
    app: str
    env: str
    version: str
    db: str


class CurrentUser(BaseModel):
    username: str
    display_name: str
    is_admin: bool = False


class MeResponse(BaseModel):
    username: str
    display_name: str
    is_admin: bool
    app_name: str
    env: str
    auth_mode: str
