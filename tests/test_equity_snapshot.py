"""Regression tests for TradingBot._equity_snapshot_values — the balance must
reflect CUMULATIVE realized PnL (true account), not just intraday daily_pnl
(which resets at the daily boundary). 2026-06-09: bot reported ~$535 while the
real wallet was $516.53 because prior-day realized losses were dropped at the
daily reset."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import main as main_mod


class _Repo:
    def __init__(self, realized):
        self._realized = realized

    def total_realized_pnl(self):
        return self._realized


class _Executor:
    def __init__(self, realized, positions):
        self._repo = _Repo(realized)
        self.open_positions = positions


def _bot(initial_capital, realized, daily_pnl, positions):
    bot = object.__new__(main_mod.TradingBot)
    bot.initial_capital = initial_capital
    bot.daily_pnl = daily_pnl          # must be IGNORED for the balance figure
    bot.executor = _Executor(realized, positions)
    return bot


def test_balance_uses_cumulative_realized_not_daily():
    # 536 start; -21.28 realized across all days; today only -0.61.
    # Balance MUST be 536 + (-21.28) = 514.72, NOT 536 + daily (535.39).
    bot = _bot(536.0, -21.28, -0.61, [])
    balance, equity = bot._equity_snapshot_values()
    assert balance == 514.72
    assert equity == 514.72  # no open positions -> equity == balance


def test_equity_adds_unrealized_to_realized_balance():
    bot = _bot(536.0, -21.28, 0.0, [{"unrealized_pnl": 2.0}])
    balance, equity = bot._equity_snapshot_values()
    assert balance == 514.72
    assert equity == 516.72


def test_balance_survives_when_realized_pnl_unavailable():
    # Repo error must not raise out of the best-effort snapshot.
    class _BoomRepo:
        def total_realized_pnl(self):
            raise RuntimeError("db down")

    bot = object.__new__(main_mod.TradingBot)
    bot.initial_capital = 536.0
    bot.daily_pnl = -0.61
    bot.executor = type("E", (), {"_repo": _BoomRepo(), "open_positions": []})()
    balance, equity = bot._equity_snapshot_values()
    assert isinstance(balance, float) and isinstance(equity, float)
