"""Typed Bitfinex client package (bfxapi for auth REST, ccxt for OHLCV).

BitfinexClient is the public entry point; it returns typed models (Order,
Position, Ticker, Wallet, Fill). Live auth goes through bfxapi; paper mode
simulates. See docs/superpowers/specs/2026-06-04-bitfinex-refactor-design.md.
"""

from bitfinex.models import Order, Position, Ticker, Wallet, Fill
from bitfinex.client import BitfinexClient

__all__ = ["BitfinexClient", "Order", "Position", "Ticker", "Wallet", "Fill"]
