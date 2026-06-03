"""Unit tests for RiskManager.calculate_position_size.

These guard the two bugs fixed in the sizing model:
  1. Leverage must actually affect the returned size (it used to be a no-op
     because the result was always clamped to a hardcoded 20% of capital).
  2. A position whose required margin exceeds available capital (liquidation
     risk) must be rejected (return 0).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from risk.manager import RiskManager


def _make_manager(leverage, *, stop_loss_pct=2.0, max_risk=0.02,
                  max_single_pct=0.20, min_value=0.0):
    return RiskManager({
        "trading": {
            "max_risk_per_trade": max_risk,
            "min_position_value_usd": min_value,
        },
        "risk": {
            "leverage": leverage,
            "stop_loss_pct": stop_loss_pct,
            "max_single_pct": max_single_pct,
        },
    })


def test_leverage_changes_position_size():
    """Higher leverage must raise the cap and therefore the returned size.

    We use high confidence + high risk so the risk-based notional exceeds the
    cap, making the cap (which scales with leverage) the binding constraint.
    """
    price = 100.0
    capital = 1000.0
    common = dict(stop_loss_pct=2.0, max_risk=0.05, max_single_pct=0.20)

    low = _make_manager(2.0, **common)
    high = _make_manager(4.0, **common)

    size_low = low.calculate_position_size(capital, price, confidence=1.0,
                                           signal_type="BUY")
    size_high = high.calculate_position_size(capital, price, confidence=1.0,
                                             signal_type="BUY")

    assert size_low > 0
    assert size_high > size_low, (
        f"leverage no-op: 2x={size_low} 4x={size_high}"
    )


def test_leverage_cap_is_proportional():
    """Cap = capital * leverage * max_single_pct, so 4x cap == 2 * 2x cap."""
    price = 100.0
    capital = 1000.0
    common = dict(stop_loss_pct=2.0, max_risk=0.05, max_single_pct=0.20)

    size_2x = _make_manager(2.0, **common).calculate_position_size(
        capital, price, confidence=1.0, signal_type="BUY")
    size_4x = _make_manager(4.0, **common).calculate_position_size(
        capital, price, confidence=1.0, signal_type="BUY")

    # Both are capped, so the ratio should track the leverage ratio.
    assert abs(size_4x / size_2x - 2.0) < 1e-6


def test_risk_based_sizing_loses_about_risk_budget():
    """At default 2% risk / 2% stop, hitting the stop loses ~2% of capital."""
    price = 100.0
    capital = 1000.0
    # Use leverage high enough that the cap does not bind, isolating the
    # risk-based notional.
    mgr = _make_manager(10.0, stop_loss_pct=2.0, max_risk=0.02,
                        max_single_pct=0.20)
    # confidence 0.5 => conf_mult 1.0 (neutral)
    size = mgr.calculate_position_size(capital, price, confidence=0.5,
                                       signal_type="BUY")
    notional = size * price
    loss_at_stop = notional * 0.02  # 2% adverse move
    assert abs(loss_at_stop - capital * 0.02) < 1e-6


def test_oversized_notional_rejected_by_margin_guard():
    """If required margin (notional/leverage) > capital, skip the trade.

    With leverage 1x, a tiny stop and high risk drive a notional far above
    capital; required margin == notional > capital, so it must return 0.
    """
    price = 100.0
    capital = 1000.0
    mgr = _make_manager(1.0, stop_loss_pct=0.5, max_risk=0.05,
                        max_single_pct=5.0)  # huge cap so the guard, not the
                                             # cap, is what rejects it
    size = mgr.calculate_position_size(capital, price, confidence=1.0,
                                       signal_type="BUY")
    assert size == 0.0


def test_zero_inputs_return_zero():
    mgr = _make_manager(2.0)
    assert mgr.calculate_position_size(0.0, 100.0, 1.0, "BUY") == 0.0
    assert mgr.calculate_position_size(1000.0, 0.0, 1.0, "BUY") == 0.0
