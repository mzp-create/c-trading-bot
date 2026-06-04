"""Exception hierarchy for the Bitfinex package."""


class BitfinexError(Exception):
    """Base for all Bitfinex client errors."""


class OrderRejected(BitfinexError):
    """The exchange explicitly rejected the order (ack status == ERROR)."""


class AckUnparseable(BitfinexError):
    """Submit returned/raised something we cannot interpret as success or
    rejection. The order's outcome is UNKNOWN — callers must NOT auto-retry;
    reconcile via the next positions/trades read."""


class KeyConflictError(BitfinexError):
    """Another live instance is already running with this API key."""
