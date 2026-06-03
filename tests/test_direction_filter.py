"""
Unit tests for direction filtering in StrategySelector.
Tests that long/short instances correctly block opposite signals.
"""

import unittest
import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategies.selector import StrategySelector


class TestDirectionFilter(unittest.TestCase):
    """Test signal filtering based on trade direction."""
    
    def setUp(self):
        """Set up test configs."""
        self.base_config = {
            "strategies": {
                "enabled": ["trend_following"],
                "trend_following": {
                    "enabled": True,
                    "weight": 0.5,
                    "ema_fast": 9,
                    "ema_slow": 21
                }
            }
        }
    
    def test_long_instance_blocks_sell(self):
        """Long instance should convert SELL signals to HOLD."""
        selector = StrategySelector(self.base_config, trade_direction="long")
        
        signal = {
            "signal": "SELL",
            "confidence": 0.8,
            "reason": "Death cross detected",
            "name": "trend_following"
        }
        
        result = selector._filter_by_direction(signal)
        
        self.assertEqual(result["signal"], "HOLD")
        self.assertIn("BLOCKED", result["reason"])
        self.assertIn("long_only", result["reason"])
        self.assertEqual(result["confidence"], 0.8)  # Preserved
    
    def test_long_instance_allows_buy(self):
        """Long instance should allow BUY signals."""
        selector = StrategySelector(self.base_config, trade_direction="long")
        
        signal = {
            "signal": "BUY",
            "confidence": 0.75,
            "reason": "Golden cross",
            "name": "trend_following"
        }
        
        result = selector._filter_by_direction(signal)
        
        self.assertEqual(result["signal"], "BUY")
        self.assertNotIn("BLOCKED", result["reason"])
        self.assertEqual(result["confidence"], 0.75)
    
    def test_short_instance_blocks_buy(self):
        """Short instance should convert BUY signals to HOLD."""
        selector = StrategySelector(self.base_config, trade_direction="short")
        
        signal = {
            "signal": "BUY",
            "confidence": 0.9,
            "reason": "Breakout detected",
            "name": "trend_following"
        }
        
        result = selector._filter_by_direction(signal)
        
        self.assertEqual(result["signal"], "HOLD")
        self.assertIn("BLOCKED", result["reason"])
        self.assertIn("short_only", result["reason"])
    
    def test_short_instance_allows_sell(self):
        """Short instance should allow SELL signals."""
        selector = StrategySelector(self.base_config, trade_direction="short")
        
        signal = {
            "signal": "SELL",
            "confidence": 0.85,
            "reason": "RSI overbought",
            "name": "trend_following"
        }
        
        result = selector._filter_by_direction(signal)
        
        self.assertEqual(result["signal"], "SELL")
        self.assertNotIn("BLOCKED", result["reason"])
        self.assertEqual(result["confidence"], 0.85)
    
    def test_both_allows_all_signals(self):
        """Both direction should allow all signals."""
        selector = StrategySelector(self.base_config, trade_direction="both")
        
        buy_signal = {
            "signal": "BUY",
            "confidence": 0.8,
            "reason": "Golden cross",
            "name": "trend_following"
        }
        
        sell_signal = {
            "signal": "SELL",
            "confidence": 0.7,
            "reason": "Death cross",
            "name": "trend_following"
        }
        
        buy_result = selector._filter_by_direction(buy_signal)
        sell_result = selector._filter_by_direction(sell_signal)
        
        self.assertEqual(buy_result["signal"], "BUY")
        self.assertEqual(sell_result["signal"], "SELL")
        self.assertNotIn("BLOCKED", buy_result["reason"])
        self.assertNotIn("BLOCKED", sell_result["reason"])
    
    def test_hold_passes_through(self):
        """HOLD signals should pass through unchanged."""
        for direction in ["long", "short", "both"]:
            selector = StrategySelector(self.base_config, trade_direction=direction)
            
            signal = {
                "signal": "HOLD",
                "confidence": 0.3,
                "reason": "No clear trend",
                "name": "trend_following"
            }
            
            result = selector._filter_by_direction(signal)
            
            self.assertEqual(result["signal"], "HOLD")
            self.assertNotIn("BLOCKED", result["reason"])
    
    def test_lowercase_signal_handling(self):
        """Should handle lowercase signal values."""
        selector = StrategySelector(self.base_config, trade_direction="long")
        
        signal = {
            "signal": "sell",  # lowercase
            "confidence": 0.8,
            "reason": "Test",
            "name": "trend_following"
        }
        
        result = selector._filter_by_direction(signal)
        
        self.assertEqual(result["signal"], "HOLD")
    
    def test_missing_signal_defaults_to_hold(self):
        """Missing signal field should default to HOLD."""
        selector = StrategySelector(self.base_config, trade_direction="long")
        
        signal = {
            "confidence": 0.5,
            "reason": "Incomplete",
            "name": "trend_following"
        }
        
        result = selector._filter_by_direction(signal)
        
        self.assertEqual(result["signal"], "HOLD")
    
    def test_preserves_all_signal_fields(self):
        """Filtering should preserve all original fields."""
        selector = StrategySelector(self.base_config, trade_direction="long")
        
        signal = {
            "signal": "SELL",
            "confidence": 0.8,
            "reason": "Test reason",
            "name": "trend_following",
            "params": {"ema_fast": 9, "ema_slow": 21},
            "metadata": {"timestamp": 1234567890}
        }
        
        result = selector._filter_by_direction(signal)
        
        self.assertEqual(result["confidence"], 0.8)
        self.assertEqual(result["name"], "trend_following")
        self.assertEqual(result["params"], {"ema_fast": 9, "ema_slow": 21})
        self.assertEqual(result["metadata"], {"timestamp": 1234567890})


class TestStrategySelectorInit(unittest.TestCase):
    """Test StrategySelector initialization with trade_direction."""
    
    def test_default_direction_is_both(self):
        """Default trade_direction should be 'both'."""
        config = {"strategies": {"enabled": []}}
        selector = StrategySelector(config)
        
        self.assertEqual(selector.trade_direction, "both")
    
    def test_explicit_long_direction(self):
        """Can set explicit long direction."""
        config = {"strategies": {"enabled": []}}
        selector = StrategySelector(config, trade_direction="long")
        
        self.assertEqual(selector.trade_direction, "long")
    
    def test_explicit_short_direction(self):
        """Can set explicit short direction."""
        config = {"strategies": {"enabled": []}}
        selector = StrategySelector(config, trade_direction="short")
        
        self.assertEqual(selector.trade_direction, "short")


if __name__ == "__main__":
    unittest.main()
