#!/usr/bin/env python3
"""
Fetch fresh OHLCV data from Bitfinex for RL training
Simple version using direct API calls
"""

import requests
import pandas as pd
from pathlib import Path
from datetime import datetime

def fetch_bitfinex_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=1000):
    """
    Fetch OHLCV data from Bitfinex public API
    
    Args:
        symbol: Trading pair (e.g., "BTC/USDT")
        timeframe: Candle timeframe ("1h", "5m", etc.)
        limit: Number of candles to fetch (max 10000)
    
    Returns:
        pd.DataFrame with OHLCV data
    """
    # Convert symbol to Bitfinex format (tBTCUST)
    base, quote = symbol.split('/')
    if quote == 'USDT':
        quote = 'UST'
    bitfinex_symbol = f"t{base}{quote}"
    
    # Map timeframe to Bitfinex format
    tf_map = {
        '1m': '1m', '5m': '5m', '15m': '15m', '30m': '30m',
        '1h': '1h', '2h': '2h', '4h': '4h', '6h': '6h',
        '12h': '12h', '1d': '1D', '1w': '1W'
    }
    bitfinex_tf = tf_map.get(timeframe, '1h')
    
    url = f"https://api-pub.bitfinex.com/v2/candles/trade:{bitfinex_tf}:{bitfinex_symbol}/hist"
    
    # Get current timestamp in milliseconds for end parameter
    import time
    end_ms = int(time.time() * 1000)
    
    params = {
        'limit': min(limit, 10000),
        'sort': -1,  # Newest first (we'll reverse after)
        'end': end_ms
    }
    
    print(f"Fetching {limit} candles of {symbol} ({timeframe}) from Bitfinex...")
    print(f"URL: {url}")
    
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if not data or len(data) == 0:
            print("❌ No data returned from API")
            return None
        
        # Reverse to get oldest first
        data = data[::-1]
        
        # Bitfinex format: [timestamp, open, close, high, low, volume]
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'close', 'high', 'low', 'volume'])
        
        # Convert timestamp (milliseconds) to datetime
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        # Sort by timestamp (oldest first)
        df.sort_index(inplace=True)
        
        print(f"✅ Fetched {len(df)} rows")
        print(f"   Date range: {df.index[0]} to {df.index[-1]}")
        print(f"   Price range: ${df['low'].min():.2f} - ${df['high'].max():.2f}")
        print(f"   Current BTC price: ${df['close'].iloc[-1]:.2f}")
        
        # Save to file
        output_dir = Path("data/ohlcv")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        safe_symbol = symbol.replace('/', '_')
        output_file = output_dir / f"{safe_symbol}_{timeframe}.csv"
        
        df.to_csv(output_file)
        print(f"   Saved to: {output_file}")
        
        return df
        
    except requests.exceptions.RequestException as e:
        print(f"❌ API request failed: {e}")
        return None
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return None

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch fresh OHLCV data from Bitfinex")
    parser.add_argument("--symbol", default="BTC/USDT", help="Trading pair")
    parser.add_argument("--timeframe", default="1h", help="Timeframe (1m, 5m, 15m, 1h, etc.)")
    parser.add_argument("--limit", type=int, default=1000, help="Number of candles (max 10000)")
    args = parser.parse_args()
    
    df = fetch_bitfinex_ohlcv(args.symbol, args.timeframe, args.limit)
    
    if df is not None:
        print("\n✅ Data fetch complete!")
        print(f"   Ready for training with {len(df)} fresh candles")
    else:
        print("\n❌ Failed to fetch data")
        sys.exit(1)
