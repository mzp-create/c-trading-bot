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
