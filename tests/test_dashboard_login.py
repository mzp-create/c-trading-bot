import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient
import dashboard.api_server as api
from dashboard.login_token import mint

client = TestClient(api.app)


def test_login_token_sets_cookie_and_redirects():
    token = mint(api.DASHBOARD_PASSWORD, ttl=120)
    r = client.get(f"/login?token={token}", follow_redirects=False)
    assert r.status_code == 302
    assert "dash_session" in r.cookies


def test_cookie_session_authenticates_api():
    token = mint(api.DASHBOARD_PASSWORD, ttl=120)
    c = TestClient(api.app)
    c.get(f"/login?token={token}", follow_redirects=False)   # sets cookie on c
    r = c.get("/api/status")
    assert r.status_code == 200


def test_bad_token_rejected():
    r = client.get("/login?token=garbage", follow_redirects=False)
    assert r.status_code == 401


def test_no_auth_still_401():
    r = TestClient(api.app).get("/api/status")
    assert r.status_code == 401


def test_basic_auth_still_works():
    r = client.get("/api/status", auth=("", api.DASHBOARD_PASSWORD))
    assert r.status_code == 200
