# `/dashboard` One-Time Token Login — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Telegram `/dashboard` command that returns a one-tap, single-use login link to the web dashboard — no password in chat — by minting an HMAC-signed token the dashboard verifies and exchanges for a session cookie.

**Architecture:** A shared `dashboard/login_token.py` (`mint`/`verify`, HMAC-SHA256, expiring, optional single-use) keyed on the dashboard's existing random password. The bot mints a 120s single-use token; the dashboard's new `GET /login` verifies it and sets an 8h signed session cookie (the cookie value is just a long-TTL token from the same helper). `verify_password` accepts either Basic auth or the session cookie.

**Tech Stack:** Python stdlib `hmac`/`hashlib`/`base64`/`json`, FastAPI (`Request`, `RedirectResponse`), `pytest` + `fastapi.testclient.TestClient`.

**Source of truth:** spec `docs/superpowers/specs/2026-06-04-dashboard-login-token-design.md`.

## Current code

- `dashboard/api_server.py`: `DASHBOARD_PASSWORD = secrets.token_urlsafe(24)` (`:47`) written to `.dashboard_password` as `DASHBOARD_PASSWORD=<value>`; `verify_password(credentials: HTTPBasicCredentials = Depends(security))` (`:293-310`); imports `from fastapi import FastAPI, HTTPException, Header, Depends` (`:21`); routes use `Depends(verify_password)`; `/healthz` is unauthenticated; run on port 8999.
- `monitoring/telegram_alerts.py`: `cmd_map` dict (`:400-413`) dispatches commands; handlers are `@staticmethod _cmd_X(_args, bot) -> str`; `_cmd_help` (`:461`) lists commands.

---

## Task 1: `login_token.py` (mint/verify)

**Files:**
- Create: `dashboard/login_token.py`
- Test: `tests/test_login_token.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_login_token.py`:
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dashboard.login_token import mint, verify

SECRET = "test-secret-123"


def test_valid_roundtrip():
    t = mint(SECRET, ttl=120)
    assert verify(SECRET, t) is True


def test_wrong_secret_rejected():
    t = mint(SECRET, ttl=120)
    assert verify("other-secret", t) is False


def test_expired_rejected():
    t = mint(SECRET, ttl=-1)          # already expired
    assert verify(SECRET, t) is False


def test_tampered_body_rejected():
    t = mint(SECRET, ttl=120)
    body, sig = t.split(".", 1)
    assert verify(SECRET, body[:-1] + "X" + "." + sig) is False


def test_malformed_returns_false_no_raise():
    assert verify(SECRET, "not-a-token") is False
    assert verify(SECRET, "") is False


def test_single_use_with_used_set():
    used = set()
    t = mint(SECRET, ttl=120)
    assert verify(SECRET, t, used) is True      # first use
    assert verify(SECRET, t, used) is False     # reused -> rejected
    # without a used set, reuse is allowed (session-cookie semantics)
    t2 = mint(SECRET, ttl=120)
    assert verify(SECRET, t2) is True
    assert verify(SECRET, t2) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_login_token.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dashboard.login_token'`.

- [ ] **Step 3: Implement**

Create `dashboard/login_token.py`:
```python
"""HMAC-signed, expiring, optionally single-use tokens for dashboard login.

Used by BOTH the dashboard (verify a login token; mint/verify the session
cookie) and the Telegram bot (mint a login token). The `secret` is the
dashboard's random password (rotates on dashboard restart), so old tokens die.
"""

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional, Set


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def mint(secret: str, ttl: int = 120) -> str:
    """Return a signed token valid for `ttl` seconds."""
    payload = {"exp": int(time.time()) + int(ttl), "nonce": os.urandom(8).hex()}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_sign(secret, body)}"


def verify(secret: str, token: str, used: Optional[Set[str]] = None) -> bool:
    """True iff `token` has a valid signature, is unexpired, and (if `used` is
    given) has an unseen nonce — which is then recorded (single-use). Never
    raises; any malformed/tampered/expired/reused token returns False."""
    try:
        body, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _sign(secret, body)):
            return False
        payload = json.loads(_unb64(body))
        if int(payload["exp"]) < time.time():
            return False
        if used is not None:
            nonce = payload.get("nonce")
            if nonce in used:
                return False
            used.add(nonce)
        return True
    except Exception:
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_login_token.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add dashboard/login_token.py tests/test_login_token.py
git commit -m "feat(dashboard): HMAC login-token mint/verify"
```

---

