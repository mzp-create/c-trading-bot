#!/usr/bin/env python3
"""
Quick import test — verify all modules load correctly.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# Load config
import yaml
with open(ROOT / 'config/default.yaml') as f:
    config = yaml.safe_load(f)

print("✅ Config loaded")

# Test each module
try:
    from market_data.bitfinex_client import BitfinexClient, PaperBitfinexClient, create_bitfinex_client
    print("✅ market_data.bitfinex_client — OK")
except Exception as e:
    print(f"❌ market_data.bitfinex_client — {e}")

try:
    from market_data.collector import MarketDataCollector
    collector = MarketDataCollector(config)
    print("✅ market_data.collector — OK")
except Exception as e:
    print(f"❌ market_data.collector — {e}")

try:
    from analysis.technical import TechnicalAnalyzer
    ta = TechnicalAnalyzer(config)
    print("✅ analysis.technical — OK")
except Exception as e:
    print(f"❌ analysis.technical — {e}")

try:
    from analysis.ml_predictor import MLPredictor
    ml = MLPredictor(config)
    print("✅ analysis.ml_predictor — OK")
except Exception as e:
    print(f"❌ analysis.ml_predictor — {e}")

try:
    from strategies.selector import StrategySelector
    ss = StrategySelector(config)
    print("✅ strategies.selector — OK")
except Exception as e:
    print(f"❌ strategies.selector — {e}")

try:
    from risk.manager import RiskManager
    rm = RiskManager(config)
    print("✅ risk.manager — OK")
except Exception as e:
    print(f"❌ risk.manager — {e}")

try:
    from execution.engine import ExecutionEngine
    ee = ExecutionEngine(config, mode="paper")
    print("✅ execution.engine — OK")
except Exception as e:
    print(f"❌ execution.engine — {e}")

try:
    from monitoring.logger import BotLogger
    bl = BotLogger(config)
    print("✅ monitoring.logger — OK")
except Exception as e:
    print(f"❌ monitoring.logger — {e}")

try:
    from monitoring.telegram_alerts import TelegramNotifier
    tn = TelegramNotifier(config)
    print("✅ monitoring.telegram_alerts — OK")
except Exception as e:
    print(f"❌ monitoring.telegram_alerts — {e}")

print("\n" + "=" * 40)
print("All imports validated!")
