import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from bitfinex.keyguard import register_key, release_key
from bitfinex.errors import KeyConflictError


def test_same_key_different_instance_conflicts(tmp_path):
    reg = str(tmp_path / "reg.json")
    register_key(reg, instance="long", api_key="KEYAAA", pid=os.getpid())
    # A different instance, same key, with a LIVE pid (this process) -> conflict.
    with pytest.raises(KeyConflictError):
        register_key(reg, instance="short", api_key="KEYAAA", pid=os.getpid())


def test_distinct_keys_ok(tmp_path):
    reg = str(tmp_path / "reg.json")
    register_key(reg, instance="long", api_key="KEYAAA", pid=os.getpid())
    register_key(reg, instance="short", api_key="KEYBBB", pid=os.getpid())  # no raise


def test_stale_pid_is_reaped(tmp_path):
    reg = str(tmp_path / "reg.json")
    dead_pid = 2_000_000_000  # not a live process
    register_key(reg, instance="long", api_key="KEYAAA", pid=dead_pid)
    # Same key, new instance, but the prior holder's pid is dead -> reaped, OK.
    register_key(reg, instance="short", api_key="KEYAAA", pid=os.getpid())


def test_registry_stores_hash_not_key(tmp_path):
    reg = str(tmp_path / "reg.json")
    register_key(reg, instance="long", api_key="SECRETKEY", pid=os.getpid())
    assert "SECRETKEY" not in Path(reg).read_text()


def test_release(tmp_path):
    reg = str(tmp_path / "reg.json")
    register_key(reg, instance="long", api_key="KEYAAA", pid=os.getpid())
    release_key(reg, api_key="KEYAAA")
    # After release, another instance may take the same key.
    register_key(reg, instance="short", api_key="KEYAAA", pid=os.getpid())