## Task 2: Dashboard `/login` endpoint + cookie-or-Basic auth

**Files:**
- Modify: `dashboard/api_server.py`
- Test: `tests/test_dashboard_login.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_dashboard_login.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_dashboard_login.py -v`
Expected: FAIL — no `/login` route (404 instead of 302) / cookie auth not accepted.

- [ ] **Step 3: Implement**

In `dashboard/api_server.py`:

(a) Extend the FastAPI import (`:21`) and add response/token imports near the top imports:
```python
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.responses import RedirectResponse
```
Add (after the other imports / near `DASHBOARD_PASSWORD`):
```python
from dashboard.login_token import mint as _mint_token, verify as _verify_token

SESSION_COOKIE = "dash_session"
SESSION_TTL = 8 * 3600          # 8 hours
_USED_TOKENS: set = set()        # single dashboard process; consumes login tokens
```

(b) Replace `verify_password` (`:293-310`) so it accepts a valid session cookie OR Basic:
```python
def verify_password(request: Request,
                    credentials: HTTPBasicCredentials = Depends(security)):
    """Authorize via a valid `dash_session` cookie OR HTTP Basic password."""
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and _verify_token(DASHBOARD_PASSWORD, cookie):   # no used-set: reusable
        return True
    if credentials is None:
        raise HTTPException(
            status_code=401, detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="Hermes Trading Bot Dashboard"'})
    if credentials.password != DASHBOARD_PASSWORD:
        raise HTTPException(
            status_code=401, detail="Invalid password",
            headers={"WWW-Authenticate": 'Basic realm="Hermes Trading Bot Dashboard"'})
    return True
```

(c) Add the `/login` route (place it among the route definitions, e.g. right after `verify_password`/CORS, before the API endpoints — it must NOT depend on `verify_password`):
```python
@app.get("/login")
def login(token: str = ""):
    """One-time token login: verify, set a session cookie, redirect to the app."""
    if not _verify_token(DASHBOARD_PASSWORD, token, _USED_TOKENS):
        raise HTTPException(status_code=401, detail="Invalid or expired login link")
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(
        SESSION_COOKIE, _mint_token(DASHBOARD_PASSWORD, ttl=SESSION_TTL),
        httponly=True, samesite="lax", path="/")
    return resp
```

> `verify_password` now takes `request: Request` as its first param; FastAPI injects it automatically for every `Depends(verify_password)` route — no route signatures change.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_dashboard_login.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add dashboard/api_server.py tests/test_dashboard_login.py
git commit -m "feat(dashboard): /login one-time token -> session cookie; cookie-or-Basic auth"
```

---

## Task 3: `/dashboard` Telegram command + config

**Files:**
- Modify: `monitoring/telegram_alerts.py`, `config/default.yaml`, `config/long.yaml`, `config/short.yaml`
- Test: `tests/test_cmd_dashboard.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cmd_dashboard.py`:
```python
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from monitoring.telegram_alerts import TelegramNotifier as T


def _bot():
    return types.SimpleNamespace(
        config={"dashboard": {"public_url": "http://example.test:8999"}})


def test_dashboard_command_builds_login_link(tmp_path, monkeypatch):
    pw_file = tmp_path / ".dashboard_password"
    pw_file.write_text("DASHBOARD_PASSWORD=secretpw123\n")
    monkeypatch.setattr(T, "_dashboard_password_file",
                        staticmethod(lambda: pw_file), raising=False)
    out = T._cmd_dashboard("", _bot())
    assert "http://example.test:8999/login?token=" in out
    assert "secretpw123" not in out          # raw password never in the reply


