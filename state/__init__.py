"""Thread-safe in-memory state stores fed by the WS feed (Phase 3).

MarketState holds latest prices; AccountState holds positions/wallets/fills.
The synchronous engine reads these instead of polling REST when the feed is
healthy. See docs/superpowers/specs/2026-06-04-websocket-feed-design.md.
"""
