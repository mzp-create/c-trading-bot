"""Per-instance API-key registry. Prevents two LIVE instances from sharing one
API key (the dual-instance 'nonce: small' root cause). Stores only a hash."""

import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone

from bitfinex.errors import KeyConflictError

try:
    import fcntl  # POSIX file locking
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


def _fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text() or "{}")
    except (ValueError, OSError):
        return {}


def register_key(registry_path: str, *, instance: str, api_key: str,
                 pid: int) -> None:
    """Claim `api_key` for `instance`/`pid`. Raise KeyConflictError if another
    instance with a LIVE pid already holds the same key. Reaps dead pids."""
    path = Path(registry_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fp = _fingerprint(api_key)
    with open(path, "a+") as lockf:
        if fcntl is not None:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            reg = _load(path)
            # Reap dead holders.
            reg = {k: v for k, v in reg.items() if _pid_alive(v.get("pid", -1))}
            held = reg.get(fp)
            if held and not (held["instance"] == instance and held["pid"] == pid):
                raise KeyConflictError(
                    f"instance {held['instance']!r} (pid {held['pid']}) is "
                    f"already running with this API key; {instance!r} needs a "
                    f"separate key")
            reg[fp] = {
                "instance": instance, "pid": pid,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            path.write_text(json.dumps(reg))
        finally:
            if fcntl is not None:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def release_key(registry_path: str, *, api_key: str) -> None:
    """Drop this key's registry entry (best-effort; called on shutdown)."""
    path = Path(registry_path)
    if not path.exists():
        return
    fp = _fingerprint(api_key)
    with open(path, "a+") as lockf:
        if fcntl is not None:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            reg = _load(path)
            reg.pop(fp, None)
            path.write_text(json.dumps(reg))
        finally:
            if fcntl is not None:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
