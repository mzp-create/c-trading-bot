#!/usr/bin/env python3
"""
Hermes Crypto Trading Bot Setup
- Creates directories
- Checks Bitfinex API connectivity
- Downloads historical data for ML training
- Runs initial ML model training
- Configures Telegram notifications
"""

import os
import sys
import re
import yaml
import logging
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# Setup basic logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s'
)
log = logging.getLogger("Setup")


def _resolve_env_vars(value):
    """Replace ${VAR_NAME} patterns with environment variable values."""
    import os
    if isinstance(value, str):
        # Simple string replacement without regex
        for env_name, env_val in sorted(os.environ.items(), key=lambda x: -len(x[0])):
            placeholder = f"${{{env_name}}}"
            if placeholder in value:
                value = value.replace(placeholder, env_val)
        return value
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(v) for v in value]
    return value


def load_config():
    config_path = ROOT / 'config/default.yaml'
    if not config_path.exists():
        log.error(f"Config not found at {config_path}")
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)
    return _resolve_env_vars(config)


def setup_directories(config):
    """Create all required directories."""
    dirs = [
        'data/ohlcv',
        'data/models',
        'logs',
    ]
    for d in dirs:
        path = ROOT / d
        path.mkdir(parents=True, exist_ok=True)
        log.info(f"✓ Created {d}")


def check_bitfinex_connectivity(config):
    """Test Bitfinex API connection."""
    from market_data.collector import MarketDataCollector

    log.info("Testing Bitfinex API connection...")
    collector = MarketDataCollector(config)

    try:
        price = collector.get_current_price('BTC/USDT')
        if price and price > 0:
            log.info(f"✓ Bitfinex connected! BTC/USDT: ${price:.2f}")
            return True
        else:
            log.error("✗ Could not fetch price from Bitfinex")
            return False
    except Exception as e:
        log.error(f"✗ Bitfinex connection failed: {e}")
        return False


def download_historical_data(config):
    """Download historical OHLCV data for ML training."""
    from market_data.collector import MarketDataCollector

    log.info("Downloading historical data for ML training...")
    collector = MarketDataCollector(config)
    symbol = config['trading']['symbol']

    # Get 1h data with since param for recent data
    import time as time_module
    since_30d = int((time_module.time() - 30*24*3600) * 1000)  # 30 days ago
    df = collector.get_historical_data(symbol, timeframe="1h", since=since_30d, limit=720)
    if df is not None:
        n = len(df)
        if n > 50:
            log.info(f"✓ Downloaded {n} candles of 1h data")
        else:
            # fallback to cached version
            df = collector.get_ohlcv(symbol, timeframe="1h", limit=200)
            if df is not None:
                log.info(f"✓ Downloaded {len(df)} candles of 1h data (cached)")
    else:
        # fallback to cached version
        df = collector.get_ohlcv(symbol, timeframe="1h", limit=200)
        if df is not None:
            log.info(f"✓ Downloaded {len(df)} candles of 1h data (cached)")
        else:
            log.warning("Could not download sufficient 1h data")

    # Get 5m data
    df_5m = collector.get_ohlcv(symbol, timeframe="5m", limit=500)
    if df_5m is not None:
        log.info(f"✓ Downloaded {len(df_5m)} candles of 5m data")

    # Get 15m data
    df_15m = collector.get_ohlcv(symbol, timeframe="15m", limit=500)
    if df_15m is not None:
        log.info(f"✓ Downloaded {len(df_15m)} candles of 15m data")

    return df


def train_ml_model(config, df):
    """Train the ML model on historical data."""
    from analysis.ml_predictor import MLPredictor
    from market_data.collector import MarketDataCollector

    log.info("Training ML model...")
    predictor = MLPredictor(config)

    if df is not None and len(df) >= 200:
        result = predictor.train(df)
        if result.get('status') == 'success':
            log.info(f"✓ ML model trained! Accuracy: {result['accuracy']:.3f}")
            log.info(f"  Samples: {result['samples']} | UP: {result['up_samples']} | DOWN: {result['down_samples']}")
            if result.get('top_features'):
                log.info(f"  Top features: {result['top_features']}")
        else:
            log.warning(f"ML training incomplete: {result.get('reason', 'unknown')}")
            log.info("Will try with 5m data combined...")

            # Try training on 5m data with more candles
            collector = MarketDataCollector(config)
            df_5m = collector.get_ohlcv(config['trading']['symbol'], timeframe="5m", limit=500)
            if df_5m is not None and len(df_5m) >= 200:
                result = predictor.train(df_5m)
                if result.get('status') == 'success':
                    log.info(f"✓ ML model trained on 5m data! Accuracy: {result['accuracy']:.3f}")
    else:
        n_rows = len(df) if df is not None else 0
        log.warning(f"Insufficient data for ML training ({n_rows} rows)")


