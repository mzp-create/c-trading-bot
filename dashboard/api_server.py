"""
FastAPI Dashboard Server for Hermes Crypto Trading Bot.

Provides REST endpoints for viewing bot status, balance, trades,
configuration, and summary statistics. Serves a single-page HTML dashboard.
"""

import os
import time
import logging
import hashlib
import hmac
import json
from pathlib import Path
from datetime import datetime, timezone
import secrets
from typing import Optional
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import urllib.request

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
BOT_DIR = HERE.parent  # trading-bot/
CONFIG_PATH = BOT_DIR / "config" / "default.yaml"
# Trade history now lives in SQLite (one DB per instance). Union all that exist.
DB_PATHS = [
    BOT_DIR / "data" / "trading.db",
    BOT_DIR / "instances" / "long" / "data" / "trading.db",
    BOT_DIR / "instances" / "short" / "data" / "trading.db",
]
LOG_PATH = BOT_DIR / "logs" / "bot.log"
ENV_PATH = BOT_DIR / ".env"
STATIC_DIR = HERE / "static"

# ---------------------------------------------------------------------------
# Password auth — random password generated at startup
# ---------------------------------------------------------------------------
DASHBOARD_PASSWORD = secrets.token_urlsafe(24)  # 192-bit random password (P3-19)
PASSWORD_FILE = HERE / ".dashboard_password"
with open(PASSWORD_FILE, "w") as f:
    f.write(f"DASHBOARD_PASSWORD={DASHBOARD_PASSWORD}\n")
print(f"\n{'='*60}")
print(f"  🌐 Dashboard: http://localhost:8999")
print(f"  🔑 Password:  {DASHBOARD_PASSWORD}")
print(f"  📁 Saved to:  {PASSWORD_FILE}")
print(f"{'='*60}\n")

security = HTTPBasic(auto_error=False)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("dashboard")

# Load env vars manually (python-dotenv not installed)
def load_env():
    """Load .env file into os.environ."""
    if not ENV_PATH.exists():
        logger.warning(".env file not found at %s", ENV_PATH)
        return
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            # Remove leading 'export ' if present
            if key.startswith("export "):
                key = key[7:]
            if key and key not in os.environ:
                os.environ[key] = val

load_env()

# ---------------------------------------------------------------------------
# Bitfinex API helpers (raw v2 auth)
# ---------------------------------------------------------------------------
BITFINEX_BASE = "https://api.bitfinex.com"


def _bfx_nonce() -> str:
    """Return millisecond-precision nonce."""
    return str(int(time.time() * 1000))


def _bfx_sign(path: str, nonce: str, body: str) -> str:
    """Compute HMAC-SHA384 signature for Bitfinex v2 auth.

    Signature = HMAC-SHA384("/api" + path + nonce + body, api_secret)
    """
    secret = os.environ.get("BITFINEX_API_SECRET", "")
    sig_payload = f"/api{path}{nonce}{body}"
    sig = hmac.new(
        secret.encode("utf-8"),
        sig_payload.encode("utf-8"),
        hashlib.sha384,
    ).hexdigest()
    return sig


