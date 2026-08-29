"""Aggregate router for everything served under /api."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import availability, bookings, health, reference

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(reference.router, tags=["reference"])
api_router.include_router(availability.router, tags=["availability"])
api_router.include_router(bookings.router, tags=["bookings"])