def test_dashboard_command_missing_password_file(tmp_path, monkeypatch):
    missing = tmp_path / "nope.txt"
    monkeypatch.setattr(T, "_dashboard_password_file",
                        staticmethod(lambda: missing), raising=False)
    out = T._cmd_dashboard("", _bot())
    assert "not running" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_cmd_dashboard.py -v`
Expected: FAIL — `TelegramNotifier` has no `_cmd_dashboard`/`_dashboard_password_file`.

- [ ] **Step 3: Implement**

In `monitoring/telegram_alerts.py`, add `"/dashboard": self._cmd_dashboard,` to the `cmd_map` dict (`:400-413`), add a help line to `_cmd_help` (after the `/portfolio` line): `"/dashboard — One-tap web dashboard login\n"`, and add the handler + a path helper (next to the other `@staticmethod _cmd_*`):
```python
    @staticmethod
    def _dashboard_password_file():
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent / "dashboard"
                / ".dashboard_password")

    @staticmethod
    def _cmd_dashboard(_args: str, bot: "TradingBot") -> str:  # noqa: F821
        base = bot.config.get("dashboard", {}).get(
            "public_url", "http://localhost:8999")
        pw_file = TelegramNotifier._dashboard_password_file()
        if not pw_file.exists():
            return ("⚠️ Dashboard not running (no `.dashboard_password`). "
                    "Start it with `dashboard/run.sh`.")
        password = ""
        for line in pw_file.read_text().splitlines():
            if line.startswith("DASHBOARD_PASSWORD="):
                password = line.split("=", 1)[1].strip()
                break
        if not password:
            return "⚠️ Dashboard password not found in `.dashboard_password`."
        from dashboard.login_token import mint
        token = mint(password, ttl=120)
        url = f"{base}/login?token={token}"
        return ("🖥️ **Dashboard Login**\n"
                f"[Open dashboard]({url})\n\n"
                f"`{url}`\n\n"
                "_One-time link — expires in 2 minutes._")
```

- [ ] **Step 4: Add config**

Add a `dashboard:` block to `config/default.yaml`, `config/long.yaml`, `config/short.yaml` (top level):
```yaml
dashboard:
  public_url: "http://localhost:8999"   # set to your server's address/tunnel for remote access
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_cmd_dashboard.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add monitoring/telegram_alerts.py config/default.yaml config/long.yaml config/short.yaml tests/test_cmd_dashboard.py
git commit -m "feat(telegram): /dashboard one-tap login command + dashboard.public_url config"
```

---

## Task 4: Verification

- [ ] **Step 1: Run the new tests + dashboard import**

Run:
```bash
.venv/bin/python -m pytest tests/test_login_token.py tests/test_dashboard_login.py tests/test_cmd_dashboard.py -v
.venv/bin/python -c "import dashboard.api_server, monitoring.telegram_alerts; print('imports ok')"
```
Expected: all pass; imports ok.

- [ ] **Step 2: End-to-end smoke (offline)**

Run:
```bash
.venv/bin/python -c "
import dashboard.api_server as api
from dashboard.login_token import mint
from fastapi.testclient import TestClient
c = TestClient(api.app)
t = mint(api.DASHBOARD_PASSWORD, ttl=120)
r = c.get(f'/login?token={t}', follow_redirects=False)
print('login status', r.status_code, 'cookie set:', 'dash_session' in r.cookies)
print('api with cookie:', c.get('/api/status').status_code)
print('reused token:', c.get(f'/login?token={t}', follow_redirects=False).status_code)  # 401 (single-use)
"
```
Expected: `login status 302 cookie set: True`, `api with cookie: 200`, `reused token: 401`.

- [ ] **Step 3: Full suite (no regressions)**

Run: `.venv/bin/python -m pytest tests/ -q 2>&1 | tail -5`
Expected: all pass except the 2 pre-existing unrelated `tests/test_nonce_atomic.py`.

- [ ] **Step 4: Commit (if any fix was needed; else none)**

```bash
git add -A && git commit -m "test(dashboard): verify /dashboard login end-to-end"
```

---

## Self-review (plan author)

**Spec coverage:** §3 components → Tasks 1–3. §4 token format → Task 1. §5 dashboard `/login`+cookie+verify_password → Task 2 (session cookie = long-TTL `mint`/`verify`, reusing Task 1 — DRY). §6 `_cmd_dashboard` → Task 3. §7 config → Task 3. §8 security (single-use via `_USED_TOKENS`, httponly cookie, compare_digest, graceful missing-file) → Tasks 1–3. §9 testing → Tasks 1–4. §10 DoD → Task 4. ✔

**Placeholder scan:** none — complete code + exact commands throughout.

**Type consistency:** `mint(secret, ttl)` / `verify(secret, token, used=None)` used identically in dashboard (login token with `_USED_TOKENS`; session cookie without) and the bot command. `SESSION_COOKIE`/`SESSION_TTL`/`_USED_TOKENS`/`_mint_token`/`_verify_token` names consistent across Task 2. `_cmd_dashboard`/`_dashboard_password_file` consistent across Task 3 and its test. `dashboard.public_url` consistent across config + command + test.

**Note:** the `Secure` cookie flag is intentionally omitted (the dashboard serves plain HTTP; setting `Secure` would break cookie auth over HTTP). Matches spec §8's "Secure only under HTTPS" — i.e. not set here.
