#!/usr/bin/env python3
"""
Tests for the keyguard — verifies that a second LIVE instance is blocked
from using the same API key as an already-running instance.

Replaces the old CCXT-nonce integration test (bfxapi now manages its own
auth, so the old ccxt nonce-file behavior no longer exists).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest
from bitfinex.keyguard import register_key
from bitfinex.errors import KeyConflictError


def test_same_key_second_instance_blocked(tmp_path):
    """A second instance with the same API key must raise KeyConflictError."""
    reg = str(tmp_path / "reg.json")
    register_key(reg, instance="long", api_key="SAMEKEY", pid=os.getpid())
    with pytest.raises(KeyConflictError):
        register_key(reg, instance="short", api_key="SAMEKEY", pid=os.getpid())


def test_different_keys_allowed(tmp_path):
    """Two instances with different API keys must coexist without error."""
    reg = str(tmp_path / "reg.json")
    register_key(reg, instance="long", api_key="KEY_ALPHA", pid=os.getpid())
    # Should not raise — different key fingerprint
    register_key(reg, instance="short", api_key="KEY_BETA", pid=os.getpid())


def test_same_instance_reregisters_ok(tmp_path):
    """Re-registering the same instance/pid pair is idempotent (no error)."""
    reg = str(tmp_path / "reg.json")
    register_key(reg, instance="long", api_key="MYKEY", pid=os.getpid())
    # Same instance + same pid => should succeed (idempotent)
    register_key(reg, instance="long", api_key="MYKEY", pid=os.getpid())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
