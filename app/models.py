"""Pydantic schemas shared by the API layer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=50_000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=50_000)
    # VARSAYIM: Chat history is kept per browser session (sessionStorage) and sent
    # with each request; the server keeps no chat state, so it works across workers.
    history: list[ChatMessage] = Field(default_factory=list, max_length=100)
    contract_id: str | None = None
