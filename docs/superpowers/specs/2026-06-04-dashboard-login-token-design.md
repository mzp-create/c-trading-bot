# Design: `/dashboard` One-Time Token Login

Date: 2026-06-04
Status: Approved (design)
Branch: refactor/trading-architecture

## 1. Background & motivation

The bot exposes a Telegram command interface (`monitoring/telegram_alerts.py`)
and a separate FastAPI web dashboard (`dashboard/api_server.py`, port 8999). The
dashboard uses HTTP Basic auth with a **random password generated at the
dashboard's startup** (`secrets.token_urlsafe(24)`), written to
`dashboard/.dashboard_password` and printed to console. There is no session — every
request re-checks Basic auth.

Goal: a Telegram `/dashboard` command that returns a one-tap login link, so the
user opens the portfolio dashboard from their phone **without** the password
appearing in chat.

## 2. Decision (resolved during brainstorming, 2026-06-04)

One-time **signed-token** login. The bot and dashboard are separate processes on
the same host; rather than an inter-process call, both sides share the dashboard's
existing random password (from `dashboard/.dashboard_password`) as an HMAC key.
The bot mints a short-lived single-use token; the dashboard verifies it, sets a
session cookie, and redirects to the dashboard.

## 3. Components

```
dashboard/login_token.py   mint(secret, ttl) -> token ; verify(secret, token) -> bool
                           HMAC-SHA256 signed, expiring, single-use (verify marks used).
dashboard/api_server.py    GET /login?token=...  -> verify, set signed session cookie, 302 to /
                           verify_password()      -> accept EITHER Basic password OR session cookie
                           _session_value()/_check_session()  cookie sign/verify (same password key)
monitoring/telegram_alerts.py
                           _cmd_dashboard()       -> read password file, mint token, build URL, reply
config/*.yaml              dashboard.public_url   default "http://localhost:8999"
```

`login_token.py` lives under `dashboard/` and is imported by both the dashboard
(direct) and the bot command (via `dashboard.login_token`), so the mint/verify
logic is defined once.

## 4. Token format (`login_token.py`)

- `mint(secret: str, ttl: int = 120) -> str`: payload = `{"exp": int(now)+ttl,
  "nonce": <random hex>}`; `body = urlsafe_b64(json(payload))`; `sig =
  hmac_sha256(secret, body)`; token = `f"{body}.{hexsig}"`.
- `verify(secret: str, token: str, used: set | None = None) -> bool`: split
  body/sig; recompute HMAC with `hmac.compare_digest`; reject if expired
  (`exp < now`); if a `used` set is passed, reject if the token's nonce is in it,
  else add it (single-use). Returns False on any malformed/tampered/expired/reused
  token — never raises.
- No secret material is logged. `secret` is the dashboard password string.

## 5. Dashboard changes (`api_server.py`)

- **Session cookie helpers** (signed with the same `DASHBOARD_PASSWORD`):
  `_make_session() -> str` = `urlsafe_b64({"exp": now+SESSION_TTL})` + `.hmacsig`;
  `_valid_session(cookie: str) -> bool` verifies sig + exp. `SESSION_TTL` default
  8h. Cookie name `dash_session`, attributes `HttpOnly`, `SameSite=Lax`,
  `Path=/`, and `Secure` only when the request is HTTPS.
- **`GET /login`** (no `verify_password` dependency): read `token` query param;
  `login_token.verify(DASHBOARD_PASSWORD, token, _USED_TOKENS)`; on success set the
  `dash_session` cookie and return a `RedirectResponse("/", 302)`; on failure
  return `401` with a short "invalid or expired login link" message. `_USED_TOKENS`
  is a module-level `set()` (single dashboard process).
- **`verify_password` extended**: accept the request if EITHER the existing Basic
  password matches OR the `dash_session` cookie is valid. It needs the `Request`
  to read the cookie (add `request: Request` param / use FastAPI's cookie access).
  All existing `Depends(verify_password)` routes then also accept a cookie session.
- `/healthz` stays unauthenticated (unchanged).

## 6. Bot command (`telegram_alerts.py`)

- `_cmd_dashboard(_args, bot) -> str`: 
  - Resolve the dashboard base URL: `bot.config.get("dashboard", {}).get(
    "public_url", "http://localhost:8999")`.
  - Read `dashboard/.dashboard_password` (parse the `DASHBOARD_PASSWORD=...` line).
    If absent/empty → return "⚠️ Dashboard not running (no .dashboard_password).
    Start it with dashboard/run.sh." (no link).
  - `token = login_token.mint(password, ttl=120)`; `url = f"{base}/login?token={token}"`.
  - Return a message with the tappable URL and a note: "one-time link, expires in
    2 min." Do NOT include the raw password.
- Register `dashboard` in the command dispatch map and add a `/dashboard` line to
  `_cmd_help`.
- Import is local inside the handler (`from dashboard.login_token import mint`) so
  the bot doesn't hard-depend on the dashboard package at module load.

## 7. Config

`dashboard.public_url` (string, default `http://localhost:8999`) in
`config/default.yaml` (and instance configs). Set to the server's reachable
address or tunnel URL for remote/phone access. `.gitignore` already ignores
`dashboard/.dashboard_password`.

## 8. Security & error handling

- Token: short TTL (120s) + single-use + HMAC-signed with the rotating dashboard
  password → low exposure even though it transits Telegram and may land in server
  logs; a leaked old link is dead within 2 min / after one use / after a dashboard
  restart.
- Session cookie: `HttpOnly` + signed; `Secure` only under HTTPS. The dashboard is
  plain HTTP — over a LAN/localhost this matches its existing posture; remote
  plain-HTTP exposure is a pre-existing property of the dashboard, not introduced
  here (noted, not solved).
- All `verify`/cookie checks are constant-time (`hmac.compare_digest`) and never
  raise on malformed input (treated as auth failure).
- `_cmd_dashboard` degrades gracefully (clear message) when the password file is
  missing or `public_url` is misconfigured.

## 9. Testing

- `tests/test_login_token.py`: round-trip valid; expired (`ttl<0` or patched time)
  rejected; tampered body/sig rejected; reused nonce rejected when a `used` set is
  passed; malformed token returns False (no raise).
- `tests/test_dashboard_login.py` (FastAPI `TestClient`): `GET /login?token=<valid>`
  → 302 + `dash_session` cookie set; a follow-up `GET /api/status` with that cookie
  → 200; `GET /login?token=bad` → 401; `GET /api/status` with no auth → 401.
- `tests/test_cmd_dashboard.py`: with a stub bot + a temp `.dashboard_password`,
  `_cmd_dashboard` returns a string containing `/login?token=` and the configured
  base URL, and does NOT contain the raw password; missing-file case returns the
  graceful warning.

## 10. Definition of done

- `dashboard/login_token.py` with mint/verify + tests.
- `/login` endpoint + cookie session + cookie-or-Basic `verify_password`, with
  TestClient tests.
- `/dashboard` Telegram command + help line + registration + offline test.
- `dashboard.public_url` config (default localhost) added to the configs.
- Existing dashboard routes still work under Basic auth (unchanged); full suite
  green.
