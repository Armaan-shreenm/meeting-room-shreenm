"""The actor is resolved from X-User-Email, and production has no fallback.

An unauthenticated request that silently books a room in somebody else's name is
worse than a request that fails, so ``DEV_USER_EMAIL`` applies only outside
production. These tests pin that down.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.core.auth import CurrentUser
from app.core.messages import ACTOR_NOT_IDENTIFIED


@pytest.fixture()
def client():
    """A throwaway app exposing the dependency.

    Deliberately not app.main: the StaticFiles mount at "/" is a catch-all that
    shadows any route added after it.
    """
    probe = FastAPI()

    @probe.get("/whoami")
    def whoami(actor: CurrentUser):
        return {
            "id": actor.id,
            "full_name": actor.full_name,
            "email": actor.email,
            "role": actor.role.value,
        }

    return TestClient(probe, raise_server_exceptions=False)


@pytest.fixture()
def as_production(monkeypatch):
    """Flip the running settings object to production for one test."""
    monkeypatch.setattr(settings, "environment", "production")
    assert settings.is_production
    yield


# ----------------------------------------------------------- outside production


def test_header_identifies_the_actor(client):
    response = client.get(
        "/whoami", headers={"X-User-Email": "reception.mumbai@shreenm.com"}
    )
    assert response.status_code == 200
    assert response.json()["role"] == "RECEPTION"


def test_header_is_case_insensitive(client):
    response = client.get("/whoami", headers={"X-User-Email": "ADMIN@SHREENM.COM"})
    assert response.status_code == 200
    assert response.json()["role"] == "ADMIN"


def test_missing_header_falls_back_outside_production(client):
    assert not settings.is_production
    response = client.get("/whoami")
    assert response.status_code == 200
    assert response.json()["email"] == settings.dev_user_email


def test_unknown_address_is_401(client):
    response = client.get("/whoami", headers={"X-User-Email": "nobody@shreenm.com"})
    assert response.status_code == 401
    detail = response.json()["detail"]
    assert "nobody@shreenm.com" in detail
    assert "directory" in detail
    # Spec section 10: never a bare "Invalid selection".
    assert detail.lower() not in {"invalid selection", "booking failed"}


# --------------------------------------------------------------- in production


def test_missing_header_is_401_in_production(client, as_production):
    """The whole point of the fix: no silent impersonation in production."""
    response = client.get("/whoami")
    assert response.status_code == 401
    assert response.json()["detail"] == ACTOR_NOT_IDENTIFIED


def test_header_still_works_in_production(client, as_production):
    response = client.get(
        "/whoami", headers={"X-User-Email": "priya.nair@shreenm.com"}
    )
    assert response.status_code == 200
    assert response.json()["full_name"] == "Priya Nair"


def test_blank_header_is_401_in_production(client, as_production):
    """A header present but empty must not fall through to the default actor."""
    response = client.get("/whoami", headers={"X-User-Email": "   "})
    assert response.status_code == 401
    assert response.json()["detail"] == ACTOR_NOT_IDENTIFIED