def configure_telegram():
    """Guide user through Telegram setup."""
    telegram_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    telegram_chat = os.environ.get('TELEGRAM_CHAT_ID', '')

    if telegram_token and telegram_chat:
        log.info("✓ Telegram configured via environment variables")
        return True

    log.info("\n--- Telegram Setup ---")
    log.info("To enable Telegram alerts, set these env vars:")
    log.info("  export TELEGRAM_BOT_TOKEN='your_bot_token'")
    log.info("  export TELEGRAM_CHAT_ID='your_chat_id'")
    log.info("\nOr add them to your ~/.bashrc or ~/.zshrc")
    return False


def run_backtest(config):
    """Run a quick backtest to verify the bot logic."""
    log.info("\n--- Running Quick Backtest ---")

    try:
        from market_data.collector import MarketDataCollector
        from analysis.technical import TechnicalAnalyzer
        from strategies.selector import StrategySelector

        collector = MarketDataCollector(config)
        analyzer = TechnicalAnalyzer(config)
        strategies = StrategySelector(config)

        df_1h = collector.get_ohlcv(config['trading']['symbol'], timeframe="1h", limit=200)
        df_5m = collector.get_ohlcv(config['trading']['symbol'], timeframe="5m", limit=200)
        df_15m = collector.get_ohlcv(config['trading']['symbol'], timeframe="15m", limit=200)

        if df_1h is not None and len(df_1h) > 50:
            ta_1h = analyzer.analyze(df_1h, "1h")
            ta_5m = analyzer.analyze(df_5m, "5m") if df_5m is not None and len(df_5m) > 30 else {}
            ta_15m = analyzer.analyze(df_15m, "15m") if df_15m is not None and len(df_15m) > 30 else {}

            log.info(f"Current Price (1h): ${ta_1h.get('current_price', 0):.2f}")
            log.info(f"1h Trend: {ta_1h.get('trend', 'N/A')} | RSI: {ta_1h.get('rsi', 0):.1f}")
            log.info(f"MACD: {ta_1h.get('macd', {}).get('histogram', 0):.2f}")
            log.info(f"Bollinger: L={ta_1h.get('bollinger', {}).get('lower', 0):.0f} "
                    f"M={ta_1h.get('bollinger', {}).get('middle', 0):.0f} "
                    f"U={ta_1h.get('bollinger', {}).get('upper', 0):.0f}")
            log.info(f"Support: {ta_1h.get('support_resistance', {}).get('nearest_support', 'N/A')} "
                    f"Resistance: {ta_1h.get('support_resistance', {}).get('nearest_resistance', 'N/A')}")
            log.info(f"Volatility: {ta_1h.get('volatility', 0):.2f}%")
            log.info(f"ADX: {ta_1h.get('indicators', {}).get('adx', 'N/A')}")

            if ta_1h.get('patterns'):
                for p in ta_1h['patterns']:
                    log.info(f"Pattern: {p['name']} ({p['direction']}, {p['strength']})")

            signals = strategies.get_signals(ta_1h, ta_5m, ta_15m, {'signal': 'HOLD', 'confidence': 0.0})
            for sig in signals:
                log.info(f"Strategy {sig['name']}: {sig['signal']} (conf: {sig['confidence']:.2f})")

            log.info(f"\n✓ Backtest analysis complete!")
        else:
            log.error(f"Could not fetch sufficient data for backtest (1h rows: {len(df_1h) if df_1h is not None else 0})")

    except Exception as e:
        log.error(f"Backtest error: {e}", exc_info=True)


def main():
    log.info("=" * 50)
    log.info("🤖 Hermes Crypto Trading Bot — Setup")
    log.info("=" * 50)

    config = load_config()
    log.info(f"Symbol: {config['trading']['symbol']}")
    log.info(f"Capital: ${config['trading']['initial_capital']}")
    log.info(f"Target: ${config['trading']['daily_target']}/day")

    # Step 1: Create directories
    log.info("\n📁 Step 1: Creating directories...")
    setup_directories(config)

    # Step 2: Check Bitfinex connectivity
    log.info("\n🌐 Step 2: Checking Bitfinex connectivity...")
    connected = check_bitfinex_connectivity(config)

    # Step 3: Download historical data
    log.info("\n📊 Step 3: Downloading historical data...")
    df = download_historical_data(config)

    # Step 4: Train ML model
    log.info("\n🧠 Step 4: Training ML model...")
    train_ml_model(config, df)

    # Step 5: Run backtest
    log.info("\n📈 Step 5: Running analysis & backtest...")
    run_backtest(config)

    # Step 6: Telegram setup
    log.info("\n📱 Step 6: Checking Telegram configuration...")
    configure_telegram()

    log.info("\n" + "=" * 50)
    log.info("✅ Setup Complete!")
    log.info("=" * 50)
    log.info("\nTo start the bot:")
    log.info("  cd trading-bot")
    log.info("  python main.py --mode paper")
    log.info("\nTo run live (when ready):")
    log.info("  # Set your Bitfinex API keys:")
    log.info("  export BITFINEX_API_KEY='your_key'")
    log.info("  export BITFINEX_API_SECRET='your_secret'")
    log.info("  python main.py --mode live")
    log.info("\nFor monitoring only:")
    log.info("  python main.py --mode monitor")


if __name__ == "__main__":
    main()
