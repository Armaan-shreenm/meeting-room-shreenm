"""Aggregate router for everything served under /api."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
