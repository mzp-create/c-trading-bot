import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_errors_importable():
    from bitfinex.errors import (
        BitfinexError, OrderRejected, AckUnparseable, KeyConflictError,
    )
    assert issubclass(OrderRejected, BitfinexError)
    assert issubclass(AckUnparseable, BitfinexError)
    assert issubclass(KeyConflictError, BitfinexError)
