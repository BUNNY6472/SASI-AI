"""Owner-token boundaries, tested without provider or cloud calls."""
import time

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner, URLSafeTimedSerializer

from app.config import Settings
from app.files import StoredFile
from app.main import COOKIE, SESSION_MAX_AGE, create_app
from app.models import FileRef, FinalResult, Run


OWNER_TOKEN = "owner-token-for-local-tests-only"
SESSION_KEY = "separate-test-session-secret-never-used-in-production"


def settings(**overrides):
    return Settings(_env_file=None, **{
        "vertex_ai": False, "trial_credits_only": False, "allow_simulated": False,
        "anthropic_api_key": "", "gemini_api_key": "", "key_store": "env",
        "storage_backend": "memory", "static_dir": "/nonexistent",
        "require_access_token": True, "app_access_code": OWNER_TOKEN,
        "session_secret": SESSION_KEY, **overrides,
    })


@pytest.mark.parametrize("overrides,missing", [
    ({"app_access_code": ""}, "APP_ACCESS_CODE"),
    ({"app_access_code": " \t\n"}, "APP_ACCESS_CODE"),
    ({"session_secret": ""}, "SESSION_SECRET"),
    ({"session_secret": " \t\n"}, "SESSION_SECRET"),
    ({"session_secret": "dev-only-change-me"}, "SESSION_SECRET"),
    ({"session_secret": " dev-only-change-me "}, "SESSION_SECRET"),
])
def test_required_auth_refuses_unsafe_configuration_before_creating_clients(monkeypatch, overrides, missing):
    def client_must_not_start(*args, **kwargs):
        pytest.fail("provider clients were constructed before authentication was validated")

    monkeypatch.setattr("app.main.ModelRegistry", client_must_not_start)
    with pytest.raises(ValueError, match=missing):
        create_app(settings(**overrides))


def test_local_open_mode_remains_explicitly_available():
    assert Settings(_env_file=None).require_access_token is False
    with TestClient(create_app(settings(require_access_token=False, app_access_code="",
                                        session_secret="dev-only-change-me"))) as client:
        assert client.get("/api/limits").status_code == 200


def test_html_entry_revalidates_to_load_current_upload_client(tmp_path):
    (tmp_path / "index.html").write_text("<html><body>SASI</body></html>", encoding="utf-8")
    with TestClient(create_app(settings(static_dir=str(tmp_path))), base_url="https://testserver") as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/runs"), ("POST", "/api/runs"), ("GET", "/api/runs/private-run"),
    ("GET", "/api/runs/private-run/solution"),
    ("GET", "/api/runs/private-run/files/private-file"),
    ("GET", "/api/runs/private-run/events"),
    ("POST", "/api/runs/private-run/approve"),
    ("POST", "/api/runs/private-run/cancel"),
    ("POST", "/api/runs/private-run/resume"),
    ("DELETE", "/api/runs/private-run"),
    ("POST", "/api/files"), ("DELETE", "/api/files/private-file"),
    ("GET", "/api/keys"), ("PUT", "/api/keys/gemini"), ("DELETE", "/api/keys"),
    ("GET", "/api/providers?refresh=true"), ("GET", "/api/limits"),
    ("GET", "/api/stats"), ("GET", "/api/profiles"),
    ("GET", "/api"), ("GET", "/api/runs/"), ("GET", "/%61pi/runs"),
    ("GET", "/api%2fruns"),
    ("GET", "/docs"), ("GET", "/redoc"), ("GET", "/openapi.json"),
])
def test_anonymous_requests_cannot_read_or_modify_workspace(method, path):
    with TestClient(create_app(settings()), base_url="https://testserver") as client:
        response = client.request(method, path)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"


def test_login_cookie_is_private_and_logout_removes_access():
    with TestClient(create_app(settings()), base_url="https://testserver") as client:
        assert client.get("/api/health").json() == {"ok": True}
        assert client.get("/api/session").json() == {"authenticated": False, "gate_enabled": True}
        assert client.post("/api/login", json={"code": "wrong"}).status_code == 401
        response = client.post("/api/login", json={"code": OWNER_TOKEN})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
        assert OWNER_TOKEN not in cookie and SESSION_KEY not in cookie
        assert client.get("/api/runs").status_code == 200
        assert client.get("/api/session").json()["authenticated"] is True
        assert client.post("/api/logout").status_code == 200
        assert client.get("/api/runs").status_code == 401


def test_production_cookie_stays_secure_if_proxy_scheme_is_missing():
    with TestClient(create_app(settings()), base_url="http://testserver") as client:
        response = client.post("/api/login", json={"code": OWNER_TOKEN})
        assert "secure" in response.headers["set-cookie"].lower()
        # The client correctly refuses to send that cookie over plain HTTP.
        assert client.get("/api/runs").status_code == 401


def test_authenticated_downloads_and_events_do_not_allow_cache_reuse():
    with TestClient(create_app(settings()), base_url="https://testserver") as client:
        private_data = b"private uploaded content"
        attachment = StoredFile(id="private-file", name="notes.txt", mime="text/plain",
                                kind="text", size=len(private_data), data=private_data)
        code = 'def main():\n    print("private result")\n\nmain()\n'
        run = Run(id="private-run", request="Private request", status="completed", profile="software",
                  files=[FileRef(**attachment.ref())],
                  final=FinalResult(output=f"## solution.py\n```python\n{code}```"))
        client.portal.call(client.app.state.store.save, run)
        client.portal.call(client.app.state.controller.files.attach, run.id, [attachment])
        assert client.post("/api/login", json={"code": OWNER_TOKEN}).status_code == 200
        for path, expected in [
            (f"/api/runs/{run.id}", "Private request"),
            (f"/api/runs/{run.id}/solution", "private result"),
            (f"/api/runs/{run.id}/files/{attachment.id}", private_data.decode()),
            (f"/api/runs/{run.id}/events", "data: "),
        ]:
            response = client.get(path)
            assert response.status_code == 200 and expected in response.text
            assert response.headers["cache-control"] == "no-store"
        client.post("/api/logout")
        for suffix in ("", "/solution", f"/files/{attachment.id}", "/events"):
            response = client.get(f"/api/runs/{run.id}{suffix}")
            assert response.status_code == 401
            assert "Private request" not in response.text and "private result" not in response.text


def test_invalid_expired_and_rotated_sessions_are_rejected(monkeypatch):
    signer = URLSafeTimedSerializer(SESSION_KEY, salt="sasi-session")
    old_signer = URLSafeTimedSerializer("previous-session-key", salt="sasi-session")
    with monkeypatch.context() as clock:
        clock.setattr(TimestampSigner, "get_timestamp", lambda self: int(time.time()) - SESSION_MAX_AGE - 60)
        expired = signer.dumps("ok")
    invalid = ["malformed-cookie", signer.dumps("unexpected-payload"),
               old_signer.dumps("ok"), expired]
    with TestClient(create_app(settings()), base_url="https://testserver") as client:
        for token in invalid:
            response = client.get("/api/runs", headers={"cookie": f"{COOKIE}={token}"})
            assert response.status_code == 401
        assert client.get("/api/runs", params={"access_token": OWNER_TOKEN}).status_code == 401
        assert client.get("/api/runs", headers={"authorization": f"Bearer {OWNER_TOKEN}"}).status_code == 401
        assert client.get("/api/runs", headers={"cookie": f"{COOKIE}={signer.dumps('ok')}"}).status_code == 200
