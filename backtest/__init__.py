"""Backtest package — historical replay engine for the Hermes trading bot.

Exposes :class:`BacktestEngine`, used by ``main.py --mode backtest``.
"""

from backtest.engine import BacktestEngine

__all__ = ["BacktestEngine"]