def fetch_bitfinex_balance() -> list:
    """Fetch wallet balances from Bitfinex via POST /v2/auth/r/wallets.

    Returns the raw list of wallet entries from the API.
    On failure returns an empty list.
    """
    api_key = os.environ.get("BITFINEX_API_KEY", "")
    api_secret = os.environ.get("BITFINEX_API_SECRET", "")
    if not api_key or not api_secret:
        logger.warning("Bitfinex API credentials not found in environment")
        return []

    path = "/v2/auth/r/wallets"
    nonce = _bfx_nonce()
    body = "{}"
    signature = _bfx_sign(path, nonce, body)

    headers = {
        "bfx-nonce": nonce,
        "bfx-apikey": api_key,
        "bfx-signature": signature,
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(
        f"{BITFINEX_BASE}{path}",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except urllib.error.HTTPError as exc:
        logger.error("Bitfinex API HTTP error: %s %s", exc.code, exc.read().decode())
        return []
    except Exception as exc:
        logger.error("Bitfinex API error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Data readers
# ---------------------------------------------------------------------------

def read_config() -> dict:
    """Read and return the bot YAML config."""
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("Config not found at %s", CONFIG_PATH)
        return {}
    except Exception as exc:
        logger.error("Error reading config: %s", exc)
        return {}


def _db_paths():
    """Existing per-instance DB files to read (overridable in tests)."""
    return [p for p in DB_PATHS if Path(p).exists()]


def read_trades() -> list[dict]:
    """Trade history from the SQLite DB(s), newest-first sort done by callers.

    Returns dicts using the same field names the frontend already consumes
    (`timestamp`, `symbol`, `side`, `entry_price`, `close_price`, `amount`,
    `pnl`, `reason`, `mode`), plus `instance`. Read-only WAL connections so a
    running bot is never blocked.
    """
    import sqlite3
    rows: list[dict] = []
    for db in _db_paths():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT ts, instance, symbol, side, entry_price, close_price, "
                "amount, pnl, fee, reason, mode FROM trades ORDER BY ts")
            for r in cur.fetchall():
                d = dict(r)
                d["timestamp"] = d.pop("ts")   # frontend expects 'timestamp'
                rows.append(d)
            conn.close()
        except sqlite3.Error as exc:
            logger.error("Error reading trades DB %s: %s", db, exc)
    rows.sort(key=lambda x: x.get("timestamp", ""))
    return rows


def read_log_tail(n: int = 50) -> list[str]:
    """Return the last n lines of the bot log file."""
    if not LOG_PATH.exists():
        return []
    try:
        with open(LOG_PATH) as f:
            lines = f.readlines()
        return [l.rstrip("\n") for l in lines[-n:]]
    except Exception as exc:
        logger.error("Error reading log: %s", exc)
        return []


def parse_bot_status_from_log(log_lines: list[str]) -> dict:
    """Parse bot status from the tail of the log."""
    status = {
        "status": "unknown",
        "mode": "unknown",
        "uptime": None,
        "last_analysis": None,
        "last_analysis_time": None,
        "initialized_at": None,
    }

    for line in reversed(log_lines):
        # Mode and initialization
        if "Mode:" in line and "initialized" in line:
            status["initialized_at"] = _extract_timestamp(line)
            if "PAPER" in line:
                status["mode"] = "PAPER"
            elif "LIVE" in line:
                status["mode"] = "LIVE"

        # Analysis signal
        if "Analysis:" in line and status["last_analysis"] is None:
            status["last_analysis_time"] = _extract_timestamp(line)
            if "HOLD" in line:
                status["last_analysis"] = "HOLD"
            elif "BUY" in line:
                status["last_analysis"] = "BUY"
            elif "SELL" in line:
                status["last_analysis"] = "SELL"
            # Try to extract confidence
            import re
            m = re.search(r"conf:\s*([\d.]+)", line)
            if m:
                status["last_analysis_confidence"] = float(m.group(1))

        # Shutdown signal
        if "Shutdown" in line and "received" in line:
            status["status"] = "stopped"
            break

        # If we see initialization and nothing after says stopped, it's running
        if "initialized" in line.lower() and (
            mode := re.search(r"Mode:\s*(\w+)", line)
        ):
            status["mode"] = mode.group(1)
            if status["status"] != "stopped":
                status["status"] = "running"

    # If no shutdown found and we have initialization, bot is running
    if status["status"] == "unknown" and status.get("initialized_at"):
        status["status"] = "running"

    return status


def _extract_timestamp(line: str) -> str:
    """Extract timestamp from a log line like '2026-05-16 22:33:31 | INFO | ...'"""
    parts = line.split(" | ")
    if parts:
        return parts[0].strip()
    return ""


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Hermes Trading Bot Dashboard", version="1.0.0")


def verify_password(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify dashboard password. Returns 401 if wrong."""
    if credentials is None:
        # No auth header — redirect to login form (handled by frontend)
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="Hermes Trading Bot Dashboard"'},
        )
    # Compare passwords (the password is the user field, no actual username)
    provided = credentials.password
    if provided != DASHBOARD_PASSWORD:
        raise HTTPException(
            status_code=401,
            detail="Invalid password",
            headers={"WWW-Authenticate": 'Basic realm="Hermes Trading Bot Dashboard"'},
        )
    return True


# CORS — restrict to localhost only (P3-20)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8999", "http://127.0.0.1:8999"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

# Mount static files (for index.html and any other assets)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Simple health check — no password required."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/status")
def api_status(auth: bool = Depends(verify_password)):
    """Bot running status, mode, uptime, last analysis."""
    log_lines = read_log_tail(100)
    status = parse_bot_status_from_log(log_lines)

    # Calculate uptime if we have initialization time
    if status.get("initialized_at") and status["status"] == "running":
        try:
            init_time = datetime.fromisoformat(status["initialized_at"])
            now = datetime.now(timezone.utc)
            if init_time.tzinfo is None:
                init_time = init_time.replace(tzinfo=timezone.utc)
            uptime_seconds = int((now - init_time).total_seconds())
            hours, remainder = divmod(uptime_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            status["uptime"] = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            status["uptime_seconds"] = uptime_seconds
        except Exception:
            pass

    return status


@app.get("/api/balance")
def api_balance(auth: bool = Depends(verify_password)):
    """Wallet balances from Bitfinex (live API) or fallback from config."""
    # Try live Bitfinex API
    wallets = fetch_bitfinex_balance()
    if wallets:
        balance_data = {"source": "bitfinex_live", "wallets": []}
        total_usd = 0.0

        for w in wallets:
            if not isinstance(w, list) or len(w) < 5:
                continue
            w_type = w[0]  # exchange, margin, funding
            currency = w[1]
            balance_str = w[2]
            # w[3] = unsettled interest, w[4] = available
            available_str = w[4] if len(w) > 4 else balance_str

            try:
                balance_val = float(balance_str)
                avail_val = float(available_str)
            except (ValueError, TypeError):
                continue

            entry = {
                "type": w_type,
                "currency": currency,
                "balance": round(balance_val, 8),
                "available": round(avail_val, 8),
            }
            balance_data["wallets"].append(entry)
            # Estimate total in USD for stablecoins
            if currency in ("UST", "USDT", "USD"):
                total_usd += balance_val

        balance_data["total_usd"] = round(total_usd, 2)
        return balance_data

    # Fallback: use config initial_capital
    config = read_config()
    capital = config.get("trading", {}).get("initial_capital", 0)
    return {
        "source": "config_fallback",
        "wallets": [
            {
                "type": "exchange",
                "currency": "USDT",
                "balance": capital,
                "available": capital,
            }
        ],
        "total_usd": capital,
    }


@app.get("/api/trades")
def api_trades(auth: bool = Depends(verify_password)):
    """Trade history from SQLite DB(s)."""
    trades = read_trades()
    # Return most recent first
    trades.reverse()
    return {"trades": trades, "count": len(trades)}


@app.get("/api/config")
def api_config(auth: bool = Depends(verify_password)):
    """Bot configuration."""
    config = read_config()
    if not config:
        return {"error": "Configuration not available"}

    # Extract meaningful summary
    trading = config.get("trading", {})
    exchange = config.get("exchange", {})
    strategies = config.get("strategies", {})
    risk = config.get("risk", {})

    summary = {
        "symbol": trading.get("symbol", "N/A"),
        "initial_capital": trading.get("initial_capital", 0),
        "daily_target": trading.get("daily_target", 0),
        "max_risk_per_trade_pct": trading.get("max_risk_per_trade", 0) * 100,
        "max_open_positions": trading.get("max_open_positions", 0),
        "position_size_mode": trading.get("position_size_mode", "N/A"),
        "exchange_name": exchange.get("name", "N/A"),
        "mode": exchange.get("testnet", False) and "TESTNET" or "LIVE",
        "default_wallet_type": exchange.get("default_type", "N/A"),
        "enabled_strategies": strategies.get("enabled", []),
        "stop_loss_pct": risk.get("stop_loss_pct", 0),
        "take_profit_pct": risk.get("take_profit_pct", 0),
        "max_daily_loss": risk.get("max_daily_loss", 0),
        "max_drawdown_pct": risk.get("max_drawdown_pct", 0),
        "trailing_stop": risk.get("trailing_stop", False),
    }
    return summary


@app.get("/api/summary")
def api_summary(auth: bool = Depends(verify_password)):
    """Summary statistics: total trades, PnL, win rate, best/worst trade."""
    trades = read_trades()
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": 0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "best_trade": None,
            "worst_trade": None,
            "total_wins": 0,
            "total_losses": 0,
        }

    total = len(trades)
    wins = 0
    losses = 0
    pnl_sum = 0.0
    best_pnl = float("-inf")
    worst_pnl = float("inf")
    best_trade = None
    worst_trade = None

    for t in trades:
        try:
            pnl = float(t.get("pnl", 0))
        except (ValueError, TypeError):
            pnl = 0.0

        pnl_sum += pnl

        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

        if pnl > best_pnl:
            best_pnl = pnl
            best_trade = {
                "symbol": t.get("symbol"),
                "side": t.get("side"),
                "pnl": pnl,
                "timestamp": t.get("timestamp"),
            }

        if pnl < worst_pnl:
            worst_pnl = pnl
            worst_trade = {
                "symbol": t.get("symbol"),
                "side": t.get("side"),
                "pnl": pnl,
                "timestamp": t.get("timestamp"),
            }

    # Ties (pnl == 0) are neither win nor loss
    decisive = wins + losses
    win_rate = round(wins / decisive * 100, 1) if decisive > 0 else 0.0
    avg_pnl = round(pnl_sum / total, 2) if total > 0 else 0.0

    return {
        "total_trades": total,
        "win_rate": win_rate,
        "total_pnl": round(pnl_sum, 2),
        "avg_pnl": avg_pnl,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "total_wins": wins,
        "total_losses": losses,
    }


@app.get("/")
def serve_dashboard(auth: bool = Depends(verify_password)):
    """Serve the main dashboard HTML — password protected."""
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return FileResponse(str(html_path))
    return JSONResponse({"error": "Dashboard HTML not found"}, status_code=404)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8999)
