import re

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from pydantic import SecretStr

from app import auth
from app.config import settings
from app.db import get_db
from app.main import app


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch):
    monkeypatch.setattr(settings, "auth_username", "test-user")
    monkeypatch.setattr(settings, "auth_password", SecretStr("test-only-login-password"))
    monkeypatch.setattr(settings, "auth_secret", SecretStr("test-only-signing-secret-at-least-32-characters"))
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    auth._failed_logins.clear()
    yield
    auth._failed_logins.clear()


def login(client, **changes):
    return client.post("/api/auth/login", json={"username": "test-user", "password": "test-only-login-password", **changes})


def test_login_session_and_no_secret_disclosure():
    with TestClient(app, headers={"X-Bini-Request": "1"}) as client:
        response = login(client)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {"username": "test-user"}
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=28800" in cookie
        assert "Path=/api" in cookie
        assert "test-only-login-password" not in response.text
        assert settings.auth_secret.get_secret_value() not in response.text
        session = client.get("/api/auth/session")
        assert session.status_code == 200
        assert session.json() == {"username": "test-user"}
        assert session.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("changes", [{"username": "wrong"}, {"password": "wrong"}, {"username": "বাংলা", "password": "ভুল"}])
def test_wrong_credentials_use_generic_error(changes):
    with TestClient(app, headers={"X-Bini-Request": "1"}) as client:
        response = login(client, **changes)
        assert response.status_code == 401
        assert response.json()["detail"] == "Incorrect username or password."


def test_every_data_route_rejects_unauthenticated_requests_before_database_access():
    def unavailable():
        raise AssertionError("An unauthenticated request must not access the database")
    app.dependency_overrides[get_db] = unavailable
    try:
        with TestClient(app, headers={"X-Bini-Request": "1"}) as client:
            # OpenAPI expands included routers into concrete routes on newer FastAPI.
            for path, operations in app.openapi()["paths"].items():
                if path == "/api/health" or path.startswith("/api/auth/"):
                    continue
                for method in operations:
                    if method not in {"get", "post", "patch", "delete"}:
                        continue
                    url = path
                    url = re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000001", url)
                    response = client.request(method, url)
                    assert response.status_code == 401, (method, path, response.text)
    finally:
        app.dependency_overrides.pop(get_db)


@pytest.mark.parametrize("header", [None, "Basic dXNlcjpwYXNz", "Bearer invalid"])
def test_missing_and_invalid_tokens(header):
    with TestClient(app, headers={"X-Bini-Request": "1"}) as client:
        response = client.get("/api/auth/session", headers={"Authorization": header} if header else {})
        assert response.status_code == 401


def test_token_tampering_expiry_and_credential_rotation(monkeypatch):
    with TestClient(app, headers={"X-Bini-Request": "1"}) as client:
        login(client)
        token = client.cookies.get(auth.COOKIE_NAME)
        client.cookies.clear()
        client.cookies.set(auth.COOKIE_NAME, token + "x")
        assert client.get("/api/auth/session").status_code == 401
        client.cookies.clear()
        client.cookies.set(auth.COOKIE_NAME, token)
        current = TimestampSigner.get_timestamp
        with monkeypatch.context() as patch:
            patch.setattr(TimestampSigner, "get_timestamp", lambda self: current(self) + auth.SESSION_SECONDS + 1)
            assert client.get("/api/auth/session").status_code == 401
        for key, value in [("auth_password", SecretStr("rotated-password")), ("auth_username", "rotated-user"), ("auth_secret", SecretStr("a-different-signing-secret-at-least-32-characters"))]:
            with monkeypatch.context() as patch:
                patch.setattr(settings, key, value)
                assert client.get("/api/auth/session").status_code == 401


@pytest.mark.parametrize("key,value", [("auth_username", ""), ("auth_password", SecretStr("")), ("auth_secret", SecretStr("short"))])
def test_missing_configuration_fails_closed(monkeypatch, key, value):
    monkeypatch.setattr(settings, key, value)
    with TestClient(app, headers={"X-Bini-Request": "1"}) as client:
        assert login(client).status_code == 503
        assert client.get("/api/transactions").status_code == 503


def test_login_throttling_recovers_after_one_minute(monkeypatch):
    monkeypatch.setattr(auth, "monotonic", lambda: 100)
    with TestClient(app, headers={"X-Bini-Request": "1"}) as client:
        for _ in range(10):
            assert login(client, password="incorrect").status_code == 401
        response = login(client)
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
        monkeypatch.setattr(auth, "monotonic", lambda: 161)
        assert login(client).status_code == 200


def test_cors_preflight_accepts_csrf_header_and_credentials():
    with TestClient(app, headers={"X-Bini-Request": "1"}) as client:
        response = client.options("/api/transactions", headers={"Origin": settings.allowed_origins[0],
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-bini-request"})
        assert response.status_code == 200
        assert "x-bini-request" in response.headers["access-control-allow-headers"].lower()
        assert response.headers["access-control-allow-credentials"] == "true"


def test_health_stays_public():
    class Database:
        def execute(self, statement):
            pass
    app.dependency_overrides[get_db] = lambda: Database()
    try:
        with TestClient(app, headers={"X-Bini-Request": "1"}) as client:
            assert client.get("/api/health").json() == {"status": "ok"}
    finally:
        app.dependency_overrides.pop(get_db)


def test_logout_clears_cookie_and_survives_refresh():
    with TestClient(app, headers={"X-Bini-Request": "1"}) as client:
        login(client)
        assert client.get("/api/auth/session").status_code == 200
        response = client.post("/api/auth/logout")
        assert response.status_code == 204
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert client.get("/api/auth/session").status_code == 401
        assert client.post("/api/auth/logout").status_code == 204


def test_secure_cookie_on_https(monkeypatch):
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    with TestClient(app, base_url="https://testserver", headers={"X-Bini-Request": "1"}) as client:
        response = login(client)
        assert "; Secure" in response.headers["set-cookie"]
        assert client.get("/api/auth/session").status_code == 200
        assert "; Secure" in client.post("/api/auth/logout").headers["set-cookie"]


def test_csrf_blocks_cross_origin_login_logout_and_writes():
    with TestClient(app) as client:
        assert login(client).status_code == 403
        client.headers["X-Bini-Request"] = "1"
        client.headers["Origin"] = "https://evil.example"
        assert login(client).status_code == 403
        client.headers["Origin"] = settings.allowed_origins[0]
        assert login(client).status_code == 200
        client.headers["Origin"] = "https://evil.example"
        assert client.post("/api/auth/logout").status_code == 403
        assert client.post("/api/investments/reconciliation/run").status_code == 403
        del client.headers["Origin"]
        del client.headers["X-Bini-Request"]
        assert client.post("/api/auth/logout").status_code == 403
        assert client.post("/api/investments/reconciliation/run").status_code == 403
        assert client.get("/api/auth/session").status_code == 200


def test_untrusted_origin_cannot_pass_preflight():
    with TestClient(app) as client:
        response = client.options("/api/auth/login", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-bini-request"})
        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers
