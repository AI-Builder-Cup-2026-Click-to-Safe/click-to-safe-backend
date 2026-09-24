import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth

TOKEN = "a-sufficiently-long-test-token"


@pytest.fixture
def public_client(monkeypatch):
    """Build the app the way Cloud Run does, with the public guard installed."""
    monkeypatch.setenv("PUBLIC_API", "true")
    monkeypatch.setenv("API_TOKEN", TOKEN)
    monkeypatch.setenv("VIDEO_BUCKET", "test-videos")
    from app import main

    importlib.reload(main)
    yield TestClient(main.app)
    monkeypatch.delenv("PUBLIC_API")
    importlib.reload(main)


def guarded_app() -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/v1/guarded")
    def guarded():
        return {"reached": True}

    auth.install(app)
    return app


def test_presented_token_accepts_bearer_scheme():
    assert auth.presented_token("Bearer secret-value") == "secret-value"
    assert auth.presented_token("bearer secret-value") == "secret-value"


def test_presented_token_accepts_a_bare_token():
    assert auth.presented_token("secret-value") == "secret-value"


def test_presented_token_rejects_another_scheme():
    assert auth.presented_token("Basic secret-value") == ""


def test_token_matches_only_the_exact_secret():
    assert auth.token_matches(f"Bearer {TOKEN}", TOKEN)
    assert not auth.token_matches(f"Bearer {TOKEN}x", TOKEN)
    assert not auth.token_matches("Bearer ", TOKEN)
    assert not auth.token_matches("", TOKEN)


def test_guard_is_absent_unless_public_api_is_set(monkeypatch):
    monkeypatch.delenv("PUBLIC_API", raising=False)
    # Agent Runtime authenticates with IAM and injects no token, so an installed
    # guard there would reject every legitimate caller.
    assert TestClient(guarded_app()).get("/v1/guarded").status_code == 200


def test_public_request_without_a_token_is_rejected(public_client):
    response = public_client.post("/v1/review", json={"video_uri": "gs://test-videos/uploads/x"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_public_request_with_a_wrong_token_is_rejected(public_client):
    response = public_client.post(
        "/v1/review",
        json={"video_uri": "gs://test-videos/uploads/x"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/healthz", "/health"])
def test_health_answers_without_a_token(public_client, path):
    # Cloud Run probes before any caller authenticates. /health exists because
    # Google's front end answers /healthz itself and never forwards it.
    assert public_client.get(path).json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/healthz/", "/health/"])
def test_a_trailing_slash_does_not_turn_health_into_a_401(public_client, path):
    # Middleware runs before the router, so an exact-match check would reject
    # the slashed form ahead of the redirect that would have normalised it.
    assert public_client.get(path, follow_redirects=True).status_code == 200


def test_is_public_covers_only_the_health_paths():
    assert auth.is_public("/health") and auth.is_public("/healthz/")
    assert not auth.is_public("/") and not auth.is_public("/v1/review")


def test_correct_token_reaches_the_route(public_client):
    # Validation rejecting the body proves the request passed authentication.
    response = public_client.post(
        "/v1/review", json={}, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 422


def test_unconfigured_token_refuses_instead_of_falling_open(monkeypatch):
    monkeypatch.setenv("PUBLIC_API", "true")
    monkeypatch.delenv("API_TOKEN", raising=False)
    response = TestClient(guarded_app()).get("/v1/guarded")
    assert response.status_code == 503
