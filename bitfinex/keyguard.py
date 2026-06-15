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


def _claim(registry_path: str, *, slot: str, instance: str, pid: int,
           conflict_msg) -> None:
    """Claim `slot` for `instance`/`pid`. Raise KeyConflictError (built by
    `conflict_msg(held)`) if a DIFFERENT live instance/pid already holds it.
    Reaps dead pids. Slots namespace key claims and account claims in one file."""
    path = Path(registry_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as lockf:
        if fcntl is not None:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            reg = _load(path)
            # Reap dead holders.
            reg = {k: v for k, v in reg.items() if _pid_alive(v.get("pid", -1))}
            held = reg.get(slot)
            if held and not (held["instance"] == instance and held["pid"] == pid):
                raise KeyConflictError(conflict_msg(held))
            reg[slot] = {
                "instance": instance, "pid": pid,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            path.write_text(json.dumps(reg))
        finally:
            if fcntl is not None:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def _release(registry_path: str, *, slot: str) -> None:
    """Drop a registry slot (best-effort; called on shutdown)."""
    path = Path(registry_path)
    if not path.exists():
        return
    with open(path, "a+") as lockf:
        if fcntl is not None:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            reg = _load(path)
            reg.pop(slot, None)
            path.write_text(json.dumps(reg))
        finally:
            if fcntl is not None:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def register_key(registry_path: str, *, instance: str, api_key: str,
                 pid: int) -> None:
    """Claim `api_key` for `instance`/`pid`. Raise KeyConflictError if another
    instance with a LIVE pid already holds the same key. Reaps dead pids."""
    _claim(registry_path, slot=_fingerprint(api_key), instance=instance, pid=pid,
           conflict_msg=lambda h: (
               f"instance {h['instance']!r} (pid {h['pid']}) is already running "
               f"with this API key; {instance!r} needs a separate key"))


def release_key(registry_path: str, *, api_key: str) -> None:
    """Drop this key's registry entry (best-effort; called on shutdown)."""
    _release(registry_path, slot=_fingerprint(api_key))


def register_account(registry_path: str, *, instance: str, account_id: str,
                     pid: int) -> None:
    """Claim a Bitfinex ACCOUNT for `instance`/`pid`. Raise KeyConflictError if
    another live instance already runs on the same account.

    Two distinct API keys on ONE Bitfinex account net each other's positions
    per symbol — the dual long/short design requires a separate (sub-)account
    per instance. This guard refuses the dangerous shared-account state."""
    _claim(registry_path, slot=f"acct:{account_id}", instance=instance, pid=pid,
           conflict_msg=lambda h: (
               f"instance {h['instance']!r} (pid {h['pid']}) is already running "
               f"live on Bitfinex account {account_id}; {instance!r} needs its "
               f"OWN sub-account — two API keys on one account net each other's "
               f"positions"))


def release_account(registry_path: str, *, account_id: str) -> None:
    """Drop this account's registry entry (best-effort; called on shutdown)."""
    _release(registry_path, slot=f"acct:{account_id}")
